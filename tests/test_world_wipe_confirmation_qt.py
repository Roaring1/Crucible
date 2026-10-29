"""Real PyQt regressions for the destructive World wipe confirmation.

Each scenario runs in an isolated Qt subprocess with a synthetic server folder.
For WorldTab integration cases, _start_wipe is mocked, so the actual wipe worker
is never created and no user/server data can be touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]

_DRIVER = r"""
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QWidget

from crucible.data.instance_model import ServerInstance
from crucible.ui import theme
from crucible.ui.tabs import world_tab as world_module
from crucible.ui.tabs.world_tab import WorldTab, _WipeWorldConfirmDialog

scenario = sys.argv[1]
output = Path(sys.argv[2])
parent_width, parent_height = int(sys.argv[3]), int(sys.argv[4])
output.mkdir(parents=True, exist_ok=True)

app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)
parent = QWidget()
parent.setWindowTitle("Synthetic World tab test host")
parent.resize(parent_width, parent_height)
parent.show()
app.processEvents()

tmp = Path(tempfile.mkdtemp(prefix="crucible-wipe-dialog-"))
server_dir = tmp / "synthetic-server"
server_dir.mkdir()
(server_dir / "server.properties").write_text(
    "level-name=synthetic_world\nserver-port=25565\n", encoding="utf-8"
)
world_root = server_dir / "synthetic_world"
world_root.mkdir()
marker = world_root / "SYNTHETIC_FIXTURE_DO_NOT_DELETE.txt"
marker.write_text("synthetic test data; actual wipe operation is mocked\n", encoding="utf-8")

data = {
    "scenario": scenario,
    "parent_size": [parent_width, parent_height],
    "wipe_calls": [],
}
integration = scenario.startswith("integration-")
dialog = None
active_dialogs = []

def _has_color(widget, color):
    image = widget.grab().toImage()
    expected = QColor(color).getRgb()[:3]
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).getRgb()[:3] == expected:
                return True
    return False

def _record_dialog_metrics(modal):
    data["cancel_focus_ring_visible"] = (
        modal._cancel_button.hasFocus() and _has_color(modal._cancel_button, theme.ACCENT)
    )
    data["dialog_size"] = [modal.width(), modal.height()]
    data["minimum_width"] = modal.minimumWidth()
    data["maximum_width"] = modal.maximumWidth()
    data["cancel_is_default"] = modal._cancel_button.isDefault()
    data["confirm_initially_disabled"] = not modal._confirm_button.isEnabled()

def exercise(modal, mode):
    app.processEvents()
    data["dialog_visible_when_action_started"] = modal.isVisible()
    focused = app.focusWidget()
    data["initial_focus_object"] = focused.objectName() if focused else ""
    data["initial_focus_class"] = type(focused).__name__ if focused else ""
    data["initial_focus_is_cancel"] = focused is modal._cancel_button
    _record_dialog_metrics(modal)

    if mode in ("cancel", "integration-cancel"):
        QTest.mouseClick(modal._cancel_button, Qt.MouseButton.LeftButton)
        return

    if mode in ("wrong", "integration-wrong"):
        modal._confirmation_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClicks(modal._confirmation_edit, "wipe")
        app.processEvents()
        data["wrong_text_confirm_disabled"] = not modal._confirm_button.isEnabled()
        data["wrong_text_dialog_stays_open"] = modal.isVisible()
        QTest.keyClick(modal._confirmation_edit, Qt.Key.Key_Return)
        app.processEvents()
        data["wrong_text_return_did_not_accept"] = modal.isVisible()
        QTest.keyClick(modal, Qt.Key.Key_Escape)
        return

    if mode in ("exact", "integration-exact"):
        modal._confirmation_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        QTest.keyClicks(modal._confirmation_edit, "WIPE")
        app.processEvents()
        data["exact_text_confirm_enabled"] = modal._confirm_button.isEnabled()
        QTest.mouseClick(modal._confirm_button, Qt.MouseButton.LeftButton)
        return

    if mode == "escape":
        QTest.keyClick(modal, Qt.Key.Key_Escape)
        return

    if mode == "close":
        modal.close()
        return

    if mode == "keyboard":
        # Start from the safe default. Tab to input, type the exact confirmation,
        # Tab to the now-enabled destructive button, then activate it with Space.
        data["cancel_had_initial_focus"] = focused is modal._cancel_button
        QTest.keyClick(modal._cancel_button, Qt.Key.Key_Tab)
        data["tab_one_reached_input"] = app.focusWidget() is modal._confirmation_edit
        data["input_focus_ring_visible"] = (
            modal._confirmation_edit.hasFocus()
            and _has_color(modal._confirmation_edit, theme.ACCENT)
        )
        QTest.keyClicks(modal._confirmation_edit, "WIPE")
        app.processEvents()
        QTest.keyClick(modal._confirmation_edit, Qt.Key.Key_Tab)
        data["tab_two_reached_confirm"] = app.focusWidget() is modal._confirm_button
        data["confirm_focus_ring_visible"] = (
            modal._confirm_button.hasFocus()
            and _has_color(modal._confirm_button, theme.ACCENT)
        )
        QTest.keyClick(modal._confirm_button, Qt.Key.Key_Space)
        return

    if mode.startswith("capture-"):
        stem = mode.removeprefix("capture-")
        modal.grab().save(str(output / f"wipe-confirm-{stem}.png"), "PNG")
        warning_panel = modal.findChild(QWidget, "WipeWarningPanel")
        warning_body = modal.findChild(QLabel, "WipeWarningBody")
        instruction = modal.findChild(QLabel, "WipeConfirmationInstruction")
        data["screenshot"] = str(output / f"wipe-confirm-{stem}.png")
        data["warning_panel_size"] = [warning_panel.width(), warning_panel.height()]
        data["warning_body_size"] = [warning_body.width(), warning_body.height()]
        data["warning_body_font_height"] = warning_body.fontMetrics().height()
        data["warning_body_word_wrap_enabled"] = warning_body.wordWrap()
        data["warning_instruction"] = instruction.text()
        modal.reject()
        return

    raise AssertionError("unknown scenario: " + mode)

