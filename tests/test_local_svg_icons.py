"""Tests for the restrained, locally bundled SVG icon family."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[1]
ICON_ROOT = ROOT / "crucible" / "assets" / "icons"
UPSTREAM_COMMIT = "81875c8c42d2320fa5123b37fee3554f4ca2bddc"
EXPECTED = {
    "folder-open": {
        "file": "folder-open.svg",
        "blob": "b01f60fcb51848a9b3a7380d4566c4de7534745c",
        "sha256": "ee57452b40e71df310214091dd9da1941a397c83197b8ff8d7ae66dd4242578b",
        "color": "#c8903a",
    },
    "archive": {
        "file": "archive.svg",
        "blob": "4c5ae8e33b565fa5edc5ea5fba50a860c4b9abcf",
        "sha256": "78c732e34e232db63af06a2330b4448045cfdf49b48b59f2d31922df54c7bb17",
        "color": "#111113",
    },
    "refresh-cw": {
        "file": "refresh-cw.svg",
        "blob": "c27b3a9677e35fb2353167b01553d3d4cc4e3091",
        "sha256": "2e10dd403c85a24f163d59fc6151aa21147fe9402e1305dfc8979208caee8944",
        "color": "#c8903a",
    },
}

_QT_DRIVER = r"""
import json
import sys
from pathlib import Path

from PyQt6.QtCore import QSize
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QPushButton

from crucible.ui import theme
from crucible.ui.icons import local_icon
from crucible.ui.tabs.backup_tab import BackupTab
from crucible.ui.tabs.config_tab import ConfigTab

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
app = QApplication([])
app.setStyleSheet(theme.STYLESHEET)

icon_results = {}
expected_colors = {
    "folder-open": "#c8903a",
    "archive": "#111113",
    "refresh-cw": "#c8903a",
}
for name in ("folder-open", "archive", "refresh-cw"):
    icon = local_icon(name)
    if icon.isNull():
        raise AssertionError(f"{name} did not load from a local SVG")
    pixmap = icon.pixmap(QSize(24, 24))
    if pixmap.isNull():
        raise AssertionError(f"{name} did not rasterize at 24px")
    image = pixmap.toImage()
    opaque = 0
    stroke_pixels = 0
    expected_rgb = QColor(expected_colors[name]).getRgb()[:3]
    for y in range(image.height()):
        for x in range(image.width()):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() > 0:
                opaque += 1
                rgb = pixel.getRgb()[:3]
                if sum(abs(a - b) for a, b in zip(rgb, expected_rgb)) <= 5:
                    stroke_pixels += 1
    if opaque < 8 or stroke_pixels < 3:
        raise AssertionError(
            f"{name} SVG pixels look blank or lost the intended stroke color: "
            f"{opaque=}, {stroke_pixels=}, {expected_rgb=}"
        )
    icon_results[name] = {
        "is_null": icon.isNull(),
        "opaque_pixels": opaque,
        "stroke_color": expected_colors[name],
        "stroke_pixels": stroke_pixels,
    }

unknown = local_icon("../folder-open")
if not unknown.isNull():
    raise AssertionError("Icon lookup should reject names outside the allow-list")

backup = BackupTab()
backup.resize(1040, 560)
backup.show()
app.processEvents()
if backup._open_btn.text() != "Open Folder" or backup._backup_btn.text() != "Backup Now":
    raise AssertionError("Backup actions must retain clear text labels")
if backup._open_btn.icon().isNull() or backup._backup_btn.icon().isNull():
    raise AssertionError("Backup actions must load their local SVGs")
backup.grab().save(str(out / "backup-actions.png"), "PNG")

config = ConfigTab()
config.resize(1040, 560)
config.show()
app.processEvents()
reload_buttons = [button for button in config.findChildren(QPushButton) if button.text() == "Reload"]
if len(reload_buttons) != 1:
    raise AssertionError(f"expected one labeled Reload button, found {len(reload_buttons)}")
reload = reload_buttons[0]
if reload.icon().isNull():
    raise AssertionError("Reload button has no local SVG icon")
if reload.accessibleName() != "Reload server properties":
    raise AssertionError("Reload accessible name changed")
config.grab().save(str(out / "config-actions.png"), "PNG")

