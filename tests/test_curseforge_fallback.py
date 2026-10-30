"""Regression tests for the default-off CurseForge release fallback."""
from types import SimpleNamespace

import pytest

from crucible.download_http import DownloadRequestError, make_request
from crucible.mods import curseforge as cf
from crucible.mods import modrinth as mr
from crucible.mods import versions


def _key_read_is_a_failure():
    pytest.fail("disabled CurseForge paths must not inspect the configured key")


def test_curseforge_search_stops_before_reading_key(monkeypatch):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    with pytest.raises(mr.ModrinthError, match="temporarily disabled"):
        cf.search("sodium")


def test_existing_curseforge_identity_cannot_list_versions(monkeypatch):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    with pytest.raises(mr.ModrinthError, match="temporarily disabled"):
        versions.list_versions("cf:12345", loader="fabric", mc_version="1.21.1")


def test_curseforge_api_and_cdn_requests_stop_before_key_read(monkeypatch):
    monkeypatch.setattr(cf, "get_api_key", _key_read_is_a_failure)
    for url in (
        "https://api.curseforge.com/v1/games",
        "https://edge.forgecdn.net/files/example.jar",
        "https://media.forgecdn.net/icons/example.png",
    ):
        with pytest.raises(DownloadRequestError, match="temporarily disabled"):
            make_request(url)


def test_curseforge_icon_fetch_is_blocked_before_network(monkeypatch):
    monkeypatch.setattr(
        mr.urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("CurseForge media request reached the network"),
    )
    with pytest.raises(mr.ModrinthError, match="temporarily disabled"):
        mr.fetch_bytes("https://media.forgecdn.net/icons/example.png")


def test_resolved_curseforge_file_cannot_be_installed(tmp_path, monkeypatch):
    old = tmp_path / "mods" / "existing.jar"
    old.parent.mkdir()
    old.write_bytes(b"keep-existing-file")
    cf_file = mr.ModFile(
        "cf:12345", "Example", "file-1", "1.0", "example.jar",
        "https://edge.forgecdn.net/files/example.jar", "", 1,
    )
    monkeypatch.setattr(
        mr,
        "_download",
        lambda *args, **kwargs: pytest.fail("blocked CurseForge file reached downloader"),
    )
    with pytest.raises(mr.ModrinthError, match="temporarily disabled"):
        versions.replace_files(tmp_path, "existing.jar", [cf_file])
    assert old.read_bytes() == b"keep-existing-file"
    assert not (tmp_path / ".crucible").exists()


def test_modrinth_version_lookup_remains_available(monkeypatch):
    digest = "a" * 40
    response = [{
        "project_id": "modrinth-project",
        "name": "Example",
        "id": "modrinth-file",
        "version_number": "1.0",
        "loaders": ["fabric"],
        "game_versions": ["1.21.1"],
        "files": [{
            "primary": True,
            "filename": "example.jar",
            "url": "https://cdn.modrinth.com/files/example.jar",
            "hashes": {"sha1": digest},
            "size": 1,
        }],
        "dependencies": [],
    }]
    monkeypatch.setattr(mr, "_get", lambda url: response)
    files = versions.list_versions("modrinth-project", loader="fabric", mc_version="1.21.1")
    assert [item.project_id for item in files] == ["modrinth-project"]
    assert files[0].version_id == "modrinth-file"


def test_manual_local_jar_import_remains_available(tmp_path):
    from crucible.mods.mod_manager import ModManager

    server = tmp_path / "server"
    server.mkdir()
    source = tmp_path / "manual.jar"
    source.write_bytes(b"owner-selected local jar")
    manager = ModManager(SimpleNamespace(path=str(server)))
    added = manager.add_from_file(source)
    assert added.path.is_file()
    assert added.path.read_bytes() == b"owner-selected local jar"
