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

from collections import OrderedDict, deque
from threading import Lock

from PyQt6.QtCore import (
    QObject, QThread, pyqtSignal, Qt, QSize, QUrl, QTimer, QPoint,
    QByteArray, QBuffer, QIODevice,
)
from PyQt6.QtGui import (
    QPixmap, QIcon, QDesktopServices, QColor, QPainter, QPen, QImageReader,
)
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QMessageBox, QSplitter, QWidget,
    QTextEdit, QFrame, QSizePolicy, QCheckBox, QComboBox, QInputDialog,
)

from ..mods import icon_fetch, modrinth
from . import theme
from .thread_lifecycle import connect_thread_cleanup


# ----------------------------- workers ---------------------------------

class _SearchWorker(QObject):
    done = pyqtSignal(int, object)   # offset, hits
    failed = pyqtSignal(str)

    def __init__(self, query, loader, mc, offset=0, page=30, provider="Modrinth"):
        super().__init__()
        self._provider = provider
        self._q, self._loader, self._mc = query, loader, mc
        self._offset, self._page = offset, page

    def run(self):
        try:
            if self._provider == "CurseForge":
                from ..mods import curseforge
                hits = curseforge.search(self._q, loader=self._loader, mc_version=self._mc, limit=self._page, offset=self._offset)
            elif self._q:
                hits = modrinth.search(self._q, loader=self._loader,
                                       mc_version=self._mc, limit=self._page,
                                       offset=self._offset)
            else:
                hits = modrinth.browse_popular(loader=self._loader,
                                               mc_version=self._mc, limit=self._page,
                                               offset=self._offset)
            self.done.emit(self._offset, hits)
        except Exception as e:  # noqa: BLE001 - surface any failure to UI
            self.failed.emit(str(e))


_MAX_QUEUED_ICON_JOBS = 48


