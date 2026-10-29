"""Keyboard-only regression coverage for SetupTab's minimum-window scroll behavior.

The driver uses a temporary synthetic server folder. It never activates setup
actions and does not start or change a server.
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

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QAbstractButton, QComboBox, QLineEdit, QScrollArea, QSpinBox

from crucible.data.instance_model import ServerInstance
from crucible.ui import theme
from crucible.ui.tabs.setup_tab import SetupTab

output = Path(sys.argv[1])
output.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)
root = Path(tempfile.mkdtemp(prefix="crucible-setup-scroll-"))
(root / "server.properties").write_text(
    "level-name=synthetic-world\nserver-port=25565\n", encoding="utf-8"
)
instance = ServerInstance(str(root), "Synthetic Setup Scroll")

# Make only the Java check deterministic. All server data remains in this
# temporary folder; the screen's action buttons are never activated.
with patch.object(ServerInstance, "java_info", return_value=(True, "Synthetic Java")):
    tab = SetupTab()
    tab.resize(900, 600)
    tab.load(instance)
    tab.show()
    app.processEvents()

    scroll = tab.findChild(QScrollArea)
    if scroll is None:
        raise AssertionError("SetupTab must retain its scroll area")
    content = scroll.widget()
    if content is None:
        raise AssertionError("Setup scroll area has no content widget")

    tab.setFocus(Qt.FocusReason.OtherFocusReason)
    QTest.keyClick(tab, Qt.Key.Key_Tab)
    app.processEvents()

    seen = []
    visible_focus = []
    hidden_focus = []
    scroll_positions = []
    bottom_button = next(
        (button for button in content.findChildren(QAbstractButton)
         if button.text().startswith("Update from a link or file")),
        None,
    )
    if bottom_button is None:
        raise AssertionError("Expected the bottom update action in the synthetic Setup view")

    bottom_reached = False
    for step in range(100):
        current = app.focusWidget()
        if current is None:
            break
        if current in seen:
            break
        seen.append(current)

        if content.isAncestorOf(current):
            mapped = current.mapTo(scroll.viewport(), QPoint(0, 0))
            rect = current.rect().translated(mapped)
            label = current.accessibleName()
            if hasattr(current, "text"):
                try:
                    label = label or current.text()
                except Exception:
                    pass
            label = label or current.toolTip() or current.objectName() or type(current).__name__
            record = {
                "step": step,
                "widget": type(current).__name__,
                "label": str(label)[:100],
                "visible_rect": [rect.x(), rect.y(), rect.width(), rect.height()],
                "viewport": [scroll.viewport().width(), scroll.viewport().height()],
                "fully_visible": scroll.viewport().rect().contains(rect),
                "scroll_value": scroll.verticalScrollBar().value(),
            }
            visible_focus.append(record)
            scroll_positions.append(scroll.verticalScrollBar().value())
            if not record["fully_visible"]:
                hidden_focus.append(record)
            if current is bottom_button:
                bottom_reached = True
                path = output / "setup-scroll-bottom.png"
                if not tab.grab().save(str(path), "PNG"):
                    raise AssertionError("could not capture bottom Setup state")
                break

        # Repeated Tab movement is intentionally allowed to trigger QScrollArea's
        # built-in ensure-visible behavior before the next focus rectangle is read.
        QTest.keyClick(current, Qt.Key.Key_Tab)
        app.processEvents()

    scrollbar = scroll.verticalScrollBar()
    report = {
        "window_size": [tab.width(), tab.height()],
        "content_size": [content.width(), content.height()],
        "viewport_size": [scroll.viewport().width(), scroll.viewport().height()],
        "scroll_maximum": scrollbar.maximum(),
        "scroll_positions_during_tab": scroll_positions,
        "focused_descendants": visible_focus,
        "hidden_focus": hidden_focus,
        "bottom_control_reached_by_tab": bottom_reached,
        "bottom_control_label": bottom_button.text(),
        "bottom_control_visible_after_tab": (
            scroll.viewport().rect().contains(
                bottom_button.rect().translated(bottom_button.mapTo(scroll.viewport(), QPoint(0, 0)))
            )
        ),
        "bottom_scroll_value": scrollbar.value(),
        "screenshot": str(output / "setup-scroll-bottom.png"),
    }

    # Verify the native keyboard page-scroll route works too, without clicking
    # the scrollbar or activating any Setup action.
    before_page_up = scrollbar.value()
    scroll.setFocus(Qt.FocusReason.OtherFocusReason)
    QTest.keyClick(scroll, Qt.Key.Key_PageUp)
    app.processEvents()
    report["page_up_changed_scroll"] = scrollbar.value() < before_page_up
    report["scroll_after_page_up"] = scrollbar.value()

    print(json.dumps(report))
    tab.close()
    app.processEvents()

shutil.rmtree(root, ignore_errors=True)
"""


def test_setup_tab_keyboard_navigation_keeps_focused_controls_visible(tmp_path):
    output = tmp_path / "setup-scroll"
    output.mkdir()
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER, str(output)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])

    assert report["window_size"] == [900, 600]
    assert report["scroll_maximum"] > 0
    assert report["bottom_control_reached_by_tab"]
    assert report["bottom_control_visible_after_tab"]
    assert report["bottom_scroll_value"] == report["scroll_maximum"]
    assert report["hidden_focus"] == []
    assert report["page_up_changed_scroll"]
    assert any("Save memory settings" in row["label"] for row in report["focused_descendants"])
    assert any("Check for updates" in row["label"] for row in report["focused_descendants"])

    screenshot = Path(report["screenshot"])
    assert screenshot.is_file()
    assert screenshot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert screenshot.stat().st_size > 1_000
