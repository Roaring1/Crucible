"""
crucible/ui/pack_update_dialog.py

The Update-pack and Rollback dialogs.

Widgets and threads only. Every decision comes from pack_update_model (pure,
headlessly tested) and every filesystem or network step from
crucible.importers.pack_update. Nothing heavy runs on the UI thread, nothing
is installed until the user has read the plan and typed the target version,
and every worker thread is retired through thread_lifecycle.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QVBoxLayout,
)

from . import pack_update_model as model
from . import theme
from .thread_lifecycle import connect_thread_cleanup


class _JobWorker(QObject):
    """Runs one pack-update job off the UI thread.

    The job is a callable taking a log callback, so "plan this", "install
    that" and "roll back" share one worker instead of one class per verb.
    It never raises: a failure comes back as text on `finished`.
    """

    log = pyqtSignal(str)
    finished = pyqtSignal(object, str)          # result, error

    def __init__(self, job, parent: QObject | None = None):
        super().__init__(parent)
        self._job = job

    @pyqtSlot()
    def run(self) -> None:
        result = None
        error = ""
        try:
            result = self._job(lambda message: self.log.emit(str(message)))
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
        finally:
            self.finished.emit(result, error)


class _JobDialog(QDialog):
    """Shared plumbing: one worker thread at a time, cleanly retired."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _JobWorker | None = None
        self._busy = False

    # Threading

    def _start_job(self, job, on_finished) -> bool:
        """Start `job` on a fresh thread. False when one is already running."""
        if self._busy:
            return False
        self._busy = True
        self._thread = QThread(self)
        self._worker = _JobWorker(job)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.log.connect(self._append)
        self._worker.finished.connect(on_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        connect_thread_cleanup(self._thread, self._thread_finished)
        self._set_busy(True)
        self._thread.start()
        return True

    def _thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._busy = False
        self._set_busy(False)

    def has_active_operation(self) -> bool:
        """True while a job is in flight (the panel asks before closing)."""
        return self._busy

    # Subclass hooks

    def _set_busy(self, busy: bool) -> None:
        """Subclasses enable/disable their own controls here."""

    @pyqtSlot(str)
    def _append(self, message: str) -> None:
        """Subclasses show a log line here."""

    # A half-finished swap must never be abandoned by closing the window.

    def _refuse_close(self) -> bool:
        if not self._busy:
            return False
        QMessageBox.information(
            self, "Still working",
            "This is still running. Let it finish -- closing now would leave "
            "you guessing about what got installed.")
        return True

    def reject(self) -> None:
        if self._refuse_close():
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self._refuse_close():
            event.ignore()
            return
        super().closeEvent(event)


class PackUpdateDialog(_JobDialog):
    """Check for a modpack update, review the plan, then install it."""

    updated = pyqtSignal()          # a successful install; tabs should reload

    def __init__(self, instance, manager=None, running=None, parent=None):
        super().__init__(parent)
        self._instance = instance
        self._manager = manager
        self._running = running
        self._preview = None
        self._cancel = threading.Event()
        self._installed = False

        self.setWindowTitle("Update modpack")
        self.setMinimumSize(780, 620)
        self.setStyleSheet(theme.STYLESHEET)
        self._build_ui()

    # UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        title = QLabel("Update this server's modpack")
        title.setStyleSheet(
            f"font-size:16px; font-weight:600; color:{theme.TEXT};")
        root.addWidget(title)

        blurb = QLabel(
            "Crucible shows you exactly what an update would change before it "
            "changes anything. Your world, server settings and hand-added mods "
            "are carried over, the current install is kept aside so you can "
            "roll back, and an update is refused while the server is running.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color:{theme.SUBTEXT};")
        root.addWidget(blurb)

        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("Source:"))
        self._source = QLineEdit()
        self._source.setClearButtonEnabled(True)
        self._source.setPlaceholderText(
            "Leave empty to use the pack's own index, or paste a link, a "
            "version like 2.8.4, or a path to a .zip")
        self._source.setAccessibleName("Modpack update source")
        self._source.returnPressed.connect(self._start_check)
        browse = QPushButton("Browse\u2026")
        browse.setAccessibleName("Choose a downloaded server pack")
        browse.setToolTip("Pick a server pack .zip you already downloaded")
        browse.clicked.connect(self._browse)
        src_row.addWidget(self._source, 1)
        src_row.addWidget(browse)
        root.addLayout(src_row)

        opts = QHBoxLayout()
        self._prerelease = QCheckBox("Offer beta and release-candidate versions")
        self._keep_previous = QCheckBox("Keep the old install so I can roll back")
        self._keep_previous.setChecked(True)
        self._check_btn = QPushButton("Check for updates")
        self._check_btn.setDefault(True)
        self._check_btn.clicked.connect(self._start_check)
        opts.addWidget(self._prerelease)
        opts.addWidget(self._keep_previous)
        opts.addStretch()
        opts.addWidget(self._check_btn)
        root.addLayout(opts)

        self._plan_view = QPlainTextEdit()
        self._plan_view.setReadOnly(True)
        self._plan_view.setAccessibleName("Update plan")
        self._plan_view.setPlaceholderText(
            "The plan appears here first. Nothing is downloaded or installed "
            "until you confirm it.")
        root.addWidget(self._plan_view, 1)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.hide()
        root.addWidget(self._progress)

        confirm_row = QHBoxLayout()
        self._confirm_label = QLabel("Type the new version to confirm:")
        self._confirm = QLineEdit()
        self._confirm.setEnabled(False)
        self._confirm.setAccessibleName("Type the new version to confirm")
        self._confirm.textChanged.connect(self._on_confirm_changed)
        self._apply_btn = QPushButton("Install update")
        self._apply_btn.setEnabled(False)
        self._apply_btn.clicked.connect(self._start_apply)
        confirm_row.addWidget(self._confirm_label)
        confirm_row.addWidget(self._confirm, 1)
        confirm_row.addWidget(self._apply_btn)
        root.addLayout(confirm_row)

        bottom = QHBoxLayout()
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setEnabled(False)
        self._stop_btn.setToolTip(
            "Stop after the current step. Nothing is swapped in until the new "
            "install has been verified.")
        self._stop_btn.clicked.connect(self._request_cancel)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.close)
        bottom.addWidget(self._stop_btn)
        bottom.addStretch()
        bottom.addWidget(self._close_btn)
        root.addLayout(bottom)

    # Public API

    def start_check(self) -> None:
        """Begin an index check straight away (used by the tab buttons)."""
        self._start_check()

    def focus_source(self) -> None:
        """Open with the cursor in the source box, waiting for a link."""
        self._source.setFocus()

    def installed(self) -> bool:
        """True when this dialog actually swapped an install in."""
        return self._installed

    # Checking / planning

    def _browse(self) -> None:
        start = str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a server pack archive", start,
            "Pack archives (*.zip *.mrpack);;All files (*)")
        if path:
            self._source.setText(path)
            self._start_check()

    def _start_check(self) -> None:
        if self.has_active_operation():
            return
        instance = self._instance
        text = self._source.text().strip()
        prerelease = self._prerelease.isChecked()
        running = self._running

        self._preview = None
        self._confirm.clear()
        self._confirm.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self._plan_view.setPlainText(
            "Working out what an update would do (nothing is being changed)\u2026")

        def job(log):
            resolved = running
            if resolved is None:
                resolved = model.resolve_running(instance)
            return model.build_preview(
                instance, source_text=text, include_prerelease=prerelease,
                running=resolved, log_cb=log)

        self._start_job(job, self._on_check_finished)

    @pyqtSlot(object, str)
    def _on_check_finished(self, result, error: str) -> None:
        if error or result is None:
            self._plan_view.setPlainText(
                "Crucible could not check for updates.\n\n"
                f"{error or 'Nothing came back from the check.'}\n\n"
                "You can still paste a link or pick a downloaded .zip above.")
            return

        self._preview = result
        self._plan_view.setPlainText("\n".join(model.describe_preview(result)))

        if not result.ok:
            if result.blockers:
                self._append("")
                self._append("Fix the blockers above and check again.")
            return

        word = result.confirm_word
        self._confirm.setEnabled(True)
        self._confirm.setPlaceholderText(word)
        self._confirm_label.setText(f'Type "{word}" to confirm:')
        if result.needs_confirmation:
            self._append("")
            self._append(
                "This source did not identify itself the way a published "
                "release does. Read the questions above before installing.")
        self._sync_apply_button()

    def _on_confirm_changed(self, _text: str) -> None:
        self._sync_apply_button()

    def _sync_apply_button(self) -> None:
        preview = self._preview
        if preview is None or not preview.ok or self.has_active_operation():
            self._apply_btn.setEnabled(False)
            return
        typed = self._confirm.text().strip()
        self._apply_btn.setEnabled(bool(typed) and typed == preview.confirm_word)

    # Installing

    def _start_apply(self) -> None:
        preview = self._preview
        if preview is None or not preview.ok or self.has_active_operation():
            return
        if self._confirm.text().strip() != preview.confirm_word:
            return

        instance = self._instance
        plan = preview.plan
        keep_previous = self._keep_previous.isChecked()
        cancel = self._cancel
        cancel.clear()

        self._append("")
        self._append(f"Installing {preview.confirm_word}\u2026")

        def job(log):
            from ..importers import pack_update as pu
            backup_cb = model.backup_callback(instance, plan, log_cb=log)
            outcome = pu.apply_update(
                instance, plan, backup_cb=backup_cb, log_cb=log,
                cancel=cancel, keep_previous=keep_previous)
            if getattr(outcome, "ok", False):
                # Stays on the worker: readiness touches the disk and can
                # shell out to java.
                for line in model.readiness_report(instance):
                    log(line)
            return outcome

        if self._start_job(job, self._on_apply_finished):
            self._stop_btn.setEnabled(True)

    def _request_cancel(self) -> None:
        self._cancel.set()
        self._stop_btn.setEnabled(False)
        self._append(
            "Stopping after the current step. The install you have now stays "
            "live until the new one is fully staged and verified.")

    @pyqtSlot(object, str)
    def _on_apply_finished(self, result, error: str) -> None:
        self._stop_btn.setEnabled(False)
        if error or result is None:
            self._append("")
            self._append(
                f"The update did not run: {error or 'nothing came back'}. "
                "Your install was not changed.")
            return

        for line in (result.messages or []):
            self._append(f"    {line}")
        self._append("")
        self._append(result.summary())

        if result.cancelled:
            self._append("Stopped before the swap -- your install is untouched.")
            return
        if not result.ok:
            self._append("Your existing install was left in place.")
            return

        self._installed = True
        provider = getattr(getattr(self._preview, "identity", None), "provider", "")
        note = model.sync_registry_after_update(
            self._manager, self._instance,
            to_version=result.to_version, provider=provider)
        if note:
            self._append(note)
        if result.backup_path:
            self._append(f"World backup: {result.backup_path}")
        if result.previous_dir:
            self._append(f"Old install kept at: {result.previous_dir}")
        self._append("")
        self._append(
            "On the first start after a pack update, Forge may ask you to "
            "confirm world changes in the console. Watch the Console tab.")

        self._confirm.setEnabled(False)
        self._apply_btn.setEnabled(False)
        self.updated.emit()

    # Hooks

    @pyqtSlot(str)
    def _append(self, message: str) -> None:
        self._plan_view.appendPlainText(str(message))

    def _set_busy(self, busy: bool) -> None:
        self._progress.setVisible(busy)
        for widget in (self._source, self._prerelease, self._keep_previous,
                       self._check_btn, self._close_btn):
            widget.setEnabled(not busy)
        if busy:
            self._apply_btn.setEnabled(False)
        else:
            self._sync_apply_button()


