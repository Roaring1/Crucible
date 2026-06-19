"""
crucible/ui/add_mod_dialog.py

"Add a mod" - the simplest possible way to add a mod: type a name, click a
result, done. Searches Modrinth (no account needed), matches the server's
Minecraft version + loader, pulls in required dependencies, and drops the jars
into mods/. All network work runs on a worker thread.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QListWidgetItem, QMessageBox,
)

from ..mods import modrinth
from . import theme


class _SearchWorker(QObject):
    done = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, query, loader, mc):
        super().__init__()
        self._q, self._loader, self._mc = query, loader, mc

    def run(self):
        try:
            self.done.emit(modrinth.search(self._q, loader=self._loader,
                                           mc_version=self._mc))
        except Exception as e:  # noqa: BLE001 - surface any failure to UI
            self.failed.emit(str(e))


class _InstallWorker(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, server_path, project_id, loader, mc):
        super().__init__()
        self._path, self._pid = server_path, project_id
        self._loader, self._mc = loader, mc

    def run(self):
        try:
            files = modrinth.resolve_with_deps(self._pid, loader=self._loader,
                                               mc_version=self._mc)
            self.done.emit(modrinth.install_files(self._path, files))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class AddModDialog(QDialog):
    def __init__(self, instance, parent=None):
        super().__init__(parent)
        self._instance = instance
        self._hits = []
        self._threads = []
        self.setWindowTitle("Add a mod")
        self.resize(540, 480)
        self._build_ui()

    def _loader(self):
        return getattr(self._instance, "loader", "") or "vanilla"

    def _mc(self):
        return getattr(self._instance, "minecraft_version", "") or ""

    def _build_ui(self):
        lay = QVBoxLayout(self)
        head = QLabel(f"Search mods for Minecraft {self._mc() or '?'} \u00b7 {self._loader()}")
        head.setStyleSheet(f"font-weight:700; color:{theme.TEXT};")
        lay.addWidget(head)

        row = QHBoxLayout()
        self._search = QLineEdit()
        self._search.setPlaceholderText("e.g. JEI, Sodium, Create\u2026")
        self._search.returnPressed.connect(self._do_search)
        row.addWidget(self._search, 1)
        self._search_btn = QPushButton("Search")
        self._search_btn.clicked.connect(self._do_search)
        row.addWidget(self._search_btn)
        lay.addLayout(row)

        self._status = QLabel("Powered by Modrinth \u00b7 no account needed")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        lay.addWidget(self._status)

        self._results = QListWidget()
        self._results.itemSelectionChanged.connect(self._on_sel)
        self._results.itemDoubleClicked.connect(lambda *_: self._do_install())
        lay.addWidget(self._results, 1)

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
        lay.addLayout(btns)

    def _run(self, worker, on_done, on_fail):
        thread = QThread()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(on_done)
        worker.failed.connect(on_fail)
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        pair = (thread, worker)
        self._threads.append(pair)
        thread.finished.connect(lambda: self._threads.remove(pair)
                                if pair in self._threads else None)
        thread.start()

    def _do_search(self):
        q = self._search.text().strip()
        if not q:
            return
        self._status.setText("Searching\u2026")
        self._search_btn.setEnabled(False)
        self._run(_SearchWorker(q, self._loader(), self._mc()),
                  self._on_results, self._on_error)

    def _on_results(self, hits):
        self._search_btn.setEnabled(True)
        self._hits = hits
        self._results.clear()
        if not hits:
            self._status.setText("No results - try a different name.")
            return
        self._status.setText(f"{len(hits)} result(s) - double-click to install")
        for h in hits:
            self._results.addItem(QListWidgetItem(
                f"{h.title}\n   {(h.description or '')[:90]}"))

    def _on_error(self, msg):
        self._search_btn.setEnabled(True)
        self._install_btn.setEnabled(False)
        self._status.setText("\u26a0 " + msg)

    def _on_sel(self):
        self._install_btn.setEnabled(self._results.currentRow() >= 0)

    def _do_install(self):
        row = self._results.currentRow()
        if row < 0 or row >= len(self._hits):
            return
        hit = self._hits[row]
        self._install_btn.setEnabled(False)
        self._status.setText(f"Installing {hit.title} (+ dependencies)\u2026")
        self._run(_InstallWorker(self._instance.path, hit.project_id,
                                 self._loader(), self._mc()),
                  self._on_installed, self._on_error)

    def _on_installed(self, res):
        self._install_btn.setEnabled(True)
        self._status.setText("\u2713 " + res.summary())
        if res.failed:
            QMessageBox.warning(self, "Add a mod",
                                "Some files could not be installed:\n" +
                                "\n".join(res.failed))
