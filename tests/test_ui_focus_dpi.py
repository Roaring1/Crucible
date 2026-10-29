"""Rendered Qt regressions for visible keyboard focus and high-DPI Create Server fields.

Each scale factor runs in a clean subprocess because Qt's scale factor must be set
before QApplication is constructed. The child uses synthetic data only, disables
version lookup, and writes screenshots/measurements into pytest's temporary directory.
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
import sys
from pathlib import Path
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QMenu, QPlainTextEdit, QPushButton,
    QSpinBox, QTabWidget, QTextEdit, QToolButton, QVBoxLayout, QWidget,
    QLineEdit, QListWidget, QListWidgetItem,
)

from crucible.data.instance_manager import InstanceManager
from crucible.ui import theme
from crucible.ui.new_server_dialog import NewServerDialog

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)

def qimage_has_border_color(image, rect, color, band=3, include_top=True):
    rgb = QColor(color).getRgb()[:3]
    left, top = max(0, rect.x()), max(0, rect.y())
    right = min(image.width() - 1, rect.x() + rect.width() - 1)
    bottom = min(image.height() - 1, rect.y() + rect.height() - 1)
    if right <= left or bottom <= top:
        return False
    if include_top:
        for y in range(top, min(bottom + 1, top + band)):
            for x in range(left, right + 1):
                if image.pixelColor(x, y).getRgb()[:3] == rgb:
                    return True
    for y in range(max(top, bottom - band + 1), bottom + 1):
        for x in range(left, right + 1):
            if image.pixelColor(x, y).getRgb()[:3] == rgb:
                return True
    for x in range(left, min(right + 1, left + band)):
        for y in range(top, bottom + 1):
            if image.pixelColor(x, y).getRgb()[:3] == rgb:
                return True
    for x in range(max(left, right - band + 1), right + 1):
        for y in range(top, bottom + 1):
            if image.pixelColor(x, y).getRgb()[:3] == rgb:
                return True
    return False

def capture_control(root, widget, name, expected_color, rect_getter=None, include_top=True):
    widget.clearFocus()
    app.processEvents()
    widget.setFocus(Qt.FocusReason.OtherFocusReason)
    app.processEvents()
    root.grab().save(str(out / f"focus-{name}.png"), "PNG")
    image = widget.grab().toImage()
    rect = rect_getter() if rect_getter else widget.rect()
    return {
        "name": name,
        "has_focus": widget.hasFocus(),
        "expected_ring_color": expected_color,
        "ring_visible": qimage_has_border_color(image, rect, expected_color, include_top=include_top),
        "widget_size": [widget.width(), widget.height()],
    }

# A small control surface represents the shared widgets, without starting services.
root = QWidget()
root.setWindowTitle("Crucible focus audit")
root.resize(760, 620)
layout = QVBoxLayout(root)

button = QPushButton("Standard action")
layout.addWidget(button)
primary = QPushButton("Primary action")
primary.setObjectName("PrimaryButton")
layout.addWidget(primary)
tool = QToolButton()
tool.setText("Navigation tool")
layout.addWidget(tool)
line_edit = QLineEdit()
line_edit.setPlaceholderText("Text input")
layout.addWidget(line_edit)
combo = QComboBox()
combo.addItems(["One", "Two"])
layout.addWidget(combo)
checkbox = QCheckBox("Enable option")
layout.addWidget(checkbox)
spin = QSpinBox()
spin.setRange(0, 100)
layout.addWidget(spin)
text_edit = QTextEdit()
text_edit.setPlaceholderText("Multi-line input")
text_edit.setFixedHeight(56)
layout.addWidget(text_edit)
plain_text = QPlainTextEdit()
plain_text.setPlaceholderText("Console input")
plain_text.setFixedHeight(56)
layout.addWidget(plain_text)
list_widget = QListWidget()
list_widget.addItems(["Navigation item", "Second item"])
list_widget.setFixedHeight(64)
layout.addWidget(list_widget)
tabs = QTabWidget()
tabs.addTab(QWidget(), "Setup")
tabs.addTab(QWidget(), "Console")
tabs.setFixedHeight(74)
layout.addWidget(tabs)

root.show()
app.processEvents()

focus_results = []
for name, widget, color, rect_getter in (
    ("button", button, theme.ACCENT, None),
    ("primary-button", primary, theme.CRUST, None),
    ("tool-button", tool, theme.ACCENT, None),
    ("line-edit", line_edit, theme.ACCENT, None),
    ("combo-box", combo, theme.ACCENT, None),
    ("checkbox", checkbox, theme.ACCENT, None),
    ("spin-box", spin, theme.ACCENT, None),
    ("text-edit", text_edit, theme.ACCENT, None),
    ("plain-text-edit", plain_text, theme.ACCENT, None),
    ("list-widget", list_widget, theme.ACCENT, None),
    (
        "tab-bar",
        tabs.tabBar(),
        theme.ACCENT,
        lambda: tabs.tabBar().tabRect(tabs.currentIndex()),
    ),
):
    focus_results.append(capture_control(root, widget, name, color, rect_getter, include_top=name != "tab-bar"))

# A context menu has its active action selected by the keyboard as well as by hover.
menu = QMenu(root)
action = menu.addAction("Menu action")
menu.resize(menu.sizeHint())
menu.setActiveAction(action)
menu.show()
app.processEvents()
menu_rect = menu.actionGeometry(action)
menu_image = menu.grab().toImage()
menu_focus_visible = qimage_has_border_color(menu_image, menu_rect, theme.ACCENT)
menu.grab().save(str(out / "focus-menu.png"), "PNG")
focus_results.append({
    "name": "menu-item",
    "has_focus": menu.activeAction() is action,
    "expected_ring_color": theme.ACCENT,
    "ring_visible": menu_focus_visible,
    "widget_size": [menu.width(), menu.height()],
})
menu.close()

# Build the actual dialog with the version lookup stubbed. No network or server mutation.
manager = InstanceManager(out / "synthetic-registry")
manager.load()
with patch.object(
    NewServerDialog,
    "_reload_versions",
    lambda self: self._ver_status.setText("Preview only · version lookup disabled"),
):
    dialog = NewServerDialog(manager)
    dialog.show()
    app.processEvents()
    dialog.grab().save(str(out / "create-server-after.png"), "PNG")
    dialog._name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
    app.processEvents()
    dialog.grab().save(str(out / "create-server-name-focused.png"), "PNG")

    local_focus_results = []
    for name, widget in (("advanced-toggle", dialog._advanced_toggle), ("eula-checkbox", dialog._eula_check)):
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        app.processEvents()
        visible = qimage_has_border_color(
            widget.grab().toImage(), widget.rect(), theme.ACCENT
        )
        dialog.grab().save(str(out / f"focus-{name}.png"), "PNG")
        local_focus_results.append({
            "name": name,
            "ring_visible": visible,
            "width": widget.width(),
            "size_hint_width": widget.sizeHint().width(),
            "text_width": widget.fontMetrics().horizontalAdvance(widget.text()) if hasattr(widget, "text") else 0,
            "font_height": widget.fontMetrics().height(),
        })

    dialog._loader_combo.setCurrentIndex(2)  # NeoForge exposes the loader-version field.
    dialog._advanced_toggle.setChecked(True)
    app.processEvents()
    dialog.grab().save(str(out / "create-server-advanced-after.png"), "PNG")

    fields = {}
    for name in ("_name_edit", "_path_edit", "_lver_edit"):
        field = getattr(dialog, name)
        fields[name] = {
            "height": field.height(),
            "minimum_height": field.minimumHeight(),
            "font_metrics_height": field.fontMetrics().height(),
            "required_minimum": field.fontMetrics().height() + 14,
            "fits_font_padding_and_focus_border": field.height() >= field.fontMetrics().height() + 14,
        }
    combo_metrics = {
        "height": dialog._ver_combo.height(),
        "font_metrics_height": dialog._ver_combo.fontMetrics().height(),
    }
    dialog.close()

disabled = QPushButton("Disabled action")
disabled.setEnabled(False)
summary = {
    "focus": focus_results,
    "focus_failures": [item["name"] for item in focus_results if not item["ring_visible"]],
    "local_focus": local_focus_results,
    "local_focus_failures": [item["name"] for item in local_focus_results if not item["ring_visible"]],
    "tab_still_selected": tabs.currentIndex() == 0,
    "field_metrics": fields,
    "combo_metrics": combo_metrics,
    "disabled_remains_disabled": not disabled.isEnabled(),
    "scale_factor": __import__("os").environ.get("QT_SCALE_FACTOR", "not-set"),
}
(out / "measurements.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
print(json.dumps(summary))
root.close()
app.processEvents()
"""


