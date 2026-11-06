from __future__ import annotations

import os
from pathlib import Path

from crucible.data.world_start_summary import inspect_world_start_state


def _make_developed_world(server_root: Path, name: str) -> Path:
    world = server_root / name
    region = world / "region"
    playerdata = world / "playerdata"
    region.mkdir(parents=True)
    playerdata.mkdir()
    (world / "level.dat").write_bytes(b"level metadata")
    (region / "r.0.0.mca").write_bytes(b"region data")
    (playerdata / "player.dat").write_bytes(b"player data")
    return world


def test_existing_world_reports_size_mtime_and_save_counts(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    world = _make_developed_world(server, "world_main")
    newest = world / "region" / "r.0.0.mca"
    os.utime(newest, (1_800_000_000, 1_800_000_000))

    summary = inspect_world_start_state(server, "world_main")

    assert summary.state == "existing"
    assert summary.world_path == world
    assert summary.size_bytes > 0
    assert summary.newest_file_mtime == 1_800_000_000
    assert summary.region_files == 1
    assert summary.player_files == 1
    assert summary.other_developed_worlds == ()
    assert not summary.requires_confirmation


def test_missing_selected_world_warns_when_another_developed_world_exists(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    _make_developed_world(server, "world_structures")

    summary = inspect_world_start_state(server, "world_fresh")

    assert summary.state == "new"
    assert summary.other_developed_worlds == ("world_structures",)
    assert summary.unexpected_empty
    assert summary.requires_confirmation


def test_existing_world_change_is_compared_to_last_started_folder(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    _make_developed_world(server, "world_structures")
    _make_developed_world(server, "world_fresh")

    summary = inspect_world_start_state(
        server, "world_fresh", previous_world_name="world_structures"
    )

    assert summary.state == "existing"
    assert summary.previous_world_name == "world_structures"
    assert summary.selection_changed
    assert summary.requires_confirmation


def test_legacy_registry_with_multiple_saves_requires_one_confirmation(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    _make_developed_world(server, "world_a")
    _make_developed_world(server, "world_b")

    summary = inspect_world_start_state(server, "world_a")

    assert summary.state == "existing"
    assert summary.previous_world_name is None
    assert summary.other_developed_worlds == ("world_b",)
    assert summary.requires_confirmation


def test_selected_world_name_cannot_escape_server_directory(tmp_path):
    server = tmp_path / "server"
    server.mkdir()

    summary = inspect_world_start_state(server, "../outside")

    assert summary.state == "unsafe"
    assert "single world-folder name" in summary.error
    assert not summary.world_path.exists()


def test_selected_world_symlink_fails_closed(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    external = tmp_path / "outside-world"
    _make_developed_world(tmp_path, external.name)
    (server / "world").symlink_to(external, target_is_directory=True)

    summary = inspect_world_start_state(server, "world")

    assert summary.state == "unavailable"
    assert "symbolic link" in summary.error


def test_detects_nested_modded_dimension_region_files(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    world = server / "world_alt"
    region = world / "dimensions" / "modid" / "moon" / "region"
    region.mkdir(parents=True)
    (world / "level.dat").write_bytes(b"level metadata")
    (region / "r.0.0.mca").write_bytes(b"modded dimension region")

    summary = inspect_world_start_state(server, "world_main")

    assert summary.state == "new"
    assert summary.other_developed_worlds == ("world_alt",)
    assert summary.requires_confirmation


def test_rejects_empty_or_path_like_world_names(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    for invalid in ("", ".", "..", "../outside", "/tmp/world", "nested/world", r"nested\world"):
        summary = inspect_world_start_state(server, invalid)
        assert summary.state == "unsafe", invalid


def test_last_started_world_marker_survives_missing_previous_folder(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    _make_developed_world(server, "world_new_target")

    summary = inspect_world_start_state(
        server, "world_new_target", previous_world_name="old_world_now_archived"
    )

    assert summary.state == "existing"
    assert summary.selection_changed
    assert summary.requires_confirmation
