"""Responsive Setup layout regressions at standard and small window sizes.

The driver uses a synthetic server folder and never activates a Setup action.
It guards horizontal fit separately from vertical keyboard scrolling.
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

from PyQt6.QtCore import QEvent, QPoint, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QAbstractButton, QComboBox, QScrollArea, QSpinBox, QWidget

from crucible.data.instance_model import ServerInstance
from crucible.ui import theme
from crucible.ui.tabs.setup_tab import SetupTab

scenario, width, height, screenshot = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
screenshot.parent.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)
root = Path(tempfile.mkdtemp(prefix="crucible-setup-responsive-"))
(root / "server.properties").write_text(
    "level-name=synthetic-world\nserver-port=25565\n", encoding="utf-8"
)
instance = ServerInstance(str(root), "Synthetic Setup Responsive")

# Deterministic, local readiness fixtures ensure the narrow card has several
# fix buttons and wrapped details. No checks or actions need the network.
instance.readiness = lambda: [
    {
        "key": "eula", "label": "Accept the Minecraft EULA", "ok": False,
        "detail": "The Minecraft EULA has not been accepted for this synthetic server. Accept it before starting.",
        "fix": "accept_eula",
    },
    {
        "key": "java", "label": "Java installation", "ok": True,
        "detail": "Synthetic Java runtime for layout regression coverage.", "fix": None,
    },
    {
        "key": "server_files", "label": "Server files", "ok": False,
        "detail": "Required server installation files are missing from this synthetic fixture.",
        "fix": "install_server",
    },
    {
        "key": "properties", "label": "Server settings", "ok": False,
        "detail": "A server property requires attention in this synthetic fixture.",
        "fix": "fix_properties",
    },
]

tab = None
with patch.object(ServerInstance, "java_info", return_value=(True, "Synthetic Java")):
    tab = SetupTab()
    tab.resize(width, height)
    tab.load(instance)
    tab.show()
    app.processEvents()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()

    scroll = tab.findChild(QScrollArea)
    if scroll is None:
        raise AssertionError("SetupTab has no scroll area")
    content = scroll.widget()
    viewport = scroll.viewport()
    if content is None:
        raise AssertionError("Setup scroll area has no content widget")

    def rect_in_view(widget):
        return widget.rect().translated(widget.mapTo(viewport, QPoint(0, 0)))

    visible_widgets = [
        w for w in content.findChildren(QWidget)
        if w.isVisible() and not w.isHidden()
    ]
    horizontal_overflow = []
    for widget in visible_widgets:
        rect = rect_in_view(widget)
        if rect.width() > 0 and (rect.left() < 0 or rect.right() >= viewport.width()):
            # Only record actual visible descendants that extend outside the
            # viewport horizontally; vertical position is irrelevant here.
            horizontal_overflow.append({
                "class": type(widget).__name__,
                "label": (
                    widget.text() if hasattr(widget, "text") else ""
                ) or widget.objectName() or type(widget).__name__,
                "x": rect.x(), "right": rect.right(), "width": rect.width(),
            })

    all_buttons = content.findChildren(QAbstractButton)
    target = next(
        (button for button in all_buttons if button.text().startswith("Update from a link or file")),
        None,
    )
    save_button = next(
        (button for button in all_buttons if button.text() == "Save memory settings"),
        None,
    )
    if target is None or save_button is None:
        raise AssertionError("Required Setup controls were not constructed")

    def traverse(reverse=False):
        if reverse:
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            QTest.keyClick(target, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
        else:
            tab.setFocus(Qt.FocusReason.OtherFocusReason)
            QTest.keyClick(tab, Qt.Key.Key_Tab)
        app.processEvents()

        visited = []
        outside_view = []
        reached_target = target.hasFocus()
        for step in range(140):
            current = app.focusWidget()
            if current is None or current in visited:
                break
            visited.append(current)
            if content.isAncestorOf(current):
                rect = rect_in_view(current)
                record = {
                    "step": step,
                    "class": type(current).__name__,
                    "label": (
                        current.accessibleName()
                        or (current.text() if hasattr(current, "text") else "")
                        or current.objectName()
                        or type(current).__name__
                    ),
                    "rect": [rect.x(), rect.y(), rect.width(), rect.height()],
                    "viewport": [viewport.width(), viewport.height()],
                    "fully_visible": viewport.rect().contains(rect),
                    "scroll_value": scroll.verticalScrollBar().value(),
                }
                if not record["fully_visible"]:
                    outside_view.append(record)
                if current is target:
                    reached_target = True
            if reverse:
                QTest.keyClick(current, Qt.Key.Key_Tab, Qt.KeyboardModifier.ShiftModifier)
            else:
                QTest.keyClick(current, Qt.Key.Key_Tab)
            app.processEvents()

        return {
            "focus_count": len(visited),
            "content_focus_count": sum(1 for item in visited if content.isAncestorOf(item)),
            "outside_view": outside_view,
            "reached_bottom_action": reached_target,
            "scroll_value_at_end": scroll.verticalScrollBar().value(),
        }

    forward = traverse(False)
    # Capture the same final state as the focused bottom action, without clicking it.
    target.setFocus(Qt.FocusReason.OtherFocusReason)
    app.processEvents()
    if not tab.grab().save(str(screenshot), "PNG"):
        raise AssertionError("Could not capture Setup window")
    bottom_rect = rect_in_view(target)
    focus_bottom = {
        "has_focus": target.hasFocus(),
        "visible": viewport.rect().contains(bottom_rect),
        "scroll_value": scroll.verticalScrollBar().value(),
        "text": target.text(),
    }

    reverse = traverse(True)

    controls = []
    for widget in all_buttons + content.findChildren(QComboBox) + content.findChildren(QSpinBox):
        if not widget.isVisible():
            continue
        rect = rect_in_view(widget)
        controls.append({
            "class": type(widget).__name__,
            "text": widget.text() if hasattr(widget, "text") else "",
            "x": rect.x(),
            "right": rect.right(),
            "width": rect.width(),
            "within_horizontal_viewport": rect.left() >= 0 and rect.right() < viewport.width(),
        })

    report = {
        "scenario": scenario,
        "window_size": [width, height],
        "content_size": [content.width(), content.height()],
        "viewport_size": [viewport.width(), viewport.height()],
        "horizontal_scroll_maximum": scroll.horizontalScrollBar().maximum(),
        "horizontal_scroll_value": scroll.horizontalScrollBar().value(),
        "vertical_scroll_maximum": scroll.verticalScrollBar().maximum(),
        "visible_widget_horizontal_overflow": horizontal_overflow,
        "controls": controls,
        "save_button_rect": [
            save_button.geometry().x(), save_button.geometry().y(),
            save_button.geometry().width(), save_button.geometry().height(),
        ],
        "forward": forward,
        "reverse": reverse,
        "bottom_focus": focus_bottom,
        "screenshot": str(screenshot),
        "actions_activated": False,
        "java_info_mocked": True,
        "readiness_synthetic": True,
    }
    print(json.dumps(report))
    tab.close()
    app.processEvents()

shutil.rmtree(root, ignore_errors=True)
"""