@pytest.mark.parametrize("scale_factor", ["1.0", "1.25", "1.5"])
def test_focus_ring_and_create_dialog_fields_at_qt_scale(tmp_path, scale_factor):
    """Render the focus states and Create Server dialog under separate Qt scale factors."""
    output = tmp_path / f"scale-{scale_factor}"
    output.mkdir()
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "QT_SCALE_FACTOR": scale_factor,
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
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["focus_failures"] == [], (
        f"Missing visible focus ring at scale {scale_factor}: "
        f"{summary['focus_failures']}\n{result.stdout}\n{result.stderr}"
    )
    assert summary["disabled_remains_disabled"]
    assert summary["tab_still_selected"], "Focus styling changed the selected tab"
    assert summary["local_focus_failures"] == [], (
        f"Missing focus ring for dialog-local controls at scale {scale_factor}: "
        f"{summary['local_focus_failures']}"
    )
    advanced_toggle = next(item for item in summary["local_focus"] if item["name"] == "advanced-toggle")
    assert advanced_toggle["width"] >= advanced_toggle["size_hint_width"], (
        f"Focused advanced toggle was narrower than its size hint at scale {scale_factor}: "
        f"{advanced_toggle}"
    )
    for name, field in summary["field_metrics"].items():
        assert field["fits_font_padding_and_focus_border"], (
            f"{name} clipped metrics at scale {scale_factor}: {field}"
        )
    assert summary["combo_metrics"]["height"] >= summary["combo_metrics"]["font_metrics_height"]
    for filename in (
        "focus-button.png",
        "focus-primary-button.png",
        "focus-tool-button.png",
        "focus-line-edit.png",
        "focus-combo-box.png",
        "focus-checkbox.png",
        "focus-spin-box.png",
        "focus-text-edit.png",
        "focus-plain-text-edit.png",
        "focus-list-widget.png",
        "focus-tab-bar.png",
        "focus-menu.png",
        "focus-advanced-toggle.png",
        "focus-eula-checkbox.png",
        "create-server-after.png",
        "create-server-name-focused.png",
        "create-server-advanced-after.png",
    ):
        assert (output / filename).is_file(), f"missing rendered evidence: {filename}"