class _IconWorker(QObject):
    """Drain one bounded, appendable queue; visible/selected jobs can jump ahead."""
    icon = pyqtSignal(str, object)   # project_id, image bytes
    failed = pyqtSignal(str)         # project_id; never include an untrusted URL
    done = pyqtSignal()

    def __init__(self, jobs=()):
        super().__init__()
        self._jobs = deque()
        self._pending_ids = set()
        self._active_pid = None
        self._lock = Lock()
        self._cancelled = False
        self._accepting = True
        self._rejected = []
        self.add_jobs(jobs)

    def add_jobs(self, jobs, *, priority=False):
        """Queue unique jobs up to the hard cap; promote visible work when asked."""
        jobs = [(pid, url) for pid, url in jobs if pid and url]
        with self._lock:
            if self._cancelled or not self._accepting:
                self._rejected.extend(pid for pid, _url in jobs)
                return False

            unique = []
            seen = set()
            for pid, url in jobs:
                if pid not in seen:
                    unique.append((pid, url))
                    seen.add(pid)

            if priority:
                # Promote already-queued visible jobs as well as adding new
                # ones. The in-flight request is intentionally not interrupted.
                promoted = []
                for pid, url in unique:
                    if pid == self._active_pid:
                        continue
                    old = next((job for job in self._jobs if job[0] == pid), None)
                    if old is not None:
                        self._jobs.remove(old)
                        promoted.append((pid, url))
                    elif pid not in self._pending_ids:
                        promoted.append((pid, url))

                # Keep priority jobs in their supplied order, never exceeding
                # the queue cap. Oldest lowest-priority tail entries are shed.
                if len(promoted) > _MAX_QUEUED_ICON_JOBS:
                    self._rejected.extend(pid for pid, _url in promoted[_MAX_QUEUED_ICON_JOBS:])
                    promoted = promoted[:_MAX_QUEUED_ICON_JOBS]
                while len(self._jobs) + len(promoted) > _MAX_QUEUED_ICON_JOBS:
                    dropped_pid, _dropped_url = self._jobs.pop()
                    self._pending_ids.discard(dropped_pid)
                    self._rejected.append(dropped_pid)
                for pid, _url in reversed(promoted):
                    self._jobs.appendleft((pid, _url))
                    self._pending_ids.add(pid)
            else:
                for pid, url in unique:
                    if pid in self._pending_ids:
                        continue
                    if len(self._jobs) >= _MAX_QUEUED_ICON_JOBS:
                        self._rejected.append(pid)
                        continue
                    self._jobs.append((pid, url))
                    self._pending_ids.add(pid)
            return True

    def take_rejected(self):
        """Return and clear job IDs rejected by the queue capacity policy."""
        with self._lock:
            rejected, self._rejected = self._rejected, []
            return rejected

    def discard_queued(self):
        """Drop obsolete queued work on a new search, but let an active fetch finish."""
        with self._lock:
            if self._cancelled or not self._accepting:
                return []
            discarded = []
            for pid, _url in self._jobs:
                self._pending_ids.discard(pid)
                discarded.append(pid)
            self._jobs.clear()
            return discarded

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._accepting = False
            self._jobs.clear()
            self._pending_ids.clear()
            self._rejected.clear()

    def run(self):
        while True:
            with self._lock:
                if self._cancelled:
                    break
                if not self._jobs:
                    # This transition is atomic with add_jobs(): callers either
                    # append before this check, or observe a retiring worker.
                    self._accepting = False
                    break
                pid, url = self._jobs.popleft()
                self._active_pid = pid

            provider = "curseforge" if pid.startswith("cf:") else "modrinth"
            data = None
            failed = False
            try:
                data = icon_fetch.fetch_icon_bytes(url, provider=provider)
            except Exception:  # noqa: BLE001 - icon failures are best-effort
                failed = True

            with self._lock:
                self._pending_ids.discard(pid)
                self._active_pid = None
                cancelled = self._cancelled
            if not cancelled:
                if failed:
                    self.failed.emit(pid)
                else:
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
            from ..mods.versions import list_versions, resolve_selected
            options = list_versions(self._pid, loader=self._loader, mc_version=self._mc)
            if not options:
                raise modrinth.ModrinthError("No compatible version was found.")
            files = resolve_selected(options[0], loader=self._loader, mc_version=self._mc)
            self.done.emit(self._pid, files)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self._pid, str(e))


class _InstallWorker(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, server_path, files, instance=None):
        super().__init__()
        self._path, self._files = server_path, files
        self._instance = instance

    def run(self):
        try:
            from ..mods.versions import replace_files
            from ..process.tmux_manager import TmuxManager
            def guard():
                if self._instance is not None and TmuxManager().get_status(self._instance) != "stopped":
                    raise RuntimeError("Server is no longer confirmed stopped. No mod change was applied.")
            guard()
            self.done.emit(replace_files(self._path, None, self._files, before_commit=guard))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


# ----------------------------- dialog -----------------------------------

