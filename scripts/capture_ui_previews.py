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
import time
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
TESTS_ROOT = REPO_ROOT / "tests"
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from tmux_isolation import isolate_tmux

isolate_tmux()


def capture_previews(output: Path) -> list[Path]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt6.QtCore import QCoreApplication, QEvent, QPoint, QBuffer, QIODevice, Qt
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import (
        QApplication,
        QMenu,
        QMessageBox,
        QPushButton,
        QStyle,
        QStyleOptionButton,
    )

    from crucible.data.fleet_backup import GenerationSummary
    from crucible.data.instance_manager import InstanceManager
    from crucible.ui import theme
    from crucible.mods import icon_fetch
    from crucible.ui.add_dialog import AddInstanceDialog
    from crucible.ui.add_mod_dialog import AddModDialog
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

    def assert_visible_buttons_fit(widget, filename: str) -> None:
        """Fail visual capture if a visible text button is forced below Qt's styled size hint."""
        for button in widget.findChildren(QPushButton):
            if not button.isVisible() or not button.text().strip():
                continue
            option = QStyleOptionButton()
            option.initFrom(button)
            option.text = button.text()
            option.icon = button.icon()
            option.iconSize = button.iconSize()
            content = button.style().subElementRect(
                QStyle.SubElement.SE_PushButtonContents, option, button
            )
            metrics = button.fontMetrics()
            ink_bounds = metrics.tightBoundingRect(button.text())
            # horizontalAdvance is Qt's layout width; tightBoundingRect can
            # extend one pixel beyond it because of glyph side bearings.
            required_width = metrics.horizontalAdvance(button.text())
            required_height = ink_bounds.height()
            if not button.icon().isNull():
                required_width += button.iconSize().width() + 4
                required_height = max(required_height, button.iconSize().height())
            if (
                content.width() < required_width
                or content.height() < required_height
            ):
                raise RuntimeError(
                    f"Button text may clip in {filename}: {button.text()!r} "
                    f"(widget {button.width()}x{button.height()}, "
                    f"available text area {content.width()}x{content.height()}, "
                    f"required {required_width}x{required_height}, "
                    f"objectName={button.objectName()!r})"
                )

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
        assert_visible_buttons_fit(widget, filename)
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

            # Capture the exact Server Appearance QMessageBox. The preview
            # only opens the choices: it never invokes the custom file picker
            # or changes a server image.
            def capture_appearance_box(box) -> None:
                box.adjustSize()
                box.show()
                QTest.qWait(30)
                app.processEvents()
                for button in box.buttons():
                    metrics = button.fontMetrics()
                    if button.width() < metrics.horizontalAdvance(button.text()) + 32:
                        raise RuntimeError(
                            f"Server Appearance button would clip: {button.text()!r}"
                        )
                    if button.height() < metrics.height() + 12:
                        raise RuntimeError(
                            f"Server Appearance button height would clip: {button.text()!r}"
                        )
                save(box, "server-appearance-dialog.png")
                box.close()

            with patch.object(QMessageBox, "exec", capture_appearance_box):
                main._on_appearance_requested(
                    SimpleNamespace(name="Midtech", id="preview-server")
                )

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

        from crucible.data.backup_manager import BackupEntry
        from crucible.data.fleet_backup import FleetBackupManager, WorldRecoveryPoint
        from crucible.process.tmux_manager import TmuxManager
        from crucible.ui.tabs import world_tab
        from crucible.ui.tabs.world_tab import WorldTab

        preview_statuses = {
            instance.id: "stopped",
            pack_instance.id: "running",
            prism_instance.id: "starting",
            missing_instance.id: "missing",
            unmanaged_instance.id: "unmanaged",
            tmux_missing_instance.id: "tmux_missing",
            unknown_instance.id: "unknown",
        }

        # The Worlds page preview uses temporary, synthetic restore points.
        # It never reads the owner's real fleet backup directory or timer.
        preview_generations = [
            GenerationSummary(
                generation_id="20261106T000000000000Z-preview02",
                path=root / "preview-backups" / "generations" / "preview02",
                created_at="2026-11-06T00:00:00.000000Z",
                server_count=7,
                file_count=112,
                logical_bytes=18_400_000,
                bytes_copied=180_000,
                server_ids=(instance.id,),
            ),
            GenerationSummary(
                generation_id="20261105T235000000000Z-preview01",
                path=root / "preview-backups" / "generations" / "preview01",
                created_at="2026-11-05T23:50:00.000000Z",
                server_count=7,
                file_count=108,
                logical_bytes=18_100_000,
                bytes_copied=18_100_000,
                server_ids=(instance.id,),
            ),
        ]
        preview_points = {
            "preview-world": [
                WorldRecoveryPoint(
                    generation_id=preview_generations[0].generation_id,
                    created_at=preview_generations[0].created_at,
                    world_name="preview-world",
                    active=True,
                    size_bytes=540 * 1024 * 1024,
                ),
                WorldRecoveryPoint(
                    generation_id=preview_generations[1].generation_id,
                    created_at=preview_generations[1].created_at,
                    world_name="preview-world",
                    active=True,
                    size_bytes=538 * 1024 * 1024,
                ),
            ],
            "saved-world": [
                WorldRecoveryPoint(
                    generation_id=preview_generations[0].generation_id,
                    created_at=preview_generations[0].created_at,
                    world_name="saved-world",
                    active=False,
                    size_bytes=3_300_000_000,
                ),
                WorldRecoveryPoint(
                    generation_id=preview_generations[1].generation_id,
                    created_at=preview_generations[1].created_at,
                    world_name="saved-world",
                    active=False,
                    size_bytes=3_280_000_000,
                ),
            ],
            "snapshot-only-world": [
                WorldRecoveryPoint(
                    generation_id=preview_generations[0].generation_id,
                    created_at=preview_generations[0].created_at,
                    world_name="snapshot-only-world",
                    active=False,
                    size_bytes=920_000_000,
                ),
                WorldRecoveryPoint(
                    generation_id=preview_generations[1].generation_id,
                    created_at=preview_generations[1].created_at,
                    world_name="snapshot-only-world",
                    active=False,
                    size_bytes=900_000_000,
                ),
            ],
        }
        saved_world_entry = BackupEntry(
            filename="saved-world-preview.zip",
            path=root / "preview-backups" / "saved-world-preview.zip",
            size_bytes=3_300_000_000,
            created_at=datetime.fromisoformat("2026-11-05T23:50:00+00:00"),
            slot_name="Earlier expedition",
        )

        class PreviewBackupManager:
            def __init__(self, _instance):
                pass

            def list_backups(self):
                return [saved_world_entry]

            def _backup_level_name(self, _entry):
                return "saved-world"

            def list_pre_swap_dirs(self):
                return []

        def preview_recovery_list(tab, mode, **_kwargs):
            if mode != "list":
                return
            tab._recovery_generations = preview_generations
            tab._recovery_scheduler = {"active": "true", "enabled": "true"}
            tab._recovery_state_data = {
                "last_status": "success",
                "last_completed_at_utc": preview_generations[0].created_at,
            }
            tab._world_recovery_points = preview_points
            tab._render_recovery_status(tab._recovery_state_data)
            tab._refresh()

        with (
            patch.object(MainWindow, "_run_crash_recovery", lambda self: None),
            patch.object(world_tab, "BackupManager", PreviewBackupManager),
            patch.object(
                FleetBackupManager, "list_legacy_world_archives",
                lambda self, instance_id: [],
            ),
            patch.object(WorldTab, "_run_recovery_job", preview_recovery_list),
            patch.object(
                type(instance),
                "world_size_bytes",
                lambda self: 540 * 1024 * 1024,
            ),
            patch.object(
                TmuxManager, "get_status",
                lambda self, server: preview_statuses.get(server.id, "stopped"),
            ),
            patch.object(TmuxManager, "tmux_available", lambda self: True),
        ):
            selected = MainWindow(manager)
            panel = selected._panel
            # The preview driver inspects every page directly. Construct Players
            # here so its later capture block retains the normal selection path;
            # production startup leaves the page deferred until More -> Players.
            panel._ensure_tab_created(6)
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

            panel._tabs.setCurrentIndex(1)  # First-use creation mirrors selecting Mods in the UI.
            QTest.qWait(350)  # deterministic local JAR inspection
            # Use the inventory filter as the normal keyboard-focus baseline;
            # Qt otherwise initially focuses the first per-mod state button.
            selected.resize(1440, 900)
            selected.activateWindow()
            selected.raise_()
            app.setActiveWindow(selected)
            panel._mods._filter.setFocus(Qt.FocusReason.OtherFocusReason)
            app.processEvents()
            if app.focusWidget() is not panel._mods._filter:
                raise RuntimeError("Could not focus the Mods inventory filter baseline")
            save(selected, "workspace-mods.png", 1440, 900)
            save(selected, "workspace-mods-compact.png", 900, 600)

            # The per-row Enabled/Disabled control has a local stylesheet;
            # verify its own keyboard focus boundary is visibly rendered.
            mod_state_button = panel._mods._table.cellWidget(
                0, panel._mods._COL_ENABLED
            )
            if mod_state_button is None:
                raise RuntimeError(
                    "The synthetic Mods inventory did not create a state control"
                )
            selected.resize(1440, 900)
            selected.activateWindow()
            selected.raise_()
            app.setActiveWindow(selected)
            QTest.qWait(30)
            mod_state_button.setFocus(Qt.FocusReason.OtherFocusReason)
            app.processEvents()
            if app.focusWidget() is not mod_state_button:
                raise RuntimeError(
                    "Could not focus the first synthetic mod state control"
                )
            save(selected, "workspace-mod-keyboard-focus.png", 1440, 900)
            normal_mods = QImage(str(output / "workspace-mods.png"))
            focused_mods = QImage(
                str(output / "workspace-mod-keyboard-focus.png")
            )
            if normal_mods.isNull() or focused_mods.isNull():
                raise RuntimeError("Could not reload the Mods focus review images")
            if normal_mods == focused_mods:
                raise RuntimeError(
                    "Mods keyboard-focus preview is pixel-identical to the normal "
                    "inventory; the state-control focus boundary was not captured"
                )

            panel._tabs.setCurrentWidget(panel._players)
            panel._players._online = {"PreviewPlayer"}
            panel._players._refresh_online_list()
            selected.resize(1440, 900)
            selected.activateWindow()
            selected.raise_()
            app.setActiveWindow(selected)
            panel._players._whitelist_w._name_input.setFocus(
                Qt.FocusReason.OtherFocusReason
            )
            app.processEvents()
            if app.focusWidget() is not panel._players._whitelist_w._name_input:
                raise RuntimeError("Could not focus the Whitelist add-player baseline")
            save(selected, "workspace-players.png", 1440, 900)

            # The Online now list has a local stylesheet; verify its own focus
            # boundary is visible separately from the ordinary Players view.
            panel._players._online_list.setFocus(Qt.FocusReason.OtherFocusReason)
            app.processEvents()
            if app.focusWidget() is not panel._players._online_list:
                raise RuntimeError("Could not focus the Online now player list")
            save(selected, "workspace-players-keyboard-focus.png", 1440, 900)
            normal_players = QImage(str(output / "workspace-players.png"))
            focused_players = QImage(
                str(output / "workspace-players-keyboard-focus.png")
            )
            if normal_players.isNull() or focused_players.isNull():
                raise RuntimeError("Could not reload the Players focus review images")
            if normal_players == focused_players:
                raise RuntimeError(
                    "Players keyboard-focus preview is pixel-identical to the "
                    "normal Players page; the online-list focus boundary was not captured"
                )

            panel._tabs.setCurrentIndex(2)  # First-use creation mirrors selecting Worlds in the UI.
            QTest.qWait(80)
            world_page = panel._world
            if world_page._table.rowCount() != 3:
                raise RuntimeError(
                    "Unified Worlds preview did not show the active world, saved archive, and snapshot-only world"
                )
            if world_page._table.item(0, 4).text() != "2 points":
                raise RuntimeError(
                    "Active world preview is missing its two restore points"
                )
            if world_page._table.item(1, 4).text() != "2 points":
                raise RuntimeError(
                    "Saved archive preview is missing its two restore points"
                )
            if world_page._table.item(2, 0).text() != "snapshot-only-world":
                raise RuntimeError(
                    "Automatic-snapshot-only world was not added to Saved Worlds"
                )
            if world_page._table.item(2, 4).text() != "2 points":
                raise RuntimeError(
                    "Snapshot-only saved world is missing its restore history"
                )
            snapshot_actions = world_page._table.cellWidget(2, 5)
            if not any(
                button.text() == "Restore"
                for button in snapshot_actions.findChildren(QPushButton)
            ):
                raise RuntimeError(
                    "Snapshot-only saved world is missing its direct Restore action"
                )
            if world_page._backup_all_btn.text() != "Back up all servers now":
                raise RuntimeError("Unified Recovery action is missing from Worlds")
            if not world_page._restore_server_btn.isEnabled():
                raise RuntimeError("Full-server restore is unavailable for the selected synthetic server")
            save(selected, "workspace-world.png", 1440, 900)
            save(selected, "workspace-world-recovery.png", 1440, 900)
            save(selected, "workspace-world-recovery-compact.png", 900, 600)

            panel._tabs.setCurrentIndex(4)  # First-use creation mirrors selecting Diagnostics in the UI.
            panel._info._inspect_logs_btn.setFocus(Qt.FocusReason.OtherFocusReason)
            app.processEvents()
            if app.focusWidget() is not panel._info._inspect_logs_btn:
                raise RuntimeError("Could not focus the Diagnostics baseline control")
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

        # The Add Mod browser gets deterministic first-page, later-page, and
        # selected-detail screenshots. Fake icon bytes ensure this visual
        # evidence never touches provider APIs or the user's CurseForge key.
        class PreviewModHit:
            def __init__(self, project_id: str, title: str, icon_url: str, color: str):
                self.project_id = project_id
                self.title = title
                self.description = "Synthetic preview result used only for UI evidence."
                self.icon_url = icon_url
                self.downloads = 12_345
                self.follows = 456
                self.author = "Preview fixture"
                self.categories = ["technology"]
                self.client_side = "optional"
                self.server_side = "required"
                self.is_client_only = False
                self._color = color

            def server_label(self) -> str:
                return "Client & server"

        def synthetic_png(color: str) -> bytes:
            image = QImage(32, 32, QImage.Format.Format_ARGB32)
            image.fill(QColor(color))
            buffer = QBuffer()
            if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(buffer, "PNG"):
                raise RuntimeError("Could not build a synthetic mod-icon fixture")
            data = bytes(buffer.data())
            buffer.close()
            return data

        first_page = [
            PreviewModHit("preview-top-1", "Amber Toolkit", "https://cdn.modrinth.com/preview/amber.png", "#c8903a"),
            PreviewModHit("preview-top-2", "Copper Utilities", "https://cdn.modrinth.com/preview/copper.png", "#d9a048"),
            PreviewModHit("preview-missing", "No Icon Provided", "", "#545460"),
        ]
        later_page = [
            PreviewModHit("preview-later-1", "Foundry Extras", "https://cdn.modrinth.com/preview/foundry.png", "#a6e3a1"),
            PreviewModHit("preview-later-2", "Quiet Machines", "https://cdn.modrinth.com/preview/quiet.png", "#96929e"),
        ]
        preview_payloads = {
            hit.icon_url: synthetic_png(hit._color)
            for hit in first_page + later_page
        }

        def synthetic_icon_fetch(url: str, *, provider: str) -> bytes:
            if provider != "modrinth" or url not in preview_payloads:
                raise RuntimeError("Unexpected non-fixture request from Add Mod screenshot capture")
            return preview_payloads[url]

        with patch.object(AddModDialog, "_do_search", lambda *_args, **_kwargs: None):
            with patch.object(icon_fetch, "fetch_icon_bytes", synthetic_icon_fetch):
                add_mod = AddModDialog(SimpleNamespace(loader="fabric", minecraft_version="1.21.1"))
                add_mod._initial_search_timer.stop()
                add_mod._has_more = False
                add_mod._resolved = {hit.project_id: [] for hit in first_page + later_page}
                add_mod.resize(960, 640)
                add_mod.show()
                add_mod._all_hits = list(first_page)
                add_mod._rebuild_view()

                def wait_for_icons(project_ids: set[str]) -> None:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        app.processEvents()
                        if project_ids <= set(add_mod._pixmaps) and not add_mod._workers_running():
                            return
                        QTest.qWait(10)
                    missing = sorted(project_ids - set(add_mod._pixmaps))
                    raise RuntimeError(f"Add Mod icon screenshot timed out waiting for {missing}")

                wait_for_icons({hit.project_id for hit in first_page if hit.icon_url})
                save(add_mod, "add-mod-icons-first-page.png", 960, 640)

                add_mod._results.setCurrentRow(2)
                QTest.qWait(20)
                if add_mod._d_icon_status.text() != "No icon provided":
                    raise RuntimeError("Add Mod missing-icon detail state did not render deliberately")
                save(add_mod, "add-mod-icons-missing-detail.png", 960, 640)

                add_mod._all_hits.extend(later_page)
                add_mod._rebuild_view()
                wait_for_icons({hit.project_id for hit in later_page})
                add_mod._results.scrollToBottom()
                QTest.qWait(20)
                save(add_mod, "add-mod-icons-later-page.png", 960, 640)

                add_mod._results.setCurrentRow(len(add_mod._view) - 1)
                QTest.qWait(20)
                save(add_mod, "add-mod-icons-selected-detail.png", 960, 640)
                add_mod.close()
                QTest.qWait(30)
                add_mod.deleteLater()
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