def _run(tmp_path: Path, width: int, height: int):
    output = tmp_path / f"setup-{width}x{height}.png"
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", _DRIVER, "responsive", str(width), str(height), str(output)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert Path(report["screenshot"]).is_file()
    assert Path(report["screenshot"]).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert Path(report["screenshot"]).stat().st_size > 1_000
    assert report["actions_activated"] is False
    assert report["java_info_mocked"] and report["readiness_synthetic"]
    return report


@pytest.mark.parametrize(("width", "height"), [(640, 420), (900, 600)])
def test_setup_content_and_controls_have_no_horizontal_overflow(tmp_path, width, height):
    report = _run(tmp_path, width, height)
    assert report["horizontal_scroll_maximum"] == 0, report
    assert report["horizontal_scroll_value"] == 0
    assert report["content_size"][0] <= report["viewport_size"][0]
    assert report["visible_widget_horizontal_overflow"] == []
    assert all(control["within_horizontal_viewport"] for control in report["controls"])
    assert report["save_button_rect"][0] >= 0


@pytest.mark.parametrize(("width", "height"), [(640, 420), (900, 600)])
def test_forward_and_reverse_tab_keep_focus_targets_visible(tmp_path, width, height):
    report = _run(tmp_path, width, height)
    assert report["forward"]["reached_bottom_action"], report["forward"]
    assert report["reverse"]["reached_bottom_action"], report["reverse"]
    assert report["forward"]["outside_view"] == [], report["forward"]["outside_view"]
    assert report["reverse"]["outside_view"] == [], report["reverse"]["outside_view"]
    assert report["bottom_focus"]["has_focus"]
    assert report["bottom_focus"]["visible"]
    assert report["vertical_scroll_maximum"] > 0
