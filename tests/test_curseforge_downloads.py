"""Offline regression coverage for the temporary CurseForge release fallback."""
import json
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace

import pytest

from crucible import download_http as http
from crucible.mods import curseforge as cf, modrinth as mr, versions
from crucible.importers import downloader as packs


def _key_read_is_a_failure():
    pytest.fail("disabled CurseForge paths must not inspect the configured key")


@pytest.mark.parametrize("host", sorted(http._CURSEFORGE_HOSTS))
def test_curseforge_hosts_are_blocked_before_key_read(host, monkeypatch):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    with pytest.raises(http.DownloadRequestError, match="temporarily disabled"):
        http.make_request(f"https://{host}/files/test.jar")


@pytest.mark.parametrize("host", ["cdn.modrinth.com", "edge.forgecdn.net.evil.invalid",
                                  "evil-forgecdn.net", "example.invalid"])
def test_non_curseforge_request_never_gets_a_key(host):
    request = http.make_request(f"https://{host}/file", headers={"X-API-KEY": "injected"})
    assert request.get_header("X-api-key") is None


@pytest.mark.parametrize("url", ["http://edge.forgecdn.net/file",
                                 "https://user:secret@edge.forgecdn.net/file",
                                 "https://edge.forgecdn.net:444/file",
                                 "https://edge.forgecdn.net/file?api-key=secret",
                                 "https://edge.forgecdn.net/file?api_key=secret"])
def test_unsafe_url_rejected_without_echoing_credentials(url):
    with pytest.raises(http.DownloadRequestError) as caught:
        http.make_request(url)
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("target", ["https://evil.invalid/leak",
                                    "http://edge.forgecdn.net/files/a.jar",
                                    "https://edge.forgecdn.net.evil.invalid/leak"])
def test_redirect_cannot_leak_credentials(target):
    # Construct a synthetic authenticated request so this test does not need
    # the disabled provider to issue a real request first.
    request = urllib.request.Request(
        "https://edge.forgecdn.net/files/a.jar",
        headers={"X-api-key": "test-only-not-a-real-key"},
    )
    with pytest.raises(http.DownloadRequestError):
        http.SafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, target)


def test_official_cdn_redirect_is_still_disabled(monkeypatch):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    request = urllib.request.Request(
        "https://edge.forgecdn.net/files/a.jar",
        headers={"X-api-key": "test-only-not-a-real-key"},
    )
    with pytest.raises(http.DownloadRequestError, match="temporarily disabled"):
        http.SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", {},
            "https://mediafilez.forgecdn.net/files/a.jar",
        )


def test_generic_redirect_stays_key_free():
    request = http.make_request("https://cdn.modrinth.com/a.jar")
    redirected = http.SafeRedirectHandler().redirect_request(
        request, None, 302, "Found", {}, "https://example.invalid/a.jar")
    assert redirected.get_header("X-api-key") is None


def test_browser_installer_blocks_curseforge_before_network(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    monkeypatch.setattr(http, "open_url", lambda *args, **kwargs: pytest.fail("network reached"))
    with pytest.raises(mr.ModrinthError, match="temporarily disabled"):
        mr._download("https://edge.forgecdn.net/files/a.jar", tmp_path / "a.jar")
    assert not (tmp_path / "a.jar").exists()


def test_curseforge_search_blocks_before_key_or_network(monkeypatch):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    monkeypatch.setattr(http, "open_url", lambda *args, **kwargs: pytest.fail("network reached"))
    with pytest.raises(mr.ModrinthError, match="temporarily disabled"):
        cf.search("example", loader="fabric", mc_version="1.21.1")


def test_old_curseforge_file_cannot_replace_an_existing_jar(tmp_path, monkeypatch):
    (tmp_path / "mods").mkdir()
    (tmp_path / "mods/old.jar").write_bytes(b"original")
    file = mr.ModFile(
        "cf:123", "Test", "7", "7", "new.jar",
        "https://edge.forgecdn.net/files/new.jar", "a" * 40, 3,
    )
    monkeypatch.setattr(
        mr, "_download",
        lambda *args, **kwargs: pytest.fail("blocked CurseForge file reached downloader"),
    )
    with pytest.raises(mr.ModrinthError, match="temporarily disabled"):
        versions.replace_files(tmp_path, "old.jar", [file])
    assert (tmp_path / "mods/old.jar").read_bytes() == b"original"
    assert not (tmp_path / ".crucible").exists()


def manifest():
    return {"files": [{"projectID": 123, "fileID": 456}]}


def test_user_manifest_can_be_parsed_but_curseforge_lookup_stays_disabled(monkeypatch):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    monkeypatch.setattr(packs, "_opener", lambda: pytest.fail("CurseForge API opener created"))
    items, notes = packs._curseforge_items(manifest(), "test-only-not-a-real-key")
    assert len(items) == 1
    assert items[0].source == "curseforge"
    assert items[0].urls == []
    assert "temporarily disabled" in items[0].error
    assert notes


def test_curseforge_pack_index_parses_locally_without_resolving_files(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    monkeypatch.setattr(packs, "_opener", lambda: pytest.fail("CurseForge API opener created"))
    index = tmp_path / "manifest.json"
    index.write_text(json.dumps(dict(manifest(), manifestType="minecraftModpack")))
    items, notes = packs.build_items(index)
    assert len(items) == 1
    assert items[0].urls == []
    assert "temporarily disabled" in items[0].error
    assert notes


def test_pack_downloader_blocks_stale_curseforge_urls_before_network(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    item = packs.DownloadItem(
        "x.jar", "mods/x.jar", ["https://edge.forgecdn.net/x.jar"],
        source="curseforge", project_id=123, file_id=456,
    )
    (tmp_path / "mods").mkdir()
    with pytest.raises(Exception, match="temporarily disabled"):
        packs._download_one(
            item, tmp_path / "mods/x.jar",
            SimpleNamespace(open=lambda *args, **kwargs: pytest.fail("network reached")),
            1, None, None,
        )
    assert not (tmp_path / "mods/x.jar").exists()
    assert list((tmp_path / "mods").iterdir()) == []


def test_pack_import_records_blocked_curseforge_files_as_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    monkeypatch.setattr(packs, "_opener", lambda: pytest.fail("CurseForge API opener created"))
    (tmp_path / "manifest.json").write_text(
        json.dumps(dict(manifest(), manifestType="minecraftModpack"))
    )
    result = packs.download_pack_mods(tmp_path)
    assert not result.downloaded
    assert len(result.failed) == 1
    assert "temporarily disabled" in result.failed[0][1]
