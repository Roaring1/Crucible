from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from crucible.sharing import gofile
from crucible.sharing.provider import UploadProvider


class _FakeSocket:
    def settimeout(self, _timeout):
        pass


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self, limit=-1):
        if limit < 0:
            return self._payload
        return self._payload[:limit]


class _FakeHTTPSConnection:
    response_payload = None
    response_status = 200
    connect_error = None
    latest = None

    def __init__(self, host, timeout, context):
        self.host = host
        self.timeout = timeout
        self.context = context
        self.sock = _FakeSocket()
        self.headers = {}
        self.header_pairs = []
        self.body = bytearray()
        self.send_sizes = []
        self.request_line = None
        self.closed = False
        _FakeHTTPSConnection.latest = self

    def connect(self):
        if _FakeHTTPSConnection.connect_error is not None:
            raise _FakeHTTPSConnection.connect_error

    def putrequest(self, method, path):
        self.request_line = (method, path)
        # Mimic http.client's default automatic Host header.
        self.headers["host"] = self.host
        self.header_pairs.append(("host", self.host))

    def putheader(self, name, value):
        self.headers[name.lower()] = value
        self.header_pairs.append((name.lower(), value))

    def endheaders(self):
        pass

    def send(self, data):
        self.send_sizes.append(len(data))
        self.body.extend(data)

    def getresponse(self):
        return _FakeResponse(self.response_status, self.response_payload)

    def close(self):
        self.closed = True