class AddModDialog(QDialog):
    _ICON_PX = 46

    def __init__(self, instance, parent=None):
        super().__init__(parent)
        self._instance = instance
        self._all_hits = []              # every ModHit fetched so far
        self._view = []                  # filtered subset shown in the list
        self._item_by_pid = {}           # project_id -> QListWidgetItem
        self._pixmaps = OrderedDict()     # bounded project_id -> small QPixmap LRU
        self._icon_states = OrderedDict()  # bounded project_id -> recent icon state
        self._placeholder_icons = {}      # state -> locally painted placeholder QIcon
        self._threads = []               # [(QThread, worker), ...]
        self._close_requested = False
        self._icon_worker = None
        self._sel_pid = None             # currently selected project id
        self._resolved = {}              # project_id -> [ModFile, ...]
        # infinite-scroll / paging state
        self._search_generation = 0
        self._query = ""                 # active query ("" = browse popular)
        self._offset = 0                 # next page offset to request
        self._page = 30                  # page size
        self._has_more = True            # whether more results may exist
        self._loading_more = False       # guard against concurrent page loads
        self.setWindowTitle("Add a mod")
        self.resize(900, 600)
        self._build_ui()
        # Parent the deferred startup to the dialog. An unowned singleShot
        # lambda can outlive a dialog that was closed before the event loop
        # runs, then start network work against deleted Qt widgets.
        self._initial_search_timer = QTimer(self)
        self._initial_search_timer.setSingleShot(True)
        self._initial_search_timer.timeout.connect(self._start_initial_search)
        self._initial_search_timer.start(0)

    def _start_initial_search(self):
        if self.isVisible() and not self._close_requested:
            self._do_search(initial=True)

    # --- server context helpers ---
    def _loader(self):
        return getattr(self._instance, "loader", "") or "vanilla"

    def _mc(self):
        return getattr(self._instance, "minecraft_version", "") or ""

    # --- UI construction ---
    def _build_ui(self):
        root = QVBoxLayout(self)

        head = QLabel(
            f"Browse mods · Minecraft {self._mc() or '?'} · {self._loader()}")
        head.setStyleSheet(f"font-weight:700; font-size:15px; color:{theme.TEXT};")
        root.addWidget(head)

        row = QHBoxLayout()
        self._provider = QComboBox()
        self._provider.addItems(["Modrinth", "CurseForge"])
        self._provider.setAccessibleName("Mod provider")
        self._provider.setMinimumWidth(110)
        self._provider.currentTextChanged.connect(lambda *_: self._do_search())
        row.addWidget(self._provider)
        key_btn = QPushButton("API key…")
        self._key_btn = key_btn
        key_btn.hide()
        key_btn.setToolTip("CurseForge API key · kept in memory for this session only")
        key_btn.clicked.connect(self._set_curseforge_key)
        row.addWidget(key_btn)
        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "Search mods by name — or just browse the popular ones below…")
        self._search.returnPressed.connect(lambda: self._do_search())
        row.addWidget(self._search, 1)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(lambda: self._do_search())
        row.addWidget(self._search_btn)
        root.addLayout(row)

        # Compatibility filter: show mods that run on the client and/or server.
        filt = QHBoxLayout()
        filt_label = QLabel("Show:")
        filt_label.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:12px;")
        filt.addWidget(filt_label)
        self._cb_client = QCheckBox("Client")
        self._cb_client.setChecked(True)
        self._cb_client.setToolTip("Show mods that run on the client side.")
        self._cb_client.toggled.connect(self._on_filter_changed)
        filt.addWidget(self._cb_client)
        self._cb_server = QCheckBox("Server")
        self._cb_server.setChecked(True)
        self._cb_server.setToolTip("Show mods that run on the server side.")
        self._cb_server.toggled.connect(self._on_filter_changed)
        filt.addWidget(self._cb_server)
        filt.addStretch()
        root.addLayout(filt)

        self._status = QLabel("Powered by Modrinth · no account needed")
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
        self._results.verticalScrollBar().valueChanged.connect(
            self._on_results_scrolled)
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
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        btns.addWidget(self._close_btn)
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

        self._d_icon_status = QLabel("")
        self._d_icon_status.setWordWrap(True)
        self._d_icon_status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        self._d_icon_status.hide()
        lay.addWidget(self._d_icon_status)

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
        thread.finished.connect(worker.deleteLater)
        thread.started.connect(worker.run)
        pair = (thread, worker)
        self._threads.append(pair)
        for sig in quit_signals:
            sig.connect(thread.quit)
        connect_thread_cleanup(thread, lambda: self._thread_finished(pair))
        thread.start()
        return worker

    def _set_curseforge_key(self):
        from ..mods import curseforge
        key, ok = QInputDialog.getText(self, "CurseForge API key", "Your own approved API key (session only):", QLineEdit.EchoMode.Password)
        if ok:
            curseforge.set_api_key(key)
            if self._provider.currentText() == "CurseForge":
                self._do_search()

    # --- search ---
    def _do_search(self, initial: bool = False):
        if self._icon_worker is not None:
            for pid in self._icon_worker.discard_queued():
                if pid not in self._pixmaps:
                    self._set_icon_state(pid, "idle")
                    self._update_item_icon(pid)
        self._key_btn.setVisible(self._provider.currentText() == "CurseForge")
        self._view = []
        self._results.clear()
        self._install_btn.setEnabled(False)
        self._search_generation += 1
        generation = self._search_generation
        q = self._search.text().strip()
        self._query = q
        self._offset = 0
        self._has_more = True
        self._loading_more = True
        for old_hit in self._all_hits:
            if old_hit.project_id not in self._pixmaps:
                self._icon_states.pop(old_hit.project_id, None)
        self._all_hits = []
        self._resolved.clear()
        self._sel_pid = None
        self._status.setText(
            "Loading popular mods…" if not q else "Searching…")
        self._search_btn.setEnabled(False)
        w = _SearchWorker(q, self._loader(), self._mc(),
                          offset=0, page=self._page, provider=self._provider.currentText())
        self._spawn(w, w.done, w.failed)
        w.done.connect(lambda offset, hits, g=generation: self._on_results(offset, hits) if g == self._search_generation else None)
        w.failed.connect(lambda message, g=generation: self._on_error(message) if g == self._search_generation else None)

    def _on_filter_changed(self, *_):
        # Re-filter what we already have, then top up if the view looks short.
        self._rebuild_view()
        self._maybe_load_more()

    def _passes_filter(self, hit) -> bool:
        want_client = self._cb_client.isChecked()
        want_server = self._cb_server.isChecked()
        if want_client and want_server:
            return True
        if not want_client and not want_server:
            return False
        if want_server:
            return getattr(hit, "server_side", "") != "unsupported"
        return getattr(hit, "client_side", "") != "unsupported"

    def _maybe_load_more(self, *_):
        if self._loading_more or not self._has_more:
            return
        # Don't hammer the API when the filter excludes everything.
        if not (self._cb_client.isChecked() or self._cb_server.isChecked()):
            return
        sb = self._results.verticalScrollBar()
        near_bottom = sb.maximum() <= 0 or sb.value() >= sb.maximum() - 4
        if not near_bottom:
            return
        self._loading_more = True
        self._status.setText("Loading more…")
        w = _SearchWorker(self._query, self._loader(), self._mc(),
                          offset=self._offset, page=self._page, provider=self._provider.currentText())
        self._spawn(w, w.done, w.failed)
        generation = self._search_generation
        w.done.connect(lambda offset, hits, g=generation: self._on_results(offset, hits) if g == self._search_generation else None)
        w.failed.connect(lambda message, g=generation: self._on_error(message) if g == self._search_generation else None)

    def _on_results(self, offset, hits):
        self._search_btn.setEnabled(True)
        self._loading_more = False
        # A late batch from a superseded query/offset: ignore it.
        if offset != self._offset:
            return
        hits = hits or []
        known = {h.project_id for h in self._all_hits}
        new_hits = [h for h in hits if h.project_id not in known]
        self._all_hits.extend(new_hits)
        for hit in new_hits:
            if hit.project_id in self._pixmaps:
                self._set_icon_state(hit.project_id, "loaded")
            elif not hit.icon_url:
                self._set_icon_state(hit.project_id, "missing")
        self._offset = offset + len(hits)
        self._has_more = len(hits) >= self._page
        self._rebuild_view()
        # _rebuild_view queues icons only for the currently visible results.
        # Filter may have hidden this whole page; keep pulling if so.
        self._maybe_load_more()

    def _make_item(self, h) -> QListWidgetItem:
        dl = modrinth.humanize_count(h.downloads)
        tag = h.server_label()
        second = f"{dl} downloads"
        if tag:
            second += f"  ·  {tag}"
        item = QListWidgetItem(f"{h.title}\n{second}")
        pix = self._pixmaps.get(h.project_id)
        if pix is not None:
            self._pixmaps.move_to_end(h.project_id)
            item.setIcon(QIcon(pix))
        else:
            state = self._icon_states.get(
                h.project_id, "loading" if h.icon_url else "missing")
            item.setIcon(self._placeholder_icon(state))
        self._item_by_pid[h.project_id] = item
        return item

    def _rebuild_view(self):
        """Re-filter every fetched hit and repaint the results list."""
        prev_pid = self._sel_pid
        self._view = [h for h in self._all_hits if self._passes_filter(h)]
        self._item_by_pid.clear()
        self._results.blockSignals(True)
        self._results.clear()
        for h in self._view:
            self._results.addItem(self._make_item(h))
        self._results.blockSignals(False)
        if prev_pid is not None:
            for i, h in enumerate(self._view):
                if h.project_id == prev_pid:
                    self._results.setCurrentRow(i)
                    break
        self._update_status()
        self._queue_visible_icons(priority=True)

    def _update_status(self):
        total = len(self._all_hits)
        shown = len(self._view)
        if total == 0:
            self._status.setText("No results — try a different name.")
            return
        msg = f"{shown} mod(s)"
        if shown != total:
            msg += f" shown of {total} fetched"
        msg += " · click for details, double-click to install"
        if self._has_more:
            msg += " · scroll for more"
        self._status.setText(msg)

    def _on_results_scrolled(self, *_args):
        # Prioritize newly visible rows before kicking off another page request.
        self._queue_visible_icons(priority=True)
        self._maybe_load_more()

    def _visible_icon_hits(self):
        """Return a small viewport window, never the entire accumulated catalog."""
        if not self._view:
            return []

        viewport = self._results.viewport()
        top_index = self._results.indexAt(QPoint(2, 2))
        bottom_index = self._results.indexAt(
            QPoint(2, max(2, viewport.height() - 2)))
        top = top_index.row() if top_index.isValid() else self._results.currentRow()
        if top < 0:
            top = 0
        bottom = bottom_index.row() if bottom_index.isValid() else top + 8
        bottom = max(top, bottom)
        # Strictly visible rows go first; then prefetch just below the fold,
        # and finally keep two rows above the viewport warm for small reversals.
        visible = self._view[top:min(len(self._view), bottom + 1)]
        after = self._view[bottom + 1:min(len(self._view), bottom + 4)]
        before = self._view[max(0, top - 2):top]
        return visible + after + before

    def _queue_visible_icons(self, *, priority=False):
        hits = self._visible_icon_hits()
        selected_row = self._results.currentRow()
        if 0 <= selected_row < len(self._view):
            selected = self._view[selected_row]
            hits = [selected] + [hit for hit in hits if hit.project_id != selected.project_id]
        self._start_icons(hits, priority=priority)

    def _set_icon_state(self, pid, state):
        self._icon_states[pid] = state
        self._icon_states.move_to_end(pid)
        while len(self._icon_states) > 1024:
            self._icon_states.popitem(last=False)

    def _reset_rejected_icon_jobs(self, pids):
        for pid in pids:
            if pid not in self._pixmaps and self._icon_states.get(pid) == "loading":
                self._set_icon_state(pid, "idle")
                self._update_item_icon(pid)
                if pid == self._sel_pid:
                    self._set_detail_icon_state(pid)

    def _start_icons(self, hits, *, priority=False):
        jobs = []
        for hit in hits:
            pid, url = hit.project_id, hit.icon_url
            if pid in self._pixmaps:
                self._set_icon_state(pid, "loaded")
                self._pixmaps.move_to_end(pid)
                self._update_item_icon(pid)
                continue
            if not url:
                self._set_icon_state(pid, "missing")
                self._update_item_icon(pid)
                continue
            state = self._icon_states.get(pid)
            if state in {"missing", "failed"}:
                continue
            if state == "loading" and not priority:
                continue
            self._set_icon_state(pid, "loading")
            self._update_item_icon(pid)
            jobs.append((pid, url))

        if not jobs:
            return

        worker = self._icon_worker
        if worker is not None:
            accepting = worker.add_jobs(jobs, priority=priority)
            self._reset_rejected_icon_jobs(worker.take_rejected())
            # A retiring worker remains authoritative until QThread.finished.
            # Its finish callback queues current visible work after retirement.
            return

        worker = _IconWorker()
        accepting = worker.add_jobs(jobs, priority=priority)
        self._reset_rejected_icon_jobs(worker.take_rejected())
        if not accepting or not worker._jobs:
            return

        self._icon_worker = worker
        # Connect every callback before starting the thread: a fast cache/CDN
        # response must not beat the icon signal connection.
        worker.icon.connect(self._on_icon)
        worker.failed.connect(self._on_icon_failed)
        worker.done.connect(self._on_icon_worker_done)
        self._spawn(worker, worker.done)

    def _decode_icon_pixmap(self, data):
        """Decode only bounded images and keep cached pixel dimensions small."""
        buffer = QBuffer()
        buffer.setData(QByteArray(bytes(data)))
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            return None
        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            size = reader.size()
            if (
                not size.isValid()
                or size.width() < 1
                or size.height() < 1
                or size.width() > 4096
                or size.height() > 4096
                or size.width() * size.height() > 4_194_304
            ):
                return None
            image = reader.read()
            if image.isNull():
                return None
            # The largest UI target is the 64px detail thumbnail; keeping
            # each result-row icon at this resolution limits retained image
            # data even though QListWidgetItem also owns a QIcon reference.
            if image.width() > 64 or image.height() > 64:
                image = image.scaled(
                    64, 64, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
            return QPixmap.fromImage(image)
        finally:
            buffer.close()

    def _on_icon(self, pid, data):
        pix = self._decode_icon_pixmap(data)
        if pix is None or pix.isNull():
            self._on_icon_failed(pid)
            return

        self._set_icon_state(pid, "loaded")
        self._pixmaps[pid] = pix
        self._pixmaps.move_to_end(pid)
        while len(self._pixmaps) > 256:
            evicted_pid, _pixmap = self._pixmaps.popitem(last=False)
            self._set_icon_state(evicted_pid, "idle")
            # QListWidgetItem owns a QIcon reference independently of the
            # decoded-image LRU; replace it too or row icons keep old pixels.
            self._update_item_icon(evicted_pid)
        self._update_item_icon(pid)
        if pid == self._sel_pid:
            self._set_detail_icon(pix)

    def _on_icon_failed(self, pid):
        if pid in self._pixmaps:
            self._set_icon_state(pid, "loaded")
            return
        self._set_icon_state(pid, "failed")
        self._update_item_icon(pid)
        if pid == self._sel_pid:
            self._set_detail_icon_state(pid)

    def _on_icon_worker_done(self):
        worker = self.sender()
        if self._icon_worker is not worker:
            return
        # Keep the worker registered until QThread.finished confirms the native
        # thread has stopped. This prevents overlapping icon workers during
        # the small interval between worker.done and thread retirement.
        for pid, state in tuple(self._icon_states.items()):
            if state == "loading" and pid not in self._pixmaps:
                self._set_icon_state(pid, "idle")
                self._update_item_icon(pid)
                if pid == self._sel_pid:
                    self._set_detail_icon_state(pid)

    def _update_item_icon(self, pid):
        item = self._item_by_pid.get(pid)
        if item is None:
            return
        pix = self._pixmaps.get(pid)
        if pix is not None:
            item.setIcon(QIcon(pix))
        else:
            item.setIcon(self._placeholder_icon(self._icon_states.get(pid, "missing")))

    def _placeholder_icon(self, state):
        """Return a small locally painted placeholder, never a remote asset."""
        kind = "failed" if state == "failed" else "missing" if state == "missing" else "loading"
        cached = self._placeholder_icons.get(kind)
        if cached is not None:
            return cached

        pix = QPixmap(self._ICON_PX, self._ICON_PX)
        pix.fill(QColor(theme.SURFACE0))
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if kind == "loading":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.SURFACE1))
            painter.drawRoundedRect(8, 12, 30, 6, 3, 3)
            painter.drawRoundedRect(8, 24, 22, 6, 3, 3)
        else:
            painter.setPen(QPen(QColor(theme.SUBTEXT), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(8, 8, 30, 30, 4, 4)
            painter.drawEllipse(14, 14, 5, 5)
            painter.drawLine(11, 31, 20, 22)
            painter.drawLine(20, 22, 26, 28)
            painter.drawLine(26, 28, 31, 23)
            painter.drawLine(31, 23, 35, 31)
            if kind == "failed":
                painter.setPen(QPen(QColor(theme.ORANGE), 2))
                painter.drawLine(29, 8, 38, 17)
                painter.drawLine(38, 8, 29, 17)
        painter.end()
        icon = QIcon(pix)
        self._placeholder_icons[kind] = icon
        return icon

    def _set_detail_icon_state(self, pid):
        pix = self._pixmaps.get(pid)
        if pix is not None:
            self._set_detail_icon(pix)
            return
        state = self._icon_states.get(pid, "missing")
        descriptions = {
            "loading": "Loading icon…",
            "idle": "Loading icon…",
            "missing": "No icon provided",
            "failed": "Icon unavailable",
        }
        message = descriptions.get(state, "No icon provided")
        self._d_icon.setText("")
        self._d_icon.setPixmap(self._placeholder_icon(state).pixmap(64, 64))
        self._d_icon.setToolTip(message)
        self._d_icon_status.setText(message)
        self._d_icon_status.setVisible(bool(message))


    def _on_error(self, msg):
        self._search_btn.setEnabled(True)
        self._loading_more = False
        self._install_btn.setEnabled(False)
        self._status.setText("⚠ " + msg)

    # --- selection / detail ---
    def _on_sel(self, row):
        if row < 0 or row >= len(self._view):
            return
        hit = self._view[row]
        self._sel_pid = hit.project_id
        self._install_btn.setEnabled(False)
        self._d_link.setEnabled(True)
        self._d_link.setText("View on CurseForge" if hit.project_id.startswith("cf:") else "View on Modrinth")

        self._d_title.setText(hit.title)
        self._d_author.setText(f"by {hit.author}" if hit.author else "")
        stats = f"⬇ {modrinth.humanize_count(hit.downloads)} downloads"
        if hit.follows:
            stats += f"   ♥ {modrinth.humanize_count(hit.follows)} followers"
        if hit.categories:
            stats += "\n" + "  ".join(f"#{c}" for c in hit.categories[:6])
        self._d_stats.setText(stats)

        if hit.is_client_only:
            self._d_compat.setText(
                "⚠ Client-only mod — this usually has no effect on a "
                "dedicated server.")
            self._d_compat.setStyleSheet(
                f"color:{theme.ORANGE}; font-size:12px; font-weight:600;")
        elif hit.server_label():
            self._d_compat.setText("✓ " + hit.server_label())
            self._d_compat.setStyleSheet(
                f"color:{theme.GREEN}; font-size:12px; font-weight:600;")
        else:
            self._d_compat.setText("")

        self._d_desc.setPlainText(hit.description or "")
        self._d_deps.setText("Checking version & dependencies…")

        pix = self._pixmaps.get(hit.project_id)
        if pix is not None:
            self._pixmaps.move_to_end(hit.project_id)
            self._set_detail_icon(pix)
        else:
            self._set_detail_icon_state(hit.project_id)
            # The small LRU may have evicted an older icon from decoded-memory
            # cache while its result-row QIcon remains valid. Fetch it again
            # only when that result is selected, not for the whole old catalog.
            if hit.icon_url and self._icon_states.get(hit.project_id) not in {"failed", "missing"}:
                self._start_icons([hit], priority=True)
                self._set_detail_icon_state(hit.project_id)

        # resolve dependencies for the preview (and to pre-stage the install)
        if hit.project_id in self._resolved:
            self._show_deps(hit.project_id, self._resolved[hit.project_id])
        else:
            w = _ResolveWorker(hit.project_id, self._loader(), self._mc())
            self._spawn(w, w.done, w.failed)
            w.done.connect(self._on_resolved)
            w.failed.connect(self._on_resolve_failed)

    def _set_detail_icon(self, pix):
        self._d_icon_status.clear()
        self._d_icon_status.hide()
        self._d_icon.setToolTip("")
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
            self._d_deps.setText("⚠ " + msg)

    def _open_page(self):
        row = self._results.currentRow()
        if 0 <= row < len(self._view):
            QDesktopServices.openUrl(QUrl(self._view[row].page_url()))

    # --- install ---
    def _do_install(self):
        row = self._results.currentRow()
        if row < 0 or row >= len(self._view):
            return
        hit = self._view[row]
        files = self._resolved.get(hit.project_id)
        if not files:
            self._status.setText("Still checking versions — try again in a moment.")
            return
        if hit.is_client_only:
            reply = QMessageBox.question(
                self, "Client-only mod",
                f"{hit.title} is marked client-only and usually does nothing on a "
                "dedicated server.\n\nInstall it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                return
        from ..process.tmux_manager import TmuxManager
        if TmuxManager().get_status(self._instance) != "stopped":
            QMessageBox.warning(self, "Stop the server first", "Mod installation requires a confirmed stopped server.")
            return
        review = "Install these verified files?\n\n" + "\n".join("• " + f.filename for f in files) + "\n\nExisting linked versions are replaced together. Originals are retained in .crucible/mod-history. No world files are modified."
        if QMessageBox.question(self, "Review mod installation", review, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel) != QMessageBox.StandardButton.Yes:
            return
        self._install_btn.setEnabled(False)
        n = len(files)
        self._status.setText(
            f"Installing {hit.title} ({n} file{'s' if n != 1 else ''})…")
        w = _InstallWorker(self._instance.path, files, self._instance)
        self._spawn(w, w.done, w.failed)
        w.done.connect(self._on_installed)
        w.failed.connect(self._on_error)

    def _on_installed(self, res):
        self._install_btn.setEnabled(True)
        self._status.setText("✓ " + res.summary())
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

    def _thread_finished(self, pair) -> None:
        worker = pair[1]
        is_icon_worker = worker is self._icon_worker
        if pair in self._threads:
            self._threads.remove(pair)
        if is_icon_worker:
            # connect_thread_cleanup invokes this only after native thread exit.
            self._icon_worker = None
            if not self._close_requested and self.isVisible():
                self._queue_visible_icons(priority=True)
        self._finish_requested_close()

    def _workers_running(self) -> bool:
        return any(thread.isRunning() for thread, _worker in self._threads)

    def _request_close(self) -> None:
        if self._icon_worker is not None:
            self._icon_worker.cancel()
        if self._workers_running():
            self._close_requested = True
            self._close_btn.setEnabled(False)
            self._status.setText(
                "Closing after the current network/install operation exits safely…"
            )
            return
        super().reject()

    def _finish_requested_close(self) -> None:
        if self._close_requested and not self._workers_running():
            super().reject()

    def reject(self) -> None:
        self._request_close()

    def closeEvent(self, event):
        if self._workers_running():
            self._request_close()
            event.ignore()
            return
        super().closeEvent(event)