class RollbackDialog(_JobDialog):
    """Put a kept pre-update install back, or clear old ones out."""

    updated = pyqtSignal()

    def __init__(self, instance, manager=None, running=None, parent=None):
        super().__init__(parent)
        self._instance = instance
        self._manager = manager
        self._running = running
        self._entries: list = []

        self.setWindowTitle("Roll back a modpack update")
        self.setMinimumSize(720, 540)
        self.setStyleSheet(theme.STYLESHEET)
        self._build_ui()
        self._reload_entries()

    # UI

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        title = QLabel("Roll back to an earlier install")
        title.setStyleSheet(
            f"font-size:16px; font-weight:600; color:{theme.TEXT};")
        root.addWidget(title)

        blurb = QLabel(
            "Every update sets the old install aside instead of deleting it. "
            "Restoring one puts that whole server folder back, world included. "
            "The install that is live now is moved aside too, so a rollback is "
            "itself reversible.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color:{theme.SUBTEXT};")
        root.addWidget(blurb)

        self._list = QListWidget()
        self._list.setAccessibleName("Kept installs")
        self._list.currentRowChanged.connect(self._on_row_changed)
        root.addWidget(self._list, 1)

        self._keep_world = QCheckBox(
            "Carry the current world onto the restored pack")
        self._keep_world.setToolTip(
            "Off by default: if the newer pack damaged the world, bringing it "
            "back carries the damage with it.")
        root.addWidget(self._keep_world)

        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setAccessibleName("Rollback details")
        self._log.setMaximumHeight(180)
        root.addWidget(self._log)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(False)
        self._progress.hide()
        root.addWidget(self._progress)

        actions = QHBoxLayout()
        self._rollback_btn = QPushButton("Roll back to selected")
        self._rollback_btn.setEnabled(False)
        self._rollback_btn.clicked.connect(self._start_rollback)
        actions.addWidget(self._rollback_btn)
        actions.addStretch()
        actions.addWidget(QLabel("Keep newest:"))
        self._keep_spin = QSpinBox()
        self._keep_spin.setRange(0, 20)
        self._keep_spin.setValue(1)
        self._keep_spin.setAccessibleName("How many old installs to keep")
        actions.addWidget(self._keep_spin)
        self._prune_btn = QPushButton("Delete the rest\u2026")
        self._prune_btn.setAccessibleName("Delete old installs")
        self._prune_btn.clicked.connect(self._start_prune)
        actions.addWidget(self._prune_btn)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.close)
        actions.addWidget(self._close_btn)
        root.addLayout(actions)

    def _reload_entries(self) -> None:
        self._entries = model.rollback_entries(self._instance)
        self._list.clear()
        for entry in self._entries:
            self._list.addItem(entry.label())
        have = bool(self._entries)
        self._prune_btn.setEnabled(have)
        self._keep_spin.setEnabled(have)
        if have:
            self._list.setCurrentRow(0)
        else:
            self._rollback_btn.setEnabled(False)
            self._log.setPlainText(
                "There is nothing to roll back to. Crucible keeps an install "
                "aside the first time you update this server's pack.")

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._entries):
            self._rollback_btn.setEnabled(False)
            return
        self._rollback_btn.setEnabled(not self.has_active_operation())
        self._log.setPlainText("\n".join(model.describe_rollback(
            self._entries[row],
            keep_current_world=self._keep_world.isChecked())))

    # Actions

    def _selected_entry(self):
        row = self._list.currentRow()
        if row < 0 or row >= len(self._entries):
            return None
        return self._entries[row]

    def _start_rollback(self) -> None:
        entry = self._selected_entry()
        if entry is None or self.has_active_operation():
            return
        keep_world = self._keep_world.isChecked()
        answer = QMessageBox.question(
            self, "Roll back this server?",
            f"Put {entry.label()} back in place?\n\n"
            + ("The current world will be carried onto it."
               if keep_world else
               "The world saved with that install will be restored too."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        instance = self._instance
        path = entry.path
        self._log.appendPlainText("")
        self._log.appendPlainText(f"Rolling back to {entry.label()}\u2026")

        def job(log):
            from ..importers import pack_update as pu
            return pu.rollback_update(
                instance, path,
                # Treat the dialog's opening state as display-only. The
                # importer re-probes immediately before the actual rename.
                running=False,
                running_check=lambda: model.resolve_running(instance),
                keep_current_world=keep_world, log_cb=log)

        self._start_job(job, self._on_rollback_finished)

    @pyqtSlot(object, str)
    def _on_rollback_finished(self, result, error: str) -> None:
        if error or result is None:
            self._append(
                f"The rollback did not run: {error or 'nothing came back'}. "
                "Nothing was changed.")
            return
        for line in (result.messages or []):
            self._append(f"    {line}")
        self._append(result.summary())
        if result.ok:
            note = model.sync_registry_after_update(
                self._manager, self._instance, to_version=result.to_version)
            if note:
                self._append(note)
            self.updated.emit()
        self._reload_entries()

    def _start_prune(self) -> None:
        if self.has_active_operation():
            return
        keep = self._keep_spin.value()
        _kept, doomed = model.prune_preview(self._instance, keep)
        if not doomed:
            QMessageBox.information(
                self, "Nothing to delete",
                f"Keeping the newest {keep} leaves nothing to remove.")
            return
        names = "\n".join(f"    {entry.label()}" for entry in doomed)
        answer = QMessageBox.question(
            self, "Delete old installs?",
            f"Permanently delete {len(doomed)} kept install(s)?\n\n{names}\n\n"
            "This frees disk space and cannot be undone. Worlds saved inside "
            "those folders go with them.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return

        instance = self._instance

        def job(_log):
            from ..importers import pack_update as pu
            return pu.prune_previous_installs(instance, keep=keep)

        self._start_job(job, self._on_prune_finished)

    @pyqtSlot(object, str)
    def _on_prune_finished(self, result, error: str) -> None:
        if error or result is None:
            self._append(f"Could not delete the old installs: "
                         f"{error or 'nothing came back'}.")
            return
        freed, removed = result
        if removed:
            self._append(f"Removed {len(removed)} old install(s), freeing "
                         f"{model.human_bytes(freed)}:")
            self._append("\n".join(f"    {name}" for name in removed))
        else:
            self._append("Nothing was removed.")
        self._reload_entries()

    # Hooks

    @pyqtSlot(str)
    def _append(self, message: str) -> None:
        self._log.appendPlainText(str(message))

    def _set_busy(self, busy: bool) -> None:
        self._progress.setVisible(busy)
        for widget in (self._list, self._keep_world, self._keep_spin,
                       self._prune_btn, self._close_btn):
            widget.setEnabled(not busy)
        if busy:
            self._rollback_btn.setEnabled(False)
        else:
            self._on_row_changed(self._list.currentRow())
