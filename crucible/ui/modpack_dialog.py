"""
crucible/ui/modpack_dialog.py

One-click *server-side* modpack hosting.  Search Modrinth modpacks, pick one,
and Crucible does the rest: it downloads the right dedicated-server loader
(Fabric/Forge/NeoForge/Quilt/Vanilla), pulls every server-side mod from the
pack manifest, applies the pack's server overrides (configs, etc.), writes a
ready-to-run start script, and registers the instance.

This mirrors NewServerDialog: every bit of network/disk work runs off the UI
thread, and failures are surfaced in the log rather than crashing the app.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QObject, QThread, pyqtSignal, QSize, QUrl
from PyQt6.QtGui import QPixmap, QIcon, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QMessageBox, QCheckBox,
    QPlainTextEdit, QProgressBar,
)

from ..mods import modrinth
from ..importers import modpack_auto
from ..data.instance_manager import InstanceManager
from ..data.instance_model import ServerInstance
from .add_mod_dialog import _IconWorker  # reuse the best-effort icon fetcher
from . import theme

_ICON_PX = 46


# ------------------------------ workers --------------------------------

class _PackSearchWorker(QObject):
    done   = pyqtSignal(int, object)   # offset, hits
    failed = pyqtSignal(str)

    def __init__(self, query, offset=0, page=30):
        super().__init__()
        self._q, self._offset, self._page = query, offset, page

    def run(self) -> None:
        try:
            hits = modrinth.search_modpacks(
                self._q, limit=self._page, offset=self._offset)
            self.done.emit(self._offset, hits)
        except Exception as exc:  # noqa: BLE001 - surface to UI
            self.failed.emit(str(exc))


class _PackInstallWorker(QObject):
    log      = pyqtSignal(str)
    finished = pyqtSignal(object)   # ModpackInstallResult or None

    def __init__(self, project_id, target, accept_eula, cancel):
        super().__init__()
        self._pid = project_id
        self._target = target
        self._eula = accept_eula
        self._cancel = cancel

    def run(self) -> None:
        result = None
        try:
            result = modpack_auto.install_modpack_from_modrinth(
                self._pid,
                self._target,
                accept_eula=self._eula,
                log_cb=lambda m: self.log.emit(m),
                cancel=self._cancel,
            )
        except Exception as exc:  # last-resort guard; installer normally never raises
            try:
                self.log.emit(f"Fatal error: {exc}")
            except Exception:
                pass
        finally:
            self.finished.emit(result)


# ------------------------------ dialog ---------------------------------

class ModpackDialog(QDialog):
    """Browse Modrinth modpacks and install one as a ready-to-run server."""

    def __init__(self, manager: InstanceManager, parent=None):
        super().__init__(parent)
        self._manager = manager
        self.result_instance: ServerInstance | None = None

        self._all_hits = []
        self._item_by_pid = {}
        self._pixmaps = {}
        self._threads = []
        self._icon_worker = None
        self._query = ""
        self._offset = 0
        self._page = 30
        self._has_more = True
        self._loading_more = False

        self._cancel_event = threading.Event()
        self._ithread: QThread | None = None
        self._iworker: _PackInstallWorker | None = None
        self._busy = False
        self._close_requested = False
        self._accept_requested = False

        self.setWindowTitle("Install a modpack")
        self.setMinimumSize(900, 600)
        self.setModal(True)
        self._build_ui()
        self._do_search(initial=True)

    # --- ui ---
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(18, 16, 18, 16)

        title = QLabel("Install a server-side modpack")
        title.setStyleSheet(f"font-size:16px; font-weight:700; color:{theme.TEXT};")
        root.addWidget(title)
        sub = QLabel(
            "Pick a modpack and Crucible sets up the whole server for you \u2014 the "
            "right loader, every server-side mod, and the pack's configs. "
            "Powered by Modrinth, no account needed.")
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:12px;")
        root.addWidget(sub)

        row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "Search modpacks by name \u2014 or browse the popular ones below\u2026")
        self._search.returnPressed.connect(lambda: self._do_search())
        row.addWidget(self._search, 1)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(lambda: self._do_search())
        row.addWidget(self._search_btn)
        root.addLayout(row)

        self._status = QLabel("Loading popular modpacks\u2026")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        root.addWidget(self._status)

        self._results = QListWidget()
        self._results.setIconSize(QSize(_ICON_PX, _ICON_PX))
        self._results.setStyleSheet(
            f"QListWidget::item {{ padding:6px; border-bottom:1px solid {theme.SURFACE0}; }}")
        self._results.currentRowChanged.connect(self._on_sel)
        self._results.itemDoubleClicked.connect(lambda *_: self._install())
        self._results.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        root.addWidget(self._results, 1)

        # EULA + install row
        ctl = QHBoxLayout()
        self._eula = QCheckBox("I accept the Minecraft EULA")
        self._eula.setToolTip(
            "Required by Mojang to run any server. See https://aka.ms/MinecraftEULA")
        ctl.addWidget(self._eula)
        self._open_btn = QPushButton("Open on Modrinth")
        self._open_btn.clicked.connect(self._open_page)
        self._open_btn.setEnabled(False)
        ctl.addWidget(self._open_btn)
        ctl.addStretch()
        self._install_btn = QPushButton("Install server")
        self._install_btn.setObjectName("PrimaryButton")
        self._install_btn.setEnabled(False)
        self._install_btn.clicked.connect(self._install)
        ctl.addWidget(self._install_btn)
        root.addLayout(ctl)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)   # indeterminate
        self._progress.hide()
        root.addWidget(self._progress)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(150)
        self._log.setStyleSheet(
            f"background:{theme.MANTLE}; color:{theme.SUBTEXT}; font-family:monospace; font-size:11px;")
        self._log.hide()
        root.addWidget(self._log)

    # --- helpers ---
    def _spawn(self, worker, *quit_signals):
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        for sig in quit_signals:
            sig.connect(thread.quit)
        pair = (thread, worker)
        thread.finished.connect(lambda: self._thread_finished(pair))
        self._threads.append(pair)
        thread.start()
        return worker

    def _selected_hit(self):
        row = self._results.currentRow()
        if 0 <= row < len(self._all_hits):
            return self._all_hits[row]
        return None

    # --- search / infinite scroll ---
    def _do_search(self, initial: bool = False):
        if self._busy:
            return
        q = self._search.text().strip()
        self._query = q
        self._offset = 0
        self._has_more = True
        self._loading_more = True
        self._all_hits = []
        self._item_by_pid.clear()
        self._results.clear()
        self._install_btn.setEnabled(False)
        self._open_btn.setEnabled(False)
        self._status.setText(
            "Loading popular modpacks\u2026" if not q else "Searching\u2026")
        self._search_btn.setEnabled(False)
        w = _PackSearchWorker(q, offset=0, page=self._page)
        self._spawn(w, w.done, w.failed)
        w.done.connect(self._on_results)
        w.failed.connect(self._on_error)

    def _maybe_load_more(self, *_):
        if self._loading_more or not self._has_more or self._busy:
            return
        sb = self._results.verticalScrollBar()
        near_bottom = sb.maximum() <= 0 or sb.value() >= sb.maximum() - 4
        if not near_bottom:
            return
        self._loading_more = True
        self._status.setText("Loading more\u2026")
        w = _PackSearchWorker(self._query, offset=self._offset, page=self._page)
        self._spawn(w, w.done, w.failed)
        w.done.connect(self._on_results)
        w.failed.connect(self._on_error)

    def _on_results(self, offset, hits):
        self._search_btn.setEnabled(True)
        self._loading_more = False
        if offset != self._offset:
            return
        hits = hits or []
        known = {h.project_id for h in self._all_hits}
        new_hits = [h for h in hits if h.project_id not in known]
        for h in new_hits:
            self._all_hits.append(h)
            self._results.addItem(self._make_item(h))
        self._offset = offset + len(hits)
        self._has_more = len(hits) >= self._page
        self._update_status()
        if new_hits:
            self._start_icons(new_hits)
        self._maybe_load_more()

    def _make_item(self, h) -> QListWidgetItem:
        dl = modrinth.humanize_count(h.downloads)
        second = f"{dl} downloads"
        if h.author:
            second += f"  \u00b7  by {h.author}"
        item = QListWidgetItem(f"{h.title}\n{second}")
        pix = self._pixmaps.get(h.project_id)
        item.setIcon(QIcon(pix) if pix is not None else self._blank_icon())
        self._item_by_pid[h.project_id] = item
        return item

    def _update_status(self):
        n = len(self._all_hits)
        if n == 0:
            self._status.setText("No modpacks found \u2014 try a different name.")
            return
        msg = f"{n} modpack(s) \u00b7 click one, accept the EULA, then Install server"
        if self._has_more:
            msg += " \u00b7 scroll for more"
        self._status.setText(msg)

    def _start_icons(self, hits):
        if self._icon_worker is not None:
            self._icon_worker.cancel()
        jobs = [(h.project_id, h.icon_url) for h in hits if h.icon_url]
        if not jobs:
            return
        w = _IconWorker(jobs)
        self._icon_worker = w
        self._spawn(w, w.done)
        w.icon.connect(self._on_icon)

    def _on_icon(self, pid, data):
        pix = QPixmap()
        if not pix.loadFromData(data):
            return
        pix = pix.scaled(_ICON_PX, _ICON_PX, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        self._pixmaps[pid] = pix
        item = self._item_by_pid.get(pid)
        if item is not None:
            item.setIcon(QIcon(pix))

    def _on_error(self, msg):
        self._search_btn.setEnabled(True)
        self._loading_more = False
        self._status.setText("\u26a0 " + msg)

    def _blank_icon(self) -> QIcon:
        pix = QPixmap(_ICON_PX, _ICON_PX)
        pix.fill(Qt.GlobalColor.transparent)
        return QIcon(pix)

    # --- selection ---
    def _on_sel(self, row):
        hit = self._selected_hit()
        ok = hit is not None and not self._busy
        self._install_btn.setEnabled(ok)
        self._open_btn.setEnabled(ok)

    def _open_page(self):
        hit = self._selected_hit()
        if hit is not None:
            QDesktopServices.openUrl(QUrl(hit.page_url()))

    # --- install ---
    def _install(self):
        if self._busy:
            return
        hit = self._selected_hit()
        if hit is None:
            return
        if not self._eula.isChecked():
            QMessageBox.information(
                self, "EULA required",
                "Mojang requires you to accept the Minecraft EULA before a "
                "server can run. Tick the EULA checkbox to continue.")
            return

        suggested = str(Path.home() / "minecraft-servers" / _safe_dir(hit.title))
        target, ok = QFileDialog.getSaveFileName(
            self, "Choose a folder for this server", suggested)
        if not ok or not target:
            return

        self._busy = True
        self._cancel_event.clear()
        self._install_btn.setEnabled(False)
        self._search_btn.setEnabled(False)
        self._search.setEnabled(False)
        self._results.setEnabled(False)
        self._progress.show()
        self._log.show()
        self._log.clear()
        self._status.setText(f"Installing {hit.title}\u2026")

        self._pending = (hit, target)
        w = _PackInstallWorker(hit.project_id, target,
                               self._eula.isChecked(), self._cancel_event)
        self._ithread = QThread()
        self._iworker = w
        w.moveToThread(self._ithread)
        self._ithread.started.connect(w.run)
        w.log.connect(self._on_log)
        w.finished.connect(self._on_installed)
        w.finished.connect(self._ithread.quit)
        w.finished.connect(w.deleteLater)
        self._ithread.finished.connect(self._ithread.deleteLater)
        self._ithread.finished.connect(self._install_thread_finished)
        self._ithread.start()

    def _on_log(self, msg):
        self._log.appendPlainText(msg)

    def _on_installed(self, result):
        self._progress.hide()
        self._busy = False
        self._search.setEnabled(True)
        self._results.setEnabled(True)
        self._search_btn.setEnabled(True)
        self._install_btn.setEnabled(True)
        hit, target = getattr(self, "_pending", (None, ""))

        if result is None:
            self._status.setText("\u26a0 Install failed \u2014 see the log above.")
            QMessageBox.critical(
                self, "Install failed",
                "The modpack could not be installed. See the log for details.")
            return
        if result.cancelled:
            self._status.setText("Install cancelled.")
            return
        if not result.ok:
            self._status.setText("\u26a0 " + (result.failed_reason or "Install failed."))
            QMessageBox.critical(
                self, "Install failed",
                result.failed_reason or "The modpack could not be installed.")
            return

        name = hit.title if hit is not None else Path(target).name
        try:
            inst = self._manager.add_instance(
                result.path,
                name,
                version=result.minecraft_version,
                pack_source="modrinth_modpack",
                minecraft_version=result.minecraft_version,
                loader=result.loader,
                loader_version=result.loader_version,
            )
        except ValueError:
            # Path already registered as a server. The pack was reinstalled in
            # place; just point the user at their existing instance list.
            QMessageBox.information(
                self, "Modpack installed",
                f"{name} was reinstalled into a folder that is already in your "
                f"server list.\n\n{result.summary()}")
            self._accept_requested = True
            return
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self, "Could not register",
                f"The pack installed but the server could not be added:\n{exc}")
            return

        self.result_instance = inst
        QMessageBox.information(
            self, "Modpack installed",
            f"{name} is ready to run.\n\n{result.summary()}")
        self._accept_requested = True

    def _thread_finished(self, pair) -> None:
        if pair in self._threads:
            self._threads.remove(pair)
        self._finish_requested_close()

    def _install_thread_finished(self) -> None:
        self._ithread = None
        self._iworker = None
        self._finish_requested_close()

    def _workers_running(self) -> bool:
        background = any(thread.isRunning() for thread, _worker in self._threads)
        installing = bool(self._ithread and self._ithread.isRunning())
        return background or installing

    def _finish_requested_close(self) -> None:
        if self._workers_running():
            return
        if self._close_requested:
            super().reject()
        elif self._accept_requested:
            super().accept()

    def reject(self) -> None:
        if self._workers_running():
            self._close_requested = True
            self._cancel_event.set()
            if self._icon_worker is not None:
                self._icon_worker.cancel()
            self._status.setText(
                "Closing after the current network/install operation exits safely…"
            )
            return
        super().reject()

    def closeEvent(self, event):
        if self._workers_running():
            self.reject()
            event.ignore()
            return
        super().closeEvent(event)


def _safe_dir(name: str) -> str:
    keep = "-_ ."
    cleaned = "".join(c for c in name if c.isalnum() or c in keep).strip()
    cleaned = cleaned.replace(" ", "-")
    return cleaned or "modpack-server"
