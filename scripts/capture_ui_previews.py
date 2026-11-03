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
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def capture_previews(output: Path) -> list[Path]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, Qt
    from PyQt6.QtGui import QImage
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QMenu

    from crucible.data.fleet_backup import GenerationSummary
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

        # Render the selected-server workspace too, so the permanent tab row
        # and More menu are covered by screenshot review. This folder and
        # registry are temporary and contain no real server data.
        server_root = root / "demo-server"
        server_root.mkdir()
        (server_root / "server.properties").write_text(
            "server-port=25565\nmax-players=20\nlevel-name=preview-world\n",
            encoding="utf-8",
        )
        (server_root / "eula.txt").write_text("eula=false\n", encoding="utf-8")
        (server_root / "start.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")

        # Real local JAR metadata makes the Mods preview exercise production
        # inspection while remaining synthetic and network-free.
        mods_dir = server_root / "mods"
        mods_dir.mkdir()
        for index in range(218):
            metadata = [{
                "modid": f"preview_mod_{index:02d}",
                "name": f"Example Mod {index:02d}",
                "version": (
                    "2026.11.2-build-1234567890" if index % 5 == 0
                    else f"{1 + index % 3}.4.{index}"
                ),
                "description": "Synthetic local visual-audit fixture.",
            }]
            with zipfile.ZipFile(
                mods_dir / f"preview_mod_{index:02d}.jar", "w", zipfile.ZIP_DEFLATED
            ) as archive:
                archive.writestr("mcmod.info", json.dumps(metadata))

        world_root = server_root / "preview-world"
        (world_root / "region").mkdir(parents=True)
        (world_root / "level.dat").write_bytes(b"synthetic preview world metadata")
        instance = manager.add_instance(
            str(server_root),
            "Example Server",
            version="Minecraft 1.21.1",
            minecraft_version="1.21.1",
            loader="neoforge",
            loader_version="21.1.233",
        )

        # Several independent fixtures exercise real image sources and status
        # text. These are visual states only: no subprocess or tmux process is
        # started, queried or manipulated by the preview harness.
        icon_image = QImage(96, 96, QImage.Format.Format_ARGB32)
        icon_image.fill(0xFF427A9B)

        pack_root = root / "pack-server"
        pack_root.mkdir()
        (pack_root / "server-icon.png").parent.mkdir(parents=True, exist_ok=True)
        assert icon_image.save(str(pack_root / "server-icon.png"), "PNG")
        pack_instance = manager.add_instance(
            str(pack_root),
            "Pack icon server",
            version="Synthetic pack",
            pack_source="manual",
            minecraft_version="1.21.1",
            loader="fabric",
        )

        prism_root = root / "PrismLauncher"
        prism_instance_root = prism_root / "instances" / "Prism icon server"
        prism_instance_root.mkdir(parents=True)
        (prism_instance_root / "instance.cfg").write_text(
            "name=Prism icon server\niconKey=preview_pack\n",
            encoding="utf-8",
        )
        (prism_root / "prismlauncher.cfg").write_text(
            "IconsDir=icons\n",
            encoding="utf-8",
        )
        prism_icons = prism_root / "icons"
        prism_icons.mkdir()
        prism_icon = QImage(96, 96, QImage.Format.Format_ARGB32)
        prism_icon.fill(0xFF8B5CA8)
        assert prism_icon.save(str(prism_icons / "preview_pack.png"), "PNG")
        prism_server_root = root / "prism-server"
        prism_server_root.mkdir()
        prism_instance = manager.add_instance(
            str(prism_server_root),
            "Prism artwork",
            version="Synthetic Prism import",
            pack_source="prism_instance",
            prism_source=str(prism_instance_root),
        )

        # A missing folder and an unmanaged process are shown as explicit
        # states, rather than indistinguishable colored dots.
        missing_instance = manager.add_instance(
            str(root / "missing-server-folder"),
            "Missing files",
            version="Synthetic",
        )
        unmanaged_root = root / "unmanaged-server"
        unmanaged_root.mkdir()
        unmanaged_instance = manager.add_instance(
            str(unmanaged_root),
            "Unmanaged process",
            version="Synthetic",
        )
        tmux_missing_root = root / "tmux-missing-server"
        tmux_missing_root.mkdir()
        tmux_missing_instance = manager.add_instance(
            str(tmux_missing_root),
            "tmux unavailable",
            version="Synthetic",
        )
        unknown_root = root / "unknown-server"
        unknown_root.mkdir()
        unknown_instance = manager.add_instance(
            str(unknown_root),
            "Status unavailable",
            version="Synthetic",
        )

        from crucible.process.tmux_manager import TmuxManager
        from crucible.ui.tabs.backup_tab import BackupTab

        preview_statuses = {
            instance.id: "stopped",
            pack_instance.id: "running",
            prism_instance.id: "starting",
            missing_instance.id: "missing",
            unmanaged_instance.id: "unmanaged",
            tmux_missing_instance.id: "tmux_missing",
            unknown_instance.id: "unknown",
        }

        # The backup page normally reads the fleet generation store. For a
        # preview, provide two fake generation summaries directly so this
        # renderer never reads or writes the owner's real backup directory.
        preview_generations = [
            GenerationSummary(
                generation_id="20261102T232000000000Z-preview02",
                path=root / "preview-backups" / "generations" / "preview02",
                created_at="2026-11-02T23:20:00.000000Z",
                server_count=7,
                file_count=112,
                logical_bytes=18_400_000,
                bytes_copied=180_000,
            ),
            GenerationSummary(
                generation_id="20261102T231000000000Z-preview01",
                path=root / "preview-backups" / "generations" / "preview01",
                created_at="2026-11-02T23:10:00.000000Z",
                server_count=7,
                file_count=108,
                logical_bytes=18_100_000,
                bytes_copied=18_100_000,
            ),
        ]
        preview_backup_data = {
            "generations": preview_generations,
            "issues": [],
            "legacy_sources": [
                {
                    "path": "retired-instance-id/old-world.zip",
                    "kind": "retired_instance_world_archive",
                    "instance_id": None,
                    "instance_name": None,
                    "status": "unassigned",
                    "bytes": 5_000_000,
                    "files": 1,
                    "reason": "Retired instance ownership needs an explicit mapping.",
                },
                {
                    "path": "manual/Example-Server-before-import",
                    "kind": "manual_server_snapshot",
                    "instance_id": instance.id,
                    "instance_name": instance.name,
                    "status": "mapped_pending",
                    "bytes": 15_000_000,
                    "files": 64,
                    "reason": "Historical full-server folder is not yet in both fleet generations.",
                },
            ],
            "state": {
                "last_status": "success",
                "last_completed_at_utc": "2026-11-02T23:20:00.000000Z",
                "registered_servers": 7,
                "legacy_archive_migration": {
                    "source_archives_removed": 3,
                    "source_bytes_removed": 640_000_000,
                    "source_archives_pending": 1,
                    "source_bytes_pending": 150_000_000,
                    "cleanup_errors": [],
                },
            },
            "scheduler": {
                "active": "false",
                "enabled": "false",
                "next": "",
                "detail": "Preview only · live systemd status disabled",
            },
        }

        def preview_backup_list(tab, *_args):
            tab._render_inventory(preview_backup_data)

        with (
            patch.object(MainWindow, "_run_crash_recovery", lambda self: None),
            patch.object(BackupTab, "_run_job", preview_backup_list),
            patch.object(
                TmuxManager, "get_status",
                lambda self, server: preview_statuses.get(server.id, "stopped"),
            ),
            patch.object(TmuxManager, "tmux_available", lambda self: True),
        ):
            selected = MainWindow(manager)
            panel = selected._panel
            selected._sidebar.update_status(pack_instance.id, "running")
            selected._sidebar.update_status(prism_instance.id, "starting")
            selected._sidebar.update_status(missing_instance.id, "missing")
            selected._sidebar.update_status(unmanaged_instance.id, "unmanaged")
            selected._sidebar.update_status(tmux_missing_instance.id, "tmux_missing")
            selected._sidebar.update_status(unknown_instance.id, "unknown")
            save(selected, "main-selected.png", 1440, 900)
            save(selected, "main-selected-compact.png", 900, 600)

            # Capture the live server workspace at a larger desktop size.
            panel._tabs.setCurrentWidget(panel._console)
            save(selected, "workspace-console.png", 1440, 900)

            panel._tabs.setCurrentWidget(panel._mods)
            QTest.qWait(350)  # deterministic local JAR inspection
            save(selected, "workspace-mods.png", 1440, 900)
            save(selected, "workspace-mods-compact.png", 900, 600)

            panel._tabs.setCurrentWidget(panel._world)
            save(selected, "workspace-world.png", 1440, 900)

            panel._tabs.setCurrentWidget(panel._backup)
            panel._backup._render_inventory(preview_backup_data)
            if not panel._backup._restore_btn.isEnabled():
                raise RuntimeError(
                    "Selected-server restore action is not enabled in the synthetic Backups preview"
                )
            save(selected, "workspace-backups.png", 1440, 900)
            save(selected, "workspace-backups-compact.png", 900, 600)

            panel._tabs.setCurrentWidget(panel._info)
            save(selected, "workspace-diagnostics.png", 1440, 900)

            # Capture a real keyboard focus state so reviewers can inspect
            # the focus boundary restored by the global Qt stylesheet.
            selected.show()
            selected.activateWindow()
            selected.raise_()
            app.setActiveWindow(selected)
            QTest.qWait(30)
            panel._more_tabs_button.setFocus(Qt.FocusReason.OtherFocusReason)
            app.processEvents()
            if app.focusWidget() is not panel._more_tabs_button:
                raise RuntimeError(
                    "Could not activate More workspace tabs for the keyboard-focus preview"
                )
            save(selected, "workspace-keyboard-focus.png", 1440, 900)
            normal_focus = QImage(
                str(output / "workspace-diagnostics.png")
            )
            focused_preview = QImage(
                str(output / "workspace-keyboard-focus.png")
            )
            if normal_focus.isNull() or focused_preview.isNull():
                raise RuntimeError("Could not reload the keyboard-focus review images")
            if normal_focus == focused_preview:
                raise RuntimeError(
                    "Keyboard-focus preview is pixel-identical to Diagnostics; "
                    "the visible focus indicator was not captured"
                )

            # Right-click is now a shortcut menu; the primary actions are also
            # visible in the selected-server workspace.
            def capture_server_menu(menu, *_args) -> None:
                item = selected._sidebar._list.currentItem()
                rect = selected._sidebar._list.visualItemRect(item)
                menu.popup(selected._sidebar._list.mapToGlobal(rect.center()))
                QTest.qWait(40)
                app.processEvents()
                target = output / "server-rail-context-menu.png"
                pixmap = menu.grab()
                if pixmap.isNull() or not pixmap.save(str(target), "PNG"):
                    raise RuntimeError(f"Could not capture server menu: {target}")
                generated.append(target)
                menu.hide()

            with patch.object(QMenu, "exec", capture_server_menu):
                item = selected._sidebar._list.currentItem()
                rect = selected._sidebar._list.visualItemRect(item)
                selected._sidebar._on_context_menu(rect.center())

            selected.close()
            QTest.qWait(80)
            selected.deleteLater()
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
        import_dialog.show()
        import_dialog.activateWindow()
        app.setActiveWindow(import_dialog)
        import_dialog._import_folder_btn.setFocus(Qt.FocusReason.OtherFocusReason)
        app.processEvents()
        if app.focusWidget() is not import_dialog._import_folder_btn:
            raise RuntimeError(
                "Could not focus the primary Prism import action in the Add server preview"
            )
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
