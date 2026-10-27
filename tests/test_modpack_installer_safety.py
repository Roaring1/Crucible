"""Safety tests for fresh Modrinth server destinations."""
from types import SimpleNamespace

from crucible.importers import modpack_auto, serverloader


def test_strict_modrinth_install_refuses_existing_target_before_resolution(
    tmp_path, monkeypatch
):
    target = tmp_path / "existing-server"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("do not overwrite", encoding="utf-8")

    def unexpected_resolve(*args, **kwargs):
        raise AssertionError("must reject the target before contacting Modrinth")

    monkeypatch.setattr(modpack_auto.modrinth, "resolve_modpack", unexpected_resolve)
    result = modpack_auto.install_modpack_from_modrinth(
        "example-pack",
        target,
        target_must_not_exist=True,
    )

    assert not result.ok
    assert "already exists" in result.failed_reason
    assert sentinel.read_text(encoding="utf-8") == "do not overwrite"


def test_strict_modrinth_install_atomically_rejects_target_created_during_resolve(
    tmp_path, monkeypatch
):
    target = tmp_path / "raced-server"
    sentinel = target / "created-by-other-process.txt"
    downloaded = []

    def resolve_and_race(*args, **kwargs):
        target.mkdir()
        sentinel.write_text("external content", encoding="utf-8")
        return SimpleNamespace(
            filename="example.mrpack",
            version_number="1.0.0",
            url="https://example.invalid/example.mrpack",
        )

    monkeypatch.setattr(modpack_auto.modrinth, "resolve_modpack", resolve_and_race)
    monkeypatch.setattr(
        modpack_auto.sl,
        "_download_to",
        lambda *args, **kwargs: downloaded.append((args, kwargs)),
    )

    result = modpack_auto.install_modpack_from_modrinth(
        "example-pack",
        target,
        target_must_not_exist=True,
    )

    assert not result.ok
    assert "appeared during install" in result.failed_reason
    assert downloaded == []
    assert sentinel.read_text(encoding="utf-8") == "external content"


def test_non_strict_modrinth_mode_keeps_existing_cli_contract(tmp_path, monkeypatch):
    """CLI callers opt into legacy behavior unless they request strict target mode."""
    target = tmp_path / "existing-cli-target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("existing", encoding="utf-8")
    monkeypatch.setattr(
        modpack_auto.modrinth,
        "resolve_modpack",
        lambda *args, **kwargs: SimpleNamespace(
            filename="example.mrpack",
            version_number="1.0.0",
            url="https://example.invalid/example.mrpack",
        ),
    )
    monkeypatch.setattr(modpack_auto.sl, "_download_to", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        modpack_auto,
        "install_modpack_from_mrpack",
        lambda *args, **kwargs: SimpleNamespace(
            path=str(target), ok=True, cancelled=False, failed_reason=""
        ),
    )

    result = modpack_auto.install_modpack_from_modrinth("example-pack", target)

    assert result.ok
    assert sentinel.read_text(encoding="utf-8") == "existing"


def test_fresh_server_creator_refuses_leaf_symlink_target(tmp_path, monkeypatch):
    real_target = tmp_path / "real-server-target"
    real_target.mkdir()
    sentinel = real_target / "keep.txt"
    sentinel.write_text("external content", encoding="utf-8")
    link = tmp_path / "linked-server"
    link.symlink_to(real_target, target_is_directory=True)

    def unexpected_install(*args, **kwargs):
        raise AssertionError("must reject the symlink before running an installer")

    monkeypatch.setattr(serverloader, "install_server_loader", unexpected_install)
    result = serverloader.create_fresh_server(
        link,
        minecraft_version="1.21.1",
        loader="vanilla",
    )

    assert not result.ok
    assert "symbolic link" in result.failed_reason
    assert sentinel.read_text(encoding="utf-8") == "external content"
    assert not (real_target / "start.sh").exists()
