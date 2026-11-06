from __future__ import annotations

import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from crucible import desktop_shortcuts as shortcuts


def _instance(**overrides):
    values = {
        "id": "7e2c23fd-9e9e-4fac-a8dc-5c1e6f6714c1",
        "name": 'A "quoted" server; $HOME `tick`',
        "path": "/tmp/synthetic-server",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_create_shortcuts_both_locations_uses_stable_id_and_no_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "xdg" / "applications"
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", applications)
    desktop = tmp_path / "Desktop"
    instance = _instance()

    result = shortcuts.create_shortcuts(
        instance,
        destination="both",
        desktop_directory=desktop,
    )

    assert len(result.created) == 4
    assert all(path.is_file() for path in result.created)
    assert all(path.stat().st_mode & 0o777 == 0o755 for path in result.created)
    start_files = [path for path in result.created if path.name.endswith("-start.desktop")]
    stop_files = [path for path in result.created if path.name.endswith("-stop.desktop")]
    assert len(start_files) == len(stop_files) == 2

    start_text = start_files[0].read_text(encoding="utf-8")
    stop_text = stop_files[0].read_text(encoding="utf-8")
    assert f'"shortcut-action" "start" "{instance.id}"' in start_text
    assert f'"shortcut-action" "stop" "{instance.id}"' in stop_text
    assert "Terminal=false" in start_text
    assert "Terminal=false" in stop_text
    assert "Name=Crucible: Start " in start_text
    assert "Name=Crucible: Stop " in stop_text
    assert "Exec=" in start_text and "Exec=" in stop_text
    assert "X-Crucible-Instance=" + instance.id in start_text
    assert "X-Crucible-Instance=" + instance.id in stop_text


def test_update_after_rename_keeps_same_shortcut_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", tmp_path / "apps")
    desktop = tmp_path / "Desktop"
    instance = _instance(name="Original name")

    first = shortcuts.create_shortcuts(
        instance, destination="desktop", desktop_directory=desktop
    )
    old_paths = set(first.created)
    instance.name = "New display name"
    second = shortcuts.create_shortcuts(
        instance, destination="desktop", desktop_directory=desktop
    )

    assert set(second.created) == old_paths
    assert all(
        "New display name" in path.read_text(encoding="utf-8")
        for path in second.created
    )
    assert all(
        instance.id in path.read_text(encoding="utf-8")
        for path in second.created
    )


def test_create_selected_shortcut_action_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", tmp_path / "apps")
    result = shortcuts.create_shortcuts(
        _instance(),
        destination="desktop",
        desktop_directory=tmp_path / "Desktop",
        include_start=False,
        include_stop=True,
    )
    assert len(result.created) == 1
    assert result.created[0].name.endswith("-stop.desktop")


def test_remove_only_exact_generated_files_for_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    applications = tmp_path / "apps"
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", applications)
    desktop = tmp_path / "Desktop"
    instance = _instance()
    made = shortcuts.create_shortcuts(
        instance, destination="both", desktop_directory=desktop
    )
    unrelated = desktop / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    removed = shortcuts.remove_shortcuts(
        instance.id,
        desktop_directories=(desktop,),
        include_applications=True,
    )

    assert set(removed.removed) == set(made.created)
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not any(path.exists() for path in made.created)


def test_create_refuses_to_overwrite_unrelated_file_with_generated_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", tmp_path / "apps")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    instance = _instance()
    target = desktop / shortcuts.shortcut_filename(instance.id, "start")
    target.write_text("[Desktop Entry]\\nName=My unrelated launcher\\n", encoding="utf-8")

    with pytest.raises(shortcuts.ShortcutError, match="not this Crucible start shortcut"):
        shortcuts.create_shortcuts(
            instance, destination="desktop", desktop_directory=desktop
        )

    assert "My unrelated launcher" in target.read_text(encoding="utf-8")


def test_remove_refuses_unrelated_file_with_generated_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", tmp_path / "apps")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    instance = _instance()
    target = desktop / shortcuts.shortcut_filename(instance.id, "stop")
    target.write_text("[Desktop Entry]\\nName=My unrelated launcher\\n", encoding="utf-8")

    with pytest.raises(shortcuts.ShortcutError, match="not this Crucible stop shortcut"):
        shortcuts.remove_shortcuts(
            instance.id,
            desktop_directories=(desktop,),
            include_applications=False,
        )

    assert "My unrelated launcher" in target.read_text(encoding="utf-8")


def test_refuse_symlink_destination_and_symlink_shortcut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", tmp_path / "apps")
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "Desktop"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(shortcuts.ShortcutError, match="symbolic-link"):
        shortcuts.create_shortcuts(
            _instance(), destination="desktop", desktop_directory=link
        )

    desktop = tmp_path / "safe"
    created = shortcuts.create_shortcuts(
        _instance(), destination="desktop", desktop_directory=desktop
    )
    shortcut = created.created[0]
    shortcut.unlink()
    shortcut.symlink_to(target / "not-a-shortcut")
    with pytest.raises(
        shortcuts.ShortcutError, match="symbolic-link or non-regular shortcut"
    ):
        shortcuts.remove_shortcuts(
            _instance().id,
            desktop_directories=(desktop,),
            include_applications=False,
        )


def test_invalid_destination_and_no_action_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="destination"):
        shortcuts.create_shortcuts(
            _instance(), destination="everywhere", desktop_directory=tmp_path
        )
    with pytest.raises(shortcuts.ShortcutError, match="at least one"):
        shortcuts.create_shortcuts(
            _instance(),
            destination="desktop",
            desktop_directory=tmp_path,
            include_start=False,
            include_stop=False,
        )
    with pytest.raises(shortcuts.ShortcutError, match="absolute"):
        shortcuts.create_shortcuts(
            _instance(), destination="desktop", desktop_directory="relative/path"
        )


def test_exec_uses_venv_interpreter_and_explicit_package_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", tmp_path / "apps")
    venv_python = tmp_path / "venv with spaces" / "bin" / "python"
    monkeypatch.setattr(shortcuts.sys, "executable", str(venv_python))

    result = shortcuts.create_shortcuts(
        _instance(),
        destination="desktop",
        desktop_directory=tmp_path / "Desktop",
    )
    entry = result.created[0].read_text(encoding="utf-8")
    exec_line = next(
        line.removeprefix("Exec=")
        for line in entry.splitlines()
        if line.startswith("Exec=")
    )
    argv = shlex.split(exec_line)

    assert argv[1] == f"PYTHONPATH={shortcuts._module_root()}"
    assert argv[2] == str(venv_python)
    assert argv[3:] == [
        "-m",
        "crucible",
        "shortcut-action",
        "start",
        _instance().id,
    ]


def test_shortcut_name_rejects_desktop_entry_line_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shortcuts, "APPLICATIONS_DIR", tmp_path / "apps")
    with pytest.raises(shortcuts.ShortcutError, match="control character"):
        shortcuts.create_shortcuts(
            _instance(name="Server\nExec=touch /tmp/unwanted"),
            destination="desktop",
            desktop_directory=tmp_path / "Desktop",
        )