print(json.dumps({
    "icons": icon_results,
    "unknown_icon_is_null": unknown.isNull(),
    "backup_labels": [backup._open_btn.text(), backup._backup_btn.text()],
    "backup_icons_non_null": [not backup._open_btn.icon().isNull(), not backup._backup_btn.icon().isNull()],
    "reload_label": reload.text(),
    "reload_accessible_name": reload.accessibleName(),
    "reload_icon_non_null": not reload.icon().isNull(),
    "screenshots": [str(out / "backup-actions.png"), str(out / "config-actions.png")],
}))
backup.close()
config.close()
app.processEvents()
"""


def test_svg_assets_are_valid_local_and_pinned_to_documented_sources():
    sources = (ROOT / "ICON_SOURCES.md").read_text(encoding="utf-8")
    assert UPSTREAM_COMMIT in sources
    assert "stroke currentColor" in sources
    assert "stroke #c8903a" in sources
    assert "stroke currentColor to theme dark ink #111113" in sources

    for name, metadata in EXPECTED.items():
        path = ICON_ROOT / metadata["file"]
        assert path.is_file(), name
        root = ET.parse(path).getroot()
        assert root.tag == "{http://www.w3.org/2000/svg}svg"
        assert root.attrib["viewBox"] == "0 0 24 24"
        assert root.attrib["stroke"] == metadata["color"]
        assert root.attrib["stroke-width"] == "2"
        assert metadata["blob"] in sources
        assert metadata["sha256"] in sources
        assert hashlib.sha256(path.read_bytes()).hexdigest() in sources
        assert metadata["file"] in sources

        # Icons are intentionally passive vector files: no scripts, embedded
        # raster images, event attributes, foreignObject, or external hrefs.
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1].lower()
            assert local_name not in {"script", "image", "foreignobject"}
            assert all(not key.lower().startswith("on") for key in element.attrib)
            for key, value in element.attrib.items():
                if key.lower().endswith("href"):
                    assert not value.startswith(("http:", "https:", "//", "data:"))
        svg_text = path.read_text(encoding="utf-8").lower()
        assert "url(" not in svg_text


def test_license_is_exact_pinned_upstream_copy_and_selected_icons_are_tracked():
    license_path = ICON_ROOT / "LICENSES" / "LUCIDE-LICENSE.txt"
    assert license_path.is_file()
    license_text = license_path.read_text(encoding="utf-8")
    assert license_text.startswith("ISC License")
    assert "The MIT License (MIT)" in license_text
    assert "Cole Bemis" in license_text
    assert "Lucide Icons and Contributors" in license_text
    assert hashlib.sha256(license_path.read_bytes()).hexdigest() == (
        "b495047bd93a9b06913511076f504daba17d5bbeb3e0650f3bb53a4220329c57"
    )

    sources = (ROOT / "ICON_SOURCES.md").read_text(encoding="utf-8")
    assert "LUCIDE-LICENSE.txt" in sources
    assert "2026-10-29" in sources
    assert "folder-open" in sources and "archive" in sources and "refresh-cw" in sources


def test_real_qt_loads_icons_and_preserves_labeled_action_controls(tmp_path):
    output = tmp_path / "qt-icon-render"
    output.mkdir()
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [sys.executable, "-c", _QT_DRIVER, str(output)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    import json

    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert set(report["icons"]) == set(EXPECTED)
    assert all(not item["is_null"] and item["stroke_pixels"] >= 3 for item in report["icons"].values())
    assert report["unknown_icon_is_null"]
    assert report["backup_labels"] == ["Open Folder", "Backup Now"]
    assert report["backup_icons_non_null"] == [True, True]
    assert report["reload_label"] == "Reload"
    assert report["reload_accessible_name"] == "Reload server properties"
    assert report["reload_icon_non_null"]
    for screenshot in report["screenshots"]:
        path = Path(screenshot)
        assert path.is_file()
        assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert len(path.read_bytes()) > 1_000


def test_svg_family_and_license_are_in_built_wheel(tmp_path):
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(ROOT),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())
    required = {
        "crucible/assets/icons/folder-open.svg",
        "crucible/assets/icons/archive.svg",
        "crucible/assets/icons/refresh-cw.svg",
        "crucible/assets/icons/LICENSES/LUCIDE-LICENSE.txt",
        "crucible/ui/icons.py",
    }
    assert required <= names
