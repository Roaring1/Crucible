"""Read-only viewer and safe forget action for local player-pack sharing history."""

from __future__ import annotations

import hashlib
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..sharing.history import ShareHistoryError, ShareHistoryStore
from . import theme


_EVENT_LABELS = {
    "pack-prepared": "ZIP prepared",
    "build-failed": "Build failed",
    "upload-started": "Upload started",
    "upload-succeeded": "Upload succeeded",
    "upload-failed": "Upload failed",
    "upload-cancelled": "Upload cancelled",
    "local-zip-deleted": "Local ZIP deleted",
}


class ShareHistoryDialog(QDialog):
    """Show local share events; cleanup is explicit and never affects remote links."""

    def __init__(self, store: ShareHistoryStore, parent=None):
        super().__init__(parent)
        self._store = store
        self._records = []
        # Only open regular ZIPs directly inside Crucible's player-packs folder.
        # A hand-edited history file must not turn this button into an arbitrary-file opener.
        self._allowed_pack_dir = store.path.parent.parent / "player-packs"
        self.setWindowTitle("Player-pack share history")
        self.resize(1000, 480)
        self.setMinimumSize(760, 340)
        self.setStyleSheet(theme.STYLESHEET)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(8)

        info = QLabel(
            "History is stored locally with owner-only permissions and keeps at most 50 events. "
            "Gofile links shown here were returned previously; Crucible does not re-check their current validity. "
            "Clearing history does not delete ZIP files or revoke remote links."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        root.addWidget(info)

        self._table = QTableWidget(0, 7)
        self._table.setHorizontalHeaderLabels([
            "Time (UTC)", "Server", "Activity", "ZIP", "Size", "Upload status", "Last returned link"
        ])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._table.itemSelectionChanged.connect(self._refresh_actions)
        root.addWidget(self._table, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        root.addWidget(self._status)

        actions = QHBoxLayout()
        self._copy_link = QPushButton("Copy selected link")
        self._copy_link.clicked.connect(self._copy_selected_link)
        actions.addWidget(self._copy_link)
        self._open_zip = QPushButton("Open local ZIP")
        self._open_zip.clicked.connect(self._open_selected_zip)
        actions.addWidget(self._open_zip)
        self._delete_zip = QPushButton("Delete local ZIP…")
        self._delete_zip.clicked.connect(self._delete_selected_zip)
        actions.addWidget(self._delete_zip)
        actions.addStretch()
        self._clear = QPushButton("Clear history")
        self._clear.clicked.connect(self._clear_history)
        actions.addWidget(self._clear)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        actions.addWidget(close)
        root.addLayout(actions)
        self._reload()

    def _reload(self):
        self._table.setRowCount(0)
        try:
            records = self._store.load()
        except (OSError, ShareHistoryError) as exc:
            self._records = []
            self._status.setText(f"Could not read local share history safely: {exc}")
            self._clear.setEnabled(True)
            self._refresh_actions()
            return

        self._records = list(reversed(records))
        for record in self._records:
            row = self._table.rowCount()
            self._table.insertRow(row)
            values = [
                record["timestampUtc"].replace("T", " ").replace("+00:00", " UTC"),
                record["instanceName"] or "Unknown server",
                _EVENT_LABELS.get(record["event"], record["event"]),
                record["fileName"] or "(no ZIP produced)",
                _format_size(record["sizeBytes"]),
                _format_status(record["uploadStatus"], record["errorCode"]),
                "Saved link; not re-verified" if record["shareUrl"] else "—",
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 3 and record["localPath"]:
                    item.setToolTip(record["localPath"])
                if column == 6 and record["shareUrl"]:
                    item.setToolTip(record["shareUrl"])
                self._table.setItem(row, column, item)

        self._clear.setEnabled(bool(self._records))
        if self._records:
            self._status.setText(
                f"Showing {len(self._records)} of at most 50 retained events. Local ZIPs are never deleted by history cleanup."
            )
        else:
            self._status.setText("No local sharing history yet.")
        self._refresh_actions()

    def _selected_record(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._records):
            return None
        return self._records[row]

    def _safe_local_archive(self, value: str):
        if not isinstance(value, str) or not value:
            return None
        candidate = Path(value)
        root = self._allowed_pack_dir
        try:
            if (
                root.is_symlink()
                or not root.is_dir()
                or candidate.suffix.lower() != ".zip"
                or candidate.is_symlink()
                or not candidate.is_file()
                or candidate.parent.resolve(strict=True) != root.resolve(strict=True)
            ):
                return None
            return candidate
        except OSError:
            return None

    def _refresh_actions(self):
        record = self._selected_record()
        self._copy_link.setEnabled(bool(record and record.get("shareUrl")))
        local_path = self._safe_local_archive(record.get("localPath", "")) if record else None
        self._open_zip.setEnabled(local_path is not None)
        self._delete_zip.setEnabled(local_path is not None)

    def _copy_selected_link(self):
        record = self._selected_record()
        if record and record.get("shareUrl"):
            QApplication.clipboard().setText(record["shareUrl"])
            self._status.setText("Copied the last returned Gofile link. Its current validity was not checked.")

    def _open_selected_zip(self):
        record = self._selected_record()
        if not record or not record.get("localPath"):
            return
        path = self._safe_local_archive(record["localPath"])
        if path is None:
            self._status.setText(
                "The recorded ZIP is unavailable or outside Crucible's player-packs folder; it was not opened."
            )
            self._refresh_actions()
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _delete_selected_zip(self):
        record = self._selected_record()
        if not record:
            return

        path = self._safe_local_archive(record.get("localPath", ""))
        expected_sha256 = record.get("archiveSha256", "")
        if path is None:
            self._status.setText(
                "The selected ZIP is unavailable or outside Crucible's player-packs folder; nothing was deleted."
            )
            self._refresh_actions()
            return
        if not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            self._status.setText("The selected history record has no usable archive checksum; nothing was deleted.")
            return

        sidecar = path.with_suffix(path.suffix + ".crucible-cache.json")
        try:
            if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
                self._status.setText(
                    "The ZIP's cache sidecar is not a regular file; nothing was deleted. Inspect it manually."
                )
                return
            if path.is_symlink() or not path.is_file():
                self._status.setText("The selected ZIP is no longer a regular file; nothing was deleted.")
                self._refresh_actions()
                return
            size_bytes = path.stat().st_size
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                self._status.setText(
                    "The ZIP checksum no longer matches this history entry; nothing was deleted."
                )
                return
        except OSError:
            self._status.setText("Could not verify the selected ZIP safely; nothing was deleted.")
            return

        choice = QMessageBox.question(
            self,
            "Delete this local player ZIP?",
            f"Delete this local file from Crucible's player-packs folder?\n\n"
            f"{path.name}\nSize: {size_bytes:,} bytes\nSHA-256: {expected_sha256}\n\n"
            "This is a manual, irreversible local-file deletion. Make sure you have another copy if you need one. "
            "It does not delete the Gofile upload, revoke the saved link, or verify whether that link still works.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return

        # Recheck the safe folder/path and checksum after the modal confirmation.
        # Do not delete if the target changed while the confirmation was open.
        if self._safe_local_archive(str(path)) is None:
            self._status.setText("The ZIP path changed during confirmation; nothing was deleted.")
            self._refresh_actions()
            return
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_sha256:
                self._status.setText("The ZIP changed during confirmation; nothing was deleted.")
                return
            if sidecar.exists() or sidecar.is_symlink():
                if sidecar.is_symlink() or not sidecar.is_file():
                    self._status.setText("The cache sidecar changed during confirmation; nothing was deleted.")
                    return
                sidecar.unlink()
            path.unlink()
        except OSError:
            self._status.setText(
                "Could not delete the selected ZIP safely. The file may still be present; check the local folder."
            )
            self._refresh_actions()
            return

        history_saved = True
        try:
            self._store.append(
                event="local-zip-deleted",
                instance_name=record.get("instanceName", "Unknown server"),
                archive_sha256=expected_sha256,
                input_fingerprint=record.get("inputFingerprint", ""),
                local_path=str(path),
                size_bytes=size_bytes,
                provider=record.get("provider", ""),
                share_url=record.get("shareUrl", ""),
                upload_status=record.get("uploadStatus", "not-attempted"),
                size_verified=record.get("sizeVerified", False),
                md5_verified=record.get("md5Verified", False),
            )
        except (OSError, ShareHistoryError, TypeError, ValueError):
            history_saved = False

        self._reload()
        if history_saved:
            self._status.setText(
                "Local ZIP and its cache sidecar were deleted by your request. The history event is saved; "
                "any remote Gofile file/link is unchanged."
            )
        else:
            self._status.setText(
                "Local ZIP and its cache sidecar were deleted by your request, but the cleanup event could not be saved. "
                "Any remote Gofile file/link is unchanged."
            )

    def _clear_history(self):
        choice = QMessageBox.question(
            self,
            "Clear local share history?",
            "This forgets Crucible's local history entries only. It does not delete local ZIP files, "
            "remove any previously uploaded Gofile file, or revoke a share link.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if choice != QMessageBox.StandardButton.Yes:
            return
        try:
            self._store.clear()
        except (OSError, ShareHistoryError) as exc:
            self._status.setText(f"Could not clear local share history: {exc}")
            return
        self._reload()


def _format_size(size: int) -> str:
    if size <= 0:
        return "—"
    if size < 1024 * 1024:
        return f"{size:,} B"
    return f"{size / (1024 * 1024):.1f} MiB"


def _format_status(status: str, error_code: str) -> str:
    labels = {
        "not-attempted": "Not uploaded",
        "uploading": "In progress",
        "succeeded": "Succeeded",
        "failed": "Failed",
        "cancelled": "Cancelled",
    }
    value = labels.get(status, status)
    if error_code == "unexpected-upload-error":
        return value + " (details suppressed)"
    return value