if integration:
    tab = WorldTab(parent)
    tab.resize(parent_width, parent_height)
    tab.show()
    tab._manager = object()
    tab._instance = ServerInstance(str(server_dir), "Synthetic World")
    tab._start_wipe = lambda: data["wipe_calls"].append("called")

    def interact_active_dialog():
        modal = app.activeModalWidget()
        if modal is None or modal.objectName() != "WipeWorldConfirmDialog":
            raise AssertionError("Expected the real World wipe confirmation to be modal")
        active_dialogs.append(modal)
        exercise(modal, scenario)

    with patch.object(world_module.TmuxManager, "get_status", return_value="stopped"):
        QTimer.singleShot(0, interact_active_dialog)
        # The production guards/dialog run, but the wipe dispatch is replaced on
        # this instance; WipeWorldWorker is never created and the fixture stays.
        tab._confirm_and_wipe()
    dialog = active_dialogs[0]
else:
    dialog = _WipeWorldConfirmDialog("synthetic_world", parent)
    QTimer.singleShot(0, lambda: exercise(dialog, scenario))
    result = dialog.exec()
    data["dialog_result"] = int(result)

data["dialog_result"] = int(dialog.result())
data["accepted"] = dialog.result() == int(dialog.DialogCode.Accepted)
data["dialog_closed"] = not dialog.isVisible()
data["confirmation_text"] = dialog.confirmation_text
data["world_fixture_still_exists"] = marker.is_file()
data["world_fixture_content"] = marker.read_text(encoding="utf-8")
print(json.dumps(data))
dialog.deleteLater()
parent.close()
app.processEvents()
shutil.rmtree(tmp, ignore_errors=True)
"""


def _run(tmp_path: Path, scenario: str, width: int = 1200, height: int = 760):
    output = tmp_path / f"{scenario}-{width}x{height}"
    output.mkdir()
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER, scenario, str(output), str(width), str(height)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_cancel_is_default_safe_path(tmp_path):
    result = _run(tmp_path, "cancel")
    assert result["cancel_is_default"]
    assert result["initial_focus_is_cancel"]
    assert result["cancel_focus_ring_visible"]
    assert result["confirm_initially_disabled"]
    assert result["accepted"] is False
    assert result["dialog_closed"]
    assert result["wipe_calls"] == []


def test_incorrect_confirmation_never_accepts(tmp_path):
    result = _run(tmp_path, "wrong")
    assert result["wrong_text_confirm_disabled"]
    assert result["wrong_text_dialog_stays_open"]
    assert result["wrong_text_return_did_not_accept"]
    assert result["accepted"] is False
    assert result["dialog_closed"]
    assert result["wipe_calls"] == []


def test_exact_confirmation_enables_and_accepts(tmp_path):
    result = _run(tmp_path, "exact")
    assert result["exact_text_confirm_enabled"]
    assert result["accepted"] is True
    assert result["confirmation_text"] == "WIPE"
    assert result["dialog_closed"]


def test_escape_and_window_close_are_safe_rejections(tmp_path):
    escape = _run(tmp_path, "escape")
    close = _run(tmp_path, "close")
    assert escape["accepted"] is False
    assert escape["dialog_closed"]
    assert close["accepted"] is False
    assert close["dialog_closed"]


def test_keyboard_navigation_starts_safe_then_requires_exact_token(tmp_path):
    result = _run(tmp_path, "keyboard")
    assert result["cancel_had_initial_focus"]
    assert result["tab_one_reached_input"]
    assert result["input_focus_ring_visible"]
    assert result["tab_two_reached_confirm"]
    assert result["confirm_focus_ring_visible"]
    assert result["accepted"] is True
    assert result["confirmation_text"] == "WIPE"


@pytest.mark.parametrize(
    ("scenario", "expected_calls", "expected_accepted"),
    [
        ("integration-cancel", [], False),
        ("integration-wrong", [], False),
        ("integration-exact", ["called"], True),
    ],
)
def test_worldtab_only_dispatches_wipe_after_exact_confirmation(
    tmp_path, scenario, expected_calls, expected_accepted
):
    result = _run(tmp_path, scenario)
    assert result["wipe_calls"] == expected_calls
    assert result["accepted"] is expected_accepted
    assert result["world_fixture_still_exists"]
    assert "synthetic test data" in result["world_fixture_content"]


@pytest.mark.parametrize(
    ("scenario", "width", "height", "expected_width"),
    [
        ("capture-standard", 1200, 760, 560),
        ("capture-small", 520, 420, 472),
    ],
)
def test_captures_bounded_dialog_at_standard_and_small_window_sizes(
    tmp_path, scenario, width, height, expected_width
):
    result = _run(tmp_path, scenario, width, height)
    screenshot = Path(result["screenshot"])
    assert screenshot.is_file()
    assert result["dialog_size"][0] == expected_width
    assert result["dialog_size"][0] <= 560
    assert result["warning_body_word_wrap_enabled"]
    assert result["warning_body_size"][1] >= result["warning_body_font_height"] * 3
    assert result["warning_panel_size"][1] >= result["warning_body_size"][1]
    assert result["warning_instruction"] == "Type exactly WIPE to enable permanent deletion:"
