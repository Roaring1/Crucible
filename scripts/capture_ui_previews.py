#!/usr/bin/env python3
"""Capture repeatable Crucible UI previews using Qt's offscreen renderer.

This is a visual QA helper, not the installed app launcher. It uses a temporary
empty registry, disables network lookups for preview-only screens, and writes
PNGs to a chosen directory. It never opens desktop windows or reads/changes a
real server registry.

Examples:
    python scripts/capture_ui_previews.py
    python scripts/capture_ui_previews.py --output /tmp/crucible-previews
    python scripts/prepare_ui_evidence.py ~/.cache/crucible/ui-previews

The default output is ~/.cache/crucible/ui-previews.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def capture_previews(output: Path) -> list[Path]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtCore import QCoreApplication, QEvent, QPoint
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QMenu
    from crucible.data.instance_manager import InstanceManager
    from crucible.ui import theme
    from crucible.ui.add_dialog import AddInstanceDialog
    from crucible.ui.main_window import MainWindow
    from crucible.ui.modpack_dialog import ModpackDialog
    from crucible.ui.new_server_dialog import NewServerDialog

    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(theme.STYLESHEET)

    generated: list[Path] = []
    temporary = tempfile.TemporaryDirectory(prefix="crucible-preview-registry-")
    root = Path(temporary.name)
    manager = InstanceManager(root / "registry")
    manager.load()

    def save(widget, filename: str, width: int | None = None, height: int | None = None) -> None:
        if width is None or height is None:
            # Preserve Qt's real initial size for dialogs without an explicit
            # runtime size, rather than stretching controls across empty space.
            widget.adjustSize()
        else:
            widget.resize(width, height)
        widget.show()
        QTest.qWait(40)
        app.processEvents()
        pixmap = widget.grab()
        if pixmap.isNull():
            raise RuntimeError(f"Qt returned an empty image for {filename}")
        target = output / filename
        if not pixmap.save(str(target), "PNG"):
            raise OSError(f"Could not save UI preview: {target}")
        generated.append(target)

    try:
        # Avoid any recovery/startup action in this screenshot harness.
        with patch.object(MainWindow, "_run_crash_recovery", lambda self: None):
            main = MainWindow(manager)
            save(main, "main-empty.png", 1200, 760)

            # Capture the first action menu as a menu, not as another dialog.
            # This is the entry point most new users see, so its three choices
            # should be clear without needing to open any of the workflows.
            def capture_add_menu(menu, *_args) -> None:
                anchor = main._sidebar._add_btn
                menu.popup(anchor.mapToGlobal(QPoint(0, anchor.height())))
                QTest.qWait(40)
                app.processEvents()
                target = output / "add-server-menu.png"
                pixmap = menu.grab()
                if pixmap.isNull() or not pixmap.save(str(target), "PNG"):
                    raise RuntimeError(f"Could not capture Add server menu: {target}")
                generated.append(target)
                menu.hide()

            with patch.object(QMenu, "exec", capture_add_menu):
                main._on_add_requested()

            main.close()
            QTest.qWait(80)
            main.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()

        # A fixed preview value avoids network calls and keeps the screenshots
        # deterministic. The screen clearly marks that lookup is disabled.
        def preview_versions(dialog) -> None:
            dialog._ver_combo.clear()
            dialog._ver_combo.addItem("1.21.1")
            dialog._ver_combo.setCurrentIndex(0)
            dialog._ver_status.setText("Preview only · live version lookup disabled")
            dialog._refresh_btn.setEnabled(True)
            dialog._snap_check.setEnabled(True)

        with patch.object(NewServerDialog, "_reload_versions", preview_versions):
            create = NewServerDialog(manager)
            create._path_edit.setText("/home/demo/CrucibleServers/My-Server")
            save(create, "create-server.png")
            create.close()
            QTest.qWait(20)
            create.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()

        import_dialog = AddInstanceDialog(manager)
        save(import_dialog, "import-files.png", 740, 420)
        import_dialog.close()
        QTest.qWait(20)
        import_dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

        def no_network_search(dialog, initial: bool = False) -> None:
            dialog._status.setText("Preview only · modpack search disabled")
            dialog._search_btn.setEnabled(True)

        with patch.object(ModpackDialog, "_do_search", no_network_search):
            pack = ModpackDialog(manager)
            save(pack, "install-modpack.png", 900, 600)
            pack.close()
            QTest.qWait(20)
            pack.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()

        print(f"Captured {len(generated)} UI previews in {output}")
        for path in generated:
            print(f"{path} ({path.stat().st_size:,} bytes)")
        return generated
    finally:
        # Close any remaining Qt widget before discarding the temporary registry.
        for widget in app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        temporary.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "crucible"
        / "ui-previews",
        help="directory for generated PNGs (default: ~/.cache/crucible/ui-previews)",
    )
    args = parser.parse_args(argv)
    try:
        capture_previews(args.output)
    except (OSError, RuntimeError) as exc:
        print(f"UI preview capture failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
