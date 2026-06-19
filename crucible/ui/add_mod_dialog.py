"""
crucible/ui/add_mod_dialog.py

"Add a mod" -- a Prism-style mod browser. Opens to the most popular mods that
are compatible with this server, shows icons + descriptions + download counts,
and a detail panel on the right. Picking a mod resolves the right file for the
server's Minecraft version + loader AND previews the dependencies that will be
pulled in automatically. One click installs everything into mods/.

All network work (search, icons, dependency resolution, downloads) runs on
worker threads so the UI never freezes.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QThread, pyqtSignal, Qt, QSize, QUrl, QTimer
from PyQt6.QtGui import QPixmap, QIcon, QDesktopServices
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QMessageBox, QSplitter, QWidget,
    QTextEdit, QFrame, QSizePolicy,
)

from ..mods import modrinth
from . import theme


# ----------------------------- workers ---------------------------------

class _SearchWorker(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, query, loader, mc):
        super().__init__()
        self._q, self._loader, self._mc = query, loader, mc

    def run(self):
        try:
            if self._q:
                hits = modrinth.search(self._q, loader=self._loader,
                                       mc_version=self._mc, limit=30)
            else:
                hits = modrinth.browse_popular(loader=self._loader,
                                               mc_version=self._mc, limit=30)
            self.done.emit(hits)
        except Exception as e:  # noqa: BLE001 - surface any failure to UI
            self.failed.emit(str(e))


class _IconWorker(QObject):
    """Fetches icon bytes for a batch of mods, one signal per icon."""
    icon = pyqtSignal(str, object)   # project_id, png/jpg bytes
    done = pyqtSignal()

    def __init__(self, jobs):
        super().__init__()
        self._jobs = list(jobs)   # [(project_id, url), ...]
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        for pid, url in self._jobs:
            if self._cancelled:
                break
            if not url:
                continue
            try:
                data = modrinth.fetch_bytes(url)
            except Exception:  # noqa: BLE001 - icons are best-effort
                continue
            if not self._cancelled:
                self.icon.emit(pid, data)
        self.done.emit()


class _ResolveWorker(QObject):
    """Resolves the file + dependencies for one project (for the preview)."""
    done = pyqtSignal(str, object)   # project_id, [ModFile, ...]
    failed = pyqtSignal(str, str)    # project_id, message

    def __init__(self, project_id, loader, mc):
        super().__init__()
        self._pid, self._loader, self._mc = project_id, loader, mc

    def run(self):
        try:
            files = modrinth.resolve_with_deps(
                self._pid, loader=self._loader, mc_version=self._mc)
            self.done.emit(self._pid, files)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self._pid, str(e))


class _InstallWorker(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, server_path, files):
        super().__init__()
        self._path, self._files = server_path, files

    def run(self):
        try:
            self.done.emit(modrinth.install_files(self._path, self._files))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ----------------------------- dialog -----------------------------------

class AddModDialog(QDialog):
    _ICON_PX = 46

    def __init__(self, instance, parent=None):
        super().__init__(parent)
        self._instance = instance
        self._hits = []                  # list[ModHit] indexed by row
        self._item_by_pid = {}           # project_id -> QListWidgetItem
        self._pixmaps = {}               # project_id -> QPixmap (cache)
        self._threads = []               # [(QThread, worker), ...]
        self._icon_worker = None
        self._sel_pid = None             # currently selected project id
        self._resolved = {}              # project_id -> [ModFile, ...]
        self.setWindowTitle("Add a mod")
        self.resize(900, 600)
        self._build_ui()
        # Kick off the popular list as soon as the dialog opens.
        QTimer.singleShot(0, lambda: self._do_search(initial=True))

    # --- server context helpers ---
    def _loader(self):
        return getattr(self._instance, "loader", "") or "vanilla"

    def _mc(self):
        return getattr(self._instance, "minecraft_version", "") or ""

    # --- UI construction ---
    def _build_ui(self):
        root = QVBoxLayout(self)

        head = QLabel(
            f"Browse mods \u00b7 Minecraft {self._mc() or '?'} \u00b7 {self._loader()}")
        head.setStyleSheet(f"font-weight:700; font-size:15px; color:{theme.TEXT};")
        root.addWidget(head)

        row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "Search mods by name \u2014 or just browse the popular ones below\u2026")
        self._search.returnPressed.connect(lambda: self._do_search())
        row.addWidget(self._search, 1)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(lambda: self._do_search())
        row.addWidget(self._search_btn)
        root.addLayout(row)

        self._status = QLabel("Powered by Modrinth \u00b7 no account needed")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        root.addWidget(self._status)

        split = QSplitter(Qt.Orientation.Horizontal)

        # left: results list
        self._results = QListWidget()
        self._results.setIconSize(QSize(self._ICON_PX, self._ICON_PX))
        self._results.setUniformItemSizes(False)
        self._results.setStyleSheet(
            f"QListWidget::item {{ padding:6px; border-bottom:1px solid {theme.SURFACE0}; }}")
        self._results.currentRowChanged.connect(self._on_sel)
        self._results.itemDoubleClicked.connect(lambda *_: self._do_install())
        split.addWidget(self._results)

        # right: detail panel
        split.addWidget(self._build_detail())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 4)
        root.addWidget(split, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        self._install_btn = QPushButton("Install")
        self._install_btn.setObjectName("PrimaryButton")
        self._install_btn.setEnabled(False)
        self._install_btn.clicked.connect(self._do_install)
        btns.addWidget(self._install_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(close_btn)
        root.addLayout(btns)

    def _build_detail(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 6, 6, 6)

        top = QHBoxLayout()
        self._d_icon = QLabel()
        self._d_icon.setFixedSize(64, 64)
        self._d_icon.setStyleSheet(
            f"background:{theme.SURFACE0}; border-radius:8px;")
        self._d_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self._d_icon)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self._d_title = QLabel("Select a mod")
        self._d_title.setWordWrap(True)
        self._d_title.setStyleSheet(
            f"font-weight:700; font-size:16px; color:{theme.TEXT};")
        self._d_author = QLabel("")
        self._d_author.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:12px;")
        title_col.addWidget(self._d_title)
        title_col.addWidget(self._d_author)
        top.addLayout(title_col, 1)
        lay.addLayout(top)

        self._d_stats = QLabel("")
        self._d_stats.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:12px;")
        lay.addWidget(self._d_stats)

        self._d_compat = QLabel("")
        self._d_compat.setWordWrap(True)
        self._d_compat.setStyleSheet("font-size:12px; font-weight:600;")
        lay.addWidget(self._d_compat)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color:{theme.SURFACE1};")
        lay.addWidget(line)

        self._d_desc = QTextEdit()
        self._d_desc.setReadOnly(True)
        self._d_desc.setStyleSheet(
            f"background:transparent; border:none; color:{theme.TEXT}; font-size:13px;")
        self._d_desc.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
        lay.addWidget(self._d_desc, 1)

        self._d_deps = QLabel("")
        self._d_deps.setWordWrap(True)
        self._d_deps.setStyleSheet(
            f"color:{theme.SUBTEXT}; font-size:12px; padding:4px 0;")
        lay.addWidget(self._d_deps)

        self._d_link = QPushButton("View on Modrinth")
        self._d_link.setObjectName("RestartButton")
        self._d_link.setEnabled(False)
        self._d_link.clicked.connect(self._open_page)
        lay.addWidget(self._d_link)

        return panel

    # --- thread plumbing ---
    def _spawn(self, worker, *quit_signals):
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        pair = (thread, worker)
        self._threads.append(pair)
        for sig in quit_signals:
            sig.connect(thread.quit)
        thread.finished.connect(
            lambda: self._threads.remove(pair) if pair in self._threads else None)
        thread.start()
        return worker

    # --- search ---
    def _do_search(self, initial: bool = False):
        q = self._search.text().strip()
        self._status.setText(
            "Loading popular mods\u2026" if not q else "Searching\u2026")
        self._search_btn.setEnabled(False)
        w = _SearchWorker(q, self._loader(), self._mc())
        self._spawn(w, w.done, w.failed)
        w.done.connect(self._on_results)
        w.failed.connect(self._on_error)

    def _on_results(self, hits):
        self._search_btn.setEnabled(True)
        self._hits = hits
        self._item_by_pid.clear()
        self._results.clear()
        if not hits:
            self._status.setText("No results \u2014 try a different name.")
            return
        self._status.setText(
            f"{len(hits)} mod(s) \u00b7 click one for details, double-click to install")
        blank = self._blank_icon()
        for h in hits:
            dl = modrinth.humanize_count(h.downloads)
            tag = h.server_label()
            second = f"{dl} downloads"
            if tag:
                second += f"  \u00b7  {tag}"
            item = QListWidgetItem(f"{h.title}\n{second}")
            item.setIcon(blank)
            self._results.addItem(item)
            self._item_by_pid[h.project_id] = item
        # fetch icons in the background
        self._start_icons(hits)

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
        if not pix.loadFromData(bytes(data)):
            return
        self._pixmaps[pid] = pix
        item = self._item_by_pid.get(pid)
        if item is not None:
            item.setIcon(QIcon(pix))
        if pid == self._sel_pid:
            self._set_detail_icon(pix)

    def _on_error(self, msg):
        self._search_btn.setEnabled(True)
        self._install_btn.setEnabled(False)
        self._status.setText("\u26a0 " + msg)

    # --- selection / detail ---
    def _on_sel(self, row):
        if row < 0 or row >= len(self._hits):
            return
        hit = self._hits[row]
        self._sel_pid = hit.project_id
        self._install_btn.setEnabled(False)
        self._d_link.setEnabled(True)

        self._d_title.setText(hit.title)
        self._d_author.setText(f"by {hit.author}" if hit.author else "")
        stats = f"\u2b07 {modrinth.humanize_count(hit.downloads)} downloads"
        if hit.follows:
            stats += f"   \u2665 {modrinth.humanize_count(hit.follows)} followers"
        if hit.categories:
            stats += "\n" + "  ".join(f"#{c}" for c in hit.categories[:6])
        self._d_stats.setText(stats)

        if hit.is_client_only:
            self._d_compat.setText(
                "\u26a0 Client-only mod \u2014 this usually has no effect on a "
                "dedicated server.")
            self._d_compat.setStyleSheet(
                f"color:{theme.ORANGE}; font-size:12px; font-weight:600;")
        elif hit.server_label():
            self._d_compat.setText("\u2713 " + hit.server_label())
            self._d_compat.setStyleSheet(
                f"color:{theme.GREEN}; font-size:12px; font-weight:600;")
        else:
            self._d_compat.setText("")

        self._d_desc.setPlainText(hit.description or "")
        self._d_deps.setText("Checking version & dependencies\u2026")

        pix = self._pixmaps.get(hit.project_id)
        if pix is not None:
            self._set_detail_icon(pix)
        else:
            self._d_icon.setPixmap(QPixmap())
            self._d_icon.setText("\u2026")

        # resolve dependencies for the preview (and to pre-stage the install)
        if hit.project_id in self._resolved:
            self._show_deps(hit.project_id, self._resolved[hit.project_id])
        else:
            w = _ResolveWorker(hit.project_id, self._loader(), self._mc())
            self._spawn(w, w.done, w.failed)
            w.done.connect(self._on_resolved)
            w.failed.connect(self._on_resolve_failed)

    def _set_detail_icon(self, pix):
        self._d_icon.setText("")
        self._d_icon.setPixmap(pix.scaled(
            64, 64, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _on_resolved(self, pid, files):
        self._resolved[pid] = files
        if pid == self._sel_pid:
            self._show_deps(pid, files)

    def _show_deps(self, pid, files):
        self._install_btn.setEnabled(True)
        if not files:
            self._d_deps.setText("")
            return
        main = files[0]
        deps = files[1:]
        txt = f"Will install: {main.filename}"
        if deps:
            names = ", ".join(d.filename for d in deps)
            txt += (f"\n+ {len(deps)} dependency(ies) automatically: {names}")
        else:
            txt += "\n(no extra dependencies needed)"
        self._d_deps.setText(txt)

    def _on_resolve_failed(self, pid, msg):
        if pid == self._sel_pid:
            self._install_btn.setEnabled(False)
            self._d_deps.setText("\u26a0 " + msg)

    def _open_page(self):
        row = self._results.currentRow()
        if 0 <= row < len(self._hits):
            QDesktopServices.openUrl(QUrl(self._hits[row].page_url()))

    # --- install ---
    def _do_install(self):
        row = self._results.currentRow()
        if row < 0 or row >= len(self._hits):
            return
        hit = self._hits[row]
        files = self._resolved.get(hit.project_id)
        if not files:
            self._status.setText("Still checking versions \u2014 try again in a moment.")
            return
        if hit.is_client_only:
            reply = QMessageBox.question(
                self, "Client-only mod",
                f"{hit.title} is marked client-only and usually does nothing on a "
                "dedicated server.\n\nInstall it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                return
        self._install_btn.setEnabled(False)
        n = len(files)
        self._status.setText(
            f"Installing {hit.title} ({n} file{'s' if n != 1 else ''})\u2026")
        w = _InstallWorker(self._instance.path, files)
        self._spawn(w, w.done, w.failed)
        w.done.connect(self._on_installed)
        w.failed.connect(self._on_error)

    def _on_installed(self, res):
        self._install_btn.setEnabled(True)
        self._status.setText("\u2713 " + res.summary())
        if res.failed:
            QMessageBox.warning(
                self, "Add a mod",
                "Some files could not be installed:\n" + "\n".join(res.failed))
        else:
            QMessageBox.information(
                self, "Add a mod",
                f"Done! {res.summary()}.\n\nRestart the server for changes to take effect.")

    # --- helpers ---
    def _blank_icon(self) -> QIcon:
        pix = QPixmap(self._ICON_PX, self._ICON_PX)
        pix.fill(Qt.GlobalColor.transparent)
        return QIcon(pix)

    def closeEvent(self, event):
        if self._icon_worker is not None:
            self._icon_worker.cancel()
        super().closeEvent(event)
