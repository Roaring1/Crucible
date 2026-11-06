from __future__ import annotations

from pathlib import Path

from crucible import __main__ as cli


def test_notification_fallback_logs_when_desktop_services_are_missing(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    cli._shortcut_notification("Shortcut failed", "The server is no longer registered.", error=True)

    log = tmp_path / ".local" / "state" / "crucible" / "shortcut-actions.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "Shortcut failed: The server is no longer registered." in lines[0]
    assert "Shortcut failed:" in capsys.readouterr().err


def test_missing_server_shortcut_is_reported_without_touching_tmux(monkeypatch) -> None:
    import pytest

    class MissingManager:
        def get_by_name_or_id(self, _key):
            return None

    class NeverCalledTmux:
        def get_status(self, _instance):
            raise AssertionError("tmux must not be queried for an unregistered server")

    notices = []
    monkeypatch.setattr(
        cli,
        "_shortcut_notification",
        lambda *args, **kwargs: notices.append((args, kwargs)),
    )

    with pytest.raises(SystemExit) as exc:
        cli.cmd_shortcut_action(
            MissingManager(),
            NeverCalledTmux(),
            type("Args", (), {"action": "start", "instance_id": "removed-id"})(),
        )

    assert exc.value.code == 1
    assert notices
    assert "no longer registered" in notices[0][0][1]
