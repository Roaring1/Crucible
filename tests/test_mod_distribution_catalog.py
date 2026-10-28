from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from crucible.mods import distribution_catalog


class DistributionCatalogTests(unittest.TestCase):
    def _record(self, digest: str, **overrides):
        row = {
            "sha256": digest,
            "modId": "examplemod",
            "modVersion": "1.2.3",
            "decision": "permitted",
            "projectUrl": "https://example.org/project",
            "evidenceUrl": "https://example.org/redistribution-policy",
            "verifiedOn": "2026-10-01",
            "basis": "Redistribution permission is explicit in the linked policy.",
            "license": "Example-License-1.0",
            "licenseUrl": "https://example.org/license",
            "conditions": [],
            "alternativeDownloadUrl": "",
            "notes": "Synthetic fixture only; not a real project permission.",
        }
        row.update(overrides)
        return row

    def _write(self, path: Path, records):
        path.write_text(json.dumps({
            "schemaVersion": 1,
            "catalogVersion": "test-1",
            "records": records,
        }), encoding="utf-8")

    def test_empty_shipped_catalog_defaults_to_unknown(self):
        catalog = distribution_catalog.load_catalog()
        self.assertEqual(catalog.records, {})
        self.assertEqual(len(catalog.revision), 64)
        self.assertIsNone(catalog.lookup("f" * 64))

    def test_exact_sha256_match_only(self):
        digest = hashlib.sha256(b"synthetic jar bytes").hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            self._write(path, [self._record(digest)])
            catalog = distribution_catalog.load_catalog(path)
        self.assertEqual(catalog.lookup(digest).decision, "permitted")
        self.assertIsNone(catalog.lookup(hashlib.sha256(b"changed jar bytes").hexdigest()))
        self.assertEqual(len(catalog.revision), 64)

    def test_prohibited_record_can_carry_a_reviewed_alternative(self):
        digest = "a" * 64
        row = self._record(
            digest,
            decision="prohibited",
            license="",
            licenseUrl="",
            alternativeDownloadUrl="https://downloads.example.org/examplemod",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            self._write(path, [row])
            record = distribution_catalog.load_catalog(path).lookup(digest)
        self.assertEqual(record.decision, "prohibited")
        self.assertEqual(record.alternative_download_url, "https://downloads.example.org/examplemod")
        self.assertEqual(record.review_entry(
            archive_path="Server/.minecraft/mods/example.jar",
            filename="example.jar",
            bundled=False,
        )["bundled"], False)

    def test_conditional_record_requires_explicit_conditions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            self._write(path, [self._record("b" * 64, decision="conditional", conditions=[])])
            with self.assertRaises(distribution_catalog.DistributionCatalogError):
                distribution_catalog.load_catalog(path)

    def test_http_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            self._write(path, [self._record("c" * 64, evidenceUrl="http://example.org/policy")])
            with self.assertRaises(distribution_catalog.DistributionCatalogError):
                distribution_catalog.load_catalog(path)

    def test_duplicate_artifact_hashes_fail_closed(self):
        record = self._record("d" * 64)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            self._write(path, [record, record])
            with self.assertRaises(distribution_catalog.DistributionCatalogError):
                distribution_catalog.load_catalog(path)

    def test_malformed_catalog_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text('{"schemaVersion":', encoding="utf-8")
            with self.assertRaises(distribution_catalog.DistributionCatalogError):
                distribution_catalog.load_catalog(path)


if __name__ == "__main__":
    unittest.main()