class GofileUploadTests(unittest.TestCase):
    def setUp(self):
        _FakeHTTPSConnection.response_status = 200
        _FakeHTTPSConnection.connect_error = None
        _FakeHTTPSConnection.latest = None
        self._patcher = patch(
            "crucible.sharing.gofile.http.client.HTTPSConnection",
            _FakeHTTPSConnection,
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _payload(self, content: bytes, *, overrides=None):
        data = {
            "status": "ok",
            "data": {
                "id": "file-id-123",
                "name": "CreatePlus - Player Pack.zip",
                "size": len(content),
                "md5": hashlib.md5(content).hexdigest(),
                "downloadPage": "https://gofile.io/d/AbCdEf123",
                "guestToken": "never-save-or-log-this-guest-token",
                "parentFolder": "folder-uuid",
            },
        }
        if overrides:
            data["data"].update(overrides)
        return data

    def test_stream_upload_uses_guest_endpoint_and_verifies_size_md5(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "CreatePlus - Player Pack.zip"
            content = b"small synthetic zip bytes"
            path.write_bytes(content)
            expected_sha = hashlib.sha256(content).hexdigest()
            _FakeHTTPSConnection.response_payload = self._payload(content)
            progress = []

            result = gofile.upload_file(
                path,
                expected_sha256=expected_sha,
                progress_cb=lambda done, total: progress.append((done, total)),
            )

            conn = _FakeHTTPSConnection.latest
            self.assertEqual(conn.host, "upload.gofile.io")
            self.assertEqual(conn.request_line, ("POST", "/uploadfile"))
            self.assertEqual(
                sum(1 for name, _ in conn.header_pairs if name == "host"),
                1,
            )
            self.assertNotIn("authorization", conn.headers)
            self.assertTrue(conn.headers["content-type"].startswith("multipart/form-data; boundary="))
            self.assertEqual(int(conn.headers["content-length"]), len(conn.body))
            self.assertIn(b"CreatePlus - Player Pack.zip", conn.body)
            self.assertIn(content, conn.body)
            self.assertTrue(conn.closed)
            self.assertTrue(progress)
            self.assertEqual(progress[-1], (len(content), len(content)))
            self.assertEqual(result.download_url, "https://gofile.io/d/AbCdEf123")
            self.assertEqual(result.local_sha256, expected_sha)
            self.assertTrue(result.size_verified)
            self.assertTrue(result.md5_verified)
            self.assertNotIn("guestToken", repr(result))

    def test_gofile_provider_conforms_to_provider_protocol(self):
        provider = gofile.GofileProvider()
        self.assertIsInstance(provider, UploadProvider)
        self.assertEqual(provider.name, "Gofile")

    def test_cancel_during_stream_stops_before_sending_the_entire_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.zip"
            content = b"x" * (gofile._CHUNK_BYTES + 123)
            path.write_bytes(content)
            expected_sha = hashlib.sha256(content).hexdigest()
            _FakeHTTPSConnection.response_payload = self._payload(content)
            cancel = threading.Event()
            progress = []

            def on_progress(done, total):
                progress.append((done, total))
                if done:
                    cancel.set()

            with self.assertRaises(gofile.UploadCancelled):
                gofile.upload_file(
                    path,
                    expected_sha256=expected_sha,
                    progress_cb=on_progress,
                    cancel_event=cancel,
                )

            conn = _FakeHTTPSConnection.latest
            self.assertIsNotNone(conn)
            self.assertTrue(conn.closed)
            self.assertEqual(progress, [(0, len(content)), (gofile._CHUNK_BYTES, len(content))])
            # The multipart header and first chunk were sent, but the tail and
            # final bytes were not; cancellation is best-effort after the first
            # network write and never discards the local file.
            self.assertLess(len(conn.body), len(content) + 1024)

    def test_optional_owner_token_is_sent_only_as_authorization_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "player-pack.zip"
            content = b"synthetic player pack"
            path.write_bytes(content)
            secret = "owner-token-must-not-escape-this-call"
            _FakeHTTPSConnection.response_payload = self._payload(content)
            result = gofile.GofileProvider(api_token=secret).upload_file(
                path,
                expected_sha256=hashlib.sha256(content).hexdigest(),
            )
            conn = _FakeHTTPSConnection.latest
            self.assertEqual(conn.headers.get("authorization"), f"Bearer {secret}")
            self.assertNotIn(secret.encode(), conn.body)
            self.assertNotIn(secret, repr(result))
            self.assertNotIn(secret, result.download_url)

    def test_changed_local_file_is_rejected_before_network_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.zip"
            path.write_bytes(b"changed bytes")
            with self.assertRaisesRegex(gofile.GofileUploadError, "changed after it was built"):
                gofile.upload_file(path, expected_sha256="0" * 64)
            self.assertIsNone(_FakeHTTPSConnection.latest)

    def test_response_rejects_non_gofile_download_link(self):
        payload = self._payload(b"file", overrides={
            "downloadPage": "https://evil.example/d/AbCdEf123",
        })
        with self.assertRaisesRegex(gofile.GofileUploadError, "outside its expected HTTPS"):
            gofile._decode_response(
                json.dumps(payload).encode(),
                200,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )

    def test_response_rejects_md5_mismatch(self):
        payload = self._payload(b"file", overrides={"md5": "0" * 32})
        with self.assertRaisesRegex(gofile.GofileUploadError, "MD5 differs"):
            gofile._decode_response(
                json.dumps(payload).encode(),
                200,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )

    def test_response_rejects_remote_size_mismatch(self):
        payload = self._payload(b"file", overrides={"size": 8})
        with self.assertRaisesRegex(gofile.GofileUploadError, "file size differs"):
            gofile._decode_response(
                json.dumps(payload).encode(),
                200,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )

    def test_response_requires_https_gofile_download_page(self):
        for value in (
            "http://gofile.io/d/abc",
            "https://gofile.io.evil.example/d/abc",
            "https://user@gofile.io/d/abc",
            "https://gofile.io/abc",
            "https://gofile.io/d/abc?token=secret",
            "https://gofile.io:bad/d/abc",
            " https://gofile.io/d/abc",
            "https://gofile.io/d/../evil",
            "https://gofile.io/d/a%2Fb",
        ):
            with self.subTest(value=value):
                with self.assertRaises(gofile.GofileUploadError):
                    gofile._decode_response(
                        json.dumps(self._payload(b"file", overrides={"downloadPage": value})).encode(),
                        200,
                        expected_size=4,
                        local_md5=hashlib.md5(b"file").hexdigest(),
                        local_sha256=hashlib.sha256(b"file").hexdigest(),
                    )

    def test_cancelled_upload_stops_before_network_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.zip"
            content = b"pack bytes"
            path.write_bytes(content)
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(gofile.UploadCancelled):
                gofile.upload_file(
                    path,
                    expected_sha256=hashlib.sha256(content).hexdigest(),
                    cancel_event=cancel,
                )
            self.assertIsNone(_FakeHTTPSConnection.latest)

    def test_plaintext_unauthorized_response_suggests_optional_owner_token(self):
        with self.assertRaisesRegex(gofile.GofileUploadError, "optional owner API token"):
            gofile._decode_response(
                b"Unauthorized",
                401,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )

    def test_payload_too_large_has_actionable_message(self):
        with self.assertRaisesRegex(gofile.GofileUploadError, "rejected the file size"):
            gofile._decode_response(
                b"",
                413,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )

    def test_http_rate_limit_has_actionable_message(self):
        _FakeHTTPSConnection.response_status = 429
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.zip"
            content = b"pack bytes"
            path.write_bytes(content)
            _FakeHTTPSConnection.response_payload = {"status": "error", "message": "rate limited"}
            with self.assertRaisesRegex(gofile.GofileUploadError, "rate-limited"):
                gofile.upload_file(path, expected_sha256=hashlib.sha256(content).hexdigest())


    def test_large_upload_is_sent_in_bounded_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "large-synthetic-pack.zip"
            content = b"z" * (gofile._CHUNK_BYTES * 2 + 19)
            path.write_bytes(content)
            _FakeHTTPSConnection.response_payload = self._payload(content)

            result = gofile.upload_file(
                path,
                expected_sha256=hashlib.sha256(content).hexdigest(),
            )

            conn = _FakeHTTPSConnection.latest
            self.assertIsNotNone(conn)
            self.assertEqual(int(conn.headers["content-length"]), len(conn.body))
            self.assertTrue(conn.send_sizes)
            self.assertLessEqual(max(conn.send_sizes), gofile._CHUNK_BYTES)
            self.assertEqual(result.file_size, len(content))
            self.assertTrue(result.md5_verified)

    def test_omitted_optional_integrity_metadata_is_not_claimed_verified(self):
        content = b"file"
        payload = self._payload(content)
        payload["data"].pop("size")
        payload["data"].pop("md5")

        result = gofile._decode_response(
            json.dumps(payload).encode(),
            200,
            expected_size=len(content),
            local_md5=hashlib.md5(content).hexdigest(),
            local_sha256=hashlib.sha256(content).hexdigest(),
        )

        self.assertFalse(result.size_verified)
        self.assertFalse(result.md5_verified)
        self.assertEqual(result.remote_md5, "")

    def test_malformed_present_integrity_metadata_is_rejected(self):
        content = b"file"
        invalid_values = (
            {"size": None},  # present-but-null is malformed, not omitted
            {"size": ""},
            {"size": "4"},
            {"size": True},
            {"size": -1},
            {"md5": None},  # present-but-null is malformed, not omitted
            {"md5": ""},
            {"md5": 123},
            {"md5": "not-an-md5"},
        )
        for overrides in invalid_values:
            with self.subTest(overrides=overrides):
                payload = self._payload(content, overrides=overrides)
                with self.assertRaises(gofile.GofileUploadError):
                    gofile._decode_response(
                        json.dumps(payload).encode(),
                        200,
                        expected_size=len(content),
                        local_md5=hashlib.md5(content).hexdigest(),
                        local_sha256=hashlib.sha256(content).hexdigest(),
                    )

    def test_network_timeout_is_wrapped_and_connection_is_closed(self):
        secret = "private-owner-token-value"
        _FakeHTTPSConnection.connect_error = TimeoutError(f"timed out with token {secret}")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pack.zip"
            content = b"synthetic player pack"
            path.write_bytes(content)

            with self.assertRaisesRegex(gofile.GofileUploadError, "Could not complete") as caught:
                gofile.upload_file(
                    path,
                    expected_sha256=hashlib.sha256(content).hexdigest(),
                    api_token=secret,
                )

            self.assertNotIn(secret, str(caught.exception))
            self.assertIsNotNone(_FakeHTTPSConnection.latest)
            self.assertTrue(_FakeHTTPSConnection.latest.closed)

    def test_provider_status_text_is_not_echoed_into_errors(self):
        secret = "owner-token-never-echo-this"
        payload = self._payload(b"file")
        payload["status"] = secret

        with self.assertRaises(gofile.GofileUploadError) as caught:
            gofile._decode_response(
                json.dumps(payload).encode(),
                200,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )

        self.assertNotIn(secret, str(caught.exception))

    def test_unreadable_json_and_missing_download_link_are_rejected(self):
        with self.assertRaisesRegex(gofile.GofileUploadError, "unreadable response"):
            gofile._decode_response(
                b"not json",
                200,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )

        payload = self._payload(b"file", overrides={"downloadPage": None})
        with self.assertRaises(gofile.GofileUploadError):
            gofile._decode_response(
                json.dumps(payload).encode(),
                200,
                expected_size=4,
                local_md5=hashlib.md5(b"file").hexdigest(),
                local_sha256=hashlib.sha256(b"file").hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
