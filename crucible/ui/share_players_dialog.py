"""Review-first, one-click-to-prepare player pack sharing dialog."""

from __future__ import annotations

import json
import os
import threading
import zipfile
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, QTimer, QStandardPaths, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QProgressBar,
    QFileDialog,
    QVBoxLayout,
)

from ..exporters import client_export
from ..sharing.gofile import GofileProvider, UploadCancelled, GofileUploadResult
from ..sharing.provider import UploadProvider
from . import theme
from .thread_lifecycle import connect_thread_cleanup


def _safe_stem(name: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in name)
    text = " ".join(text.split()).strip(" .")
    return (text or "Minecraft Server")[:100]


def _next_pack_path(directory: Path, name: str) -> Path:
    """Choose a clear, non-overwriting vN filename for a local player ZIP."""
    stem = _safe_stem(name)
    version = 1
    while True:
        candidate = directory / f"{stem} - Player Pack - v{version}.zip"
        try:
            # Reserve atomically so two share dialogs cannot choose the same
            # version and overwrite each other's reviewed ZIP.
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            version += 1
            continue
        except OSError:
            raise
        else:
            # The mode is already restricted at open time (umask may make it
            # more restrictive). Avoid a path-based chmod after closing the
            # descriptor; that would create a small replacement race.
            os.close(descriptor)
            return candidate


class _PrepareWorker(QObject):
    done = pyqtSignal(object)

    def __init__(
        self, instance, output: Path, client_source: str, include_resourcepacks: bool,
    ):
        super().__init__()
        self._instance = instance
        self._output = output
        self._client_source = client_source
        self._include_resourcepacks = include_resourcepacks

    def run(self):
        try:
            result = client_export.export(
                self._instance,
                self._output,
                fmt="prism",
                include_config=True,
                client_source=self._client_source,
                include_resourcepacks=self._include_resourcepacks,
            )
        except Exception as exc:
            result = client_export.ExportResult(error=f"Unexpected pack-build error: {exc}")
        if result.ok and os.name == "posix":
            output = Path(result.path)
            try:
                output.chmod(0o600)
            except OSError as exc:
                # Never leave a freshly created archive world-readable if the
                # required owner-only permission couldn't be enforced.
                try:
                    if output == self._output and output.is_file() and not output.is_symlink():
                        output.unlink()
                except OSError:
                    pass
                result = client_export.ExportResult(
                    error=(
                        "The player ZIP was built, but Crucible could not restrict "
                        f"its local file permissions ({exc}). The unprotected artifact "
                        "was removed where possible and no upload is allowed."
                    )
                )
        self.done.emit(result)


class _UploadWorker(QObject):
    progress = pyqtSignal(int, int)
    done = pyqtSignal(object)

    def __init__(
        self,
        provider: UploadProvider,
        path: Path,
        expected_sha256: str,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self._provider = provider
        self._path = path
        self._expected_sha256 = expected_sha256
        self._cancel_event = cancel_event

    def run(self):
        try:
            result = self._provider.upload_file(
                self._path,
                expected_sha256=self._expected_sha256,
                progress_cb=self.progress.emit,
                cancel_event=self._cancel_event,
            )
            self.done.emit({"result": result, "error": "", "cancelled": False})
        except UploadCancelled as exc:
            self.done.emit({"result": None, "error": str(exc), "cancelled": True})
        except Exception as exc:
            self.done.emit({"result": None, "error": str(exc), "cancelled": False})


class SharePlayersDialog(QDialog):
    """Build a Prism ZIP, require an explicit file/risks review, then upload it."""

    def __init__(self, instance, parent=None, manager=None):
        super().__init__(parent)
        self._instance = instance
        self._instance_manager = manager
        self._prepare_thread = None
        self._prepare_worker = None
        self._upload_thread = None
        self._upload_worker = None
        self._cancel_event = None
        self._result = None
        self._archive_path: Path | None = None
        self._archive_sha256 = ""
        self._built_source = ""
        self._upload_result: GofileUploadResult | None = None
        self._started_once = False
        self._phase = "idle"
        self._review_text = ""
        self.setWindowTitle("Share with players")
        self.setMinimumSize(760, 680)
        self.resize(880, 760)
        self.setStyleSheet(theme.STYLESHEET)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(10)

        title = QLabel(f"Share “{self._instance.name}” with players")
        title.setStyleSheet(f"font-size:16px; font-weight:700; color:{theme.TEXT};")
        title.setWordWrap(True)
        root.addWidget(title)

        intro = QLabel(
            "Crucible uses a matching, working Prism client profile to build one "
            "Prism-compatible ZIP, shows the exact files/privacy/licensing risks, "
            "and uploads only after your approval. The ZIP stays on this computer "
            "if the upload fails."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{theme.SUBTEXT};")
        root.addWidget(intro)

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Required working Prism client:"))
        self._source = QLineEdit()
        self._source.setPlaceholderText("Choose a working Prism instance with mmc-pack.json")
        suggested = str(getattr(self._instance, "prism_source", "") or "")
        suggested_path = Path(suggested).expanduser() if suggested else None
        if suggested_path and suggested_path.is_dir() and not suggested_path.is_symlink():
            self._source.setText(str(suggested_path))
        source_row.addWidget(self._source, 1)
        self._browse_source = QPushButton("Browse…")
        self._browse_source.clicked.connect(self._pick_source)
        source_row.addWidget(self._browse_source)
        root.addLayout(source_row)

        self._resourcepacks = QCheckBox(
            "Include resource packs from this Prism profile (off by default)"
        )
        self._resourcepacks.setChecked(False)
        self._resourcepacks.setToolTip(
            "Only enable this if the modpack needs a resource pack from this profile. "
            "All included resource packs will be listed for review and their licenses are not auto-verified."
        )
        root.addWidget(self._resourcepacks)

        self._remember_source = QCheckBox(
            "Remember this client profile for future shares after it passes validation"
        )
        self._remember_source.setChecked(True)
        self._remember_source.setEnabled(self._instance_manager is not None)
        root.addWidget(self._remember_source)

        self._use_api_token = QCheckBox(
            "Use an owner Gofile API token (optional; never saved)"
        )
        self._use_api_token.toggled.connect(self._toggle_api_token)
        root.addWidget(self._use_api_token)
        self._api_token = QLineEdit()
        self._api_token.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_token.setPlaceholderText(
            "Only if Gofile declines guest upload; this token is never saved or sent to players"
        )
        self._api_token.setAccessibleName("Optional owner Gofile API token")
        self._api_token.setToolTip(
            "Kept only in this dialog's memory for the current upload/retry. "
            "It is not written to the registry, manifest, logs, ZIP or player instructions."
        )
        self._api_token.hide()
        root.addWidget(self._api_token)

        actions = QHBoxLayout()
        self._prepare_btn = QPushButton("Build and review ZIP")
        self._prepare_btn.setObjectName("PrimaryButton")
        self._prepare_btn.clicked.connect(self._prepare_pack)
        actions.addWidget(self._prepare_btn)
        self._open_local_btn = QPushButton("Open local ZIP")
        self._open_local_btn.setEnabled(False)
        self._open_local_btn.clicked.connect(self._open_local)
        actions.addWidget(self._open_local_btn)
        actions.addStretch()
        root.addLayout(actions)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setTextVisible(True)
        self._progress.setFormat("Preparing player pack…")
        self._progress.hide()
        root.addWidget(self._progress)

        self._status = QLabel("Nothing is uploaded until you approve the review below.")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{theme.SUBTEXT}; font-size:11px;")
        root.addWidget(self._status)

        self._review = QPlainTextEdit()
        self._review.setReadOnly(True)
        self._review.setAccessibleName("Included files and pre-upload review")
        self._review.setPlaceholderText(
            "The exact files, per-file sizes and hashes, privacy findings, and license notes appear here after the ZIP is built."
        )
        self._review.setMinimumHeight(260)
        root.addWidget(self._review, 1)

        self._review_ack = QCheckBox(
            "I reviewed the exact included file list and privacy-scan findings."
        )
        self._review_ack.stateChanged.connect(self._refresh_upload_enabled)
        self._review_ack.setEnabled(False)
        root.addWidget(self._review_ack)

        self._license_ack = QCheckBox(
            "I checked mod/config distribution rights and accept responsibility for sharing these files."
        )
        self._license_ack.stateChanged.connect(self._refresh_upload_enabled)
        self._license_ack.setEnabled(False)
        root.addWidget(self._license_ack)

        self._public_ack = QCheckBox(
            "I understand anyone with the Gofile link can access it, and free/guest storage may be ephemeral."
        )
        self._public_ack.stateChanged.connect(self._refresh_upload_enabled)
        self._public_ack.setEnabled(False)
        root.addWidget(self._public_ack)

        link_row = QHBoxLayout()
        link_row.addWidget(QLabel("Gofile link:"))
        self._link = QLineEdit()
        self._link.setReadOnly(True)
        self._link.setPlaceholderText("Appears after a successful upload")
        link_row.addWidget(self._link, 1)
        root.addLayout(link_row)

        link_actions = QHBoxLayout()
        self._upload_btn = QPushButton("Upload to Gofile")
        self._upload_btn.setObjectName("PrimaryButton")
        self._upload_btn.setEnabled(False)
        self._upload_btn.clicked.connect(self._start_upload)
        link_actions.addWidget(self._upload_btn)
        self._cancel_upload_btn = QPushButton("Cancel upload")
        self._cancel_upload_btn.setEnabled(False)
        self._cancel_upload_btn.clicked.connect(self._cancel_upload)
        link_actions.addWidget(self._cancel_upload_btn)
        self._copy_link_btn = QPushButton("Copy link")
        self._copy_link_btn.setEnabled(False)
        self._copy_link_btn.clicked.connect(self._copy_link)
        link_actions.addWidget(self._copy_link_btn)
        self._copy_instructions_btn = QPushButton("Copy player instructions")
        self._copy_instructions_btn.setEnabled(False)
        self._copy_instructions_btn.clicked.connect(self._copy_instructions)
        link_actions.addWidget(self._copy_instructions_btn)
        self._open_link_btn = QPushButton("Open link")
        self._open_link_btn.setEnabled(False)
        self._open_link_btn.clicked.connect(self._open_link)
        link_actions.addWidget(self._open_link_btn)
        link_actions.addStretch()
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        link_actions.addWidget(self._close_btn)
        root.addLayout(link_actions)

    def showEvent(self, event):
        super().showEvent(event)
        if self._started_once:
            return
        self._started_once = True
        # If a valid source is already configured, the happy path starts building
        # as soon as the Share dialog appears. First use lets the owner pick it.
        source = Path(self._source.text()).expanduser() if self._source.text().strip() else None
        if source and source.is_dir() and not source.is_symlink() and (source / "mmc-pack.json").is_file():
            has_resourcepacks = False
            for layout in (".minecraft", "minecraft"):
                layout_root = source / layout
                if layout_root.is_symlink():
                    self._status.setText(
                        "The selected Prism profile has a linked game directory. "
                        "Choose a real installed profile before sharing."
                    )
                    return
                candidate = layout_root / "resourcepacks"
                if not candidate.is_symlink() and candidate.is_dir():
                    try:
                        has_resourcepacks = any(candidate.iterdir())
                    except OSError:
                        has_resourcepacks = True
                    if has_resourcepacks:
                        break
            if has_resourcepacks:
                self._status.setText(
                    "This profile contains resource packs. They are excluded by default; "
                    "check the resource-pack option only if they are required, then build the ZIP for review."
                )
                return
            QTimer.singleShot(0, self._prepare_pack)

    def _toggle_api_token(self, enabled: bool):
        self._api_token.setVisible(enabled)
        self._api_token.setEnabled(enabled)
        if not enabled:
            self._api_token.clear()

    def _pick_source(self):
        start = self._source.text().strip() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(self, "Choose known-good Prism instance", start)
        if selected:
            self._source.setText(selected)

    def _prepare_pack(self):
        if self._phase in {"preparing", "uploading"}:
            return
        raw_source = self._source.text().strip()
        if not raw_source:
            self._prepare_failed(
                "Choose the working Prism profile you use to join this server. "
                "Without it Crucible cannot account for client-only mods, so this sharing flow will not create an incomplete server-only pack."
            )
            return
        source = Path(raw_source).expanduser()
        source_pack = source / "mmc-pack.json"
        if (
            source.is_symlink()
            or not source.is_dir()
            or source_pack.is_symlink()
            or not source_pack.is_file()
        ):
            self._prepare_failed(
                "The selected client profile is not a real, installed Prism/MultiMC instance. "
                "Choose its instance folder containing a regular mmc-pack.json."
            )
            return
        self._result = None
        self._archive_path = None
        self._archive_sha256 = ""
        self._upload_result = None
        self._link.clear()
        self._copy_link_btn.setEnabled(False)
        self._copy_instructions_btn.setEnabled(False)
        self._open_link_btn.setEnabled(False)
        self._open_local_btn.setEnabled(False)
        self._upload_btn.setEnabled(False)
        # Freeze the profile that actually contributed to this build. The
        # source field becomes editable again after preparation so the owner can
        # rebuild, but the review must keep showing the source used for this ZIP.
        self._built_source = str(source.resolve())
        self._phase = "preparing"
        self._prepare_btn.setEnabled(False)
        self._browse_source.setEnabled(False)
        self._source.setEnabled(False)
        self._remember_source.setEnabled(False)
        self._review_ack.setEnabled(False)
        self._license_ack.setEnabled(False)
        self._public_ack.setEnabled(False)
        self._upload_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setFormat("Preparing player pack…")
        self._progress.show()
        self._status.setText("Checking versions, building ZIP, validating the archive and scanning configuration…")

        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        out_root = (Path(base) if base else Path.home() / ".local" / "share" / "crucible") / "player-packs"
        try:
            if out_root.is_symlink():
                raise OSError("the player-pack folder itself is a symbolic link")
            out_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if out_root.is_symlink() or not out_root.is_dir():
                raise OSError("the player-pack folder is not a real directory")
            if os.name == "posix":
                out_root.chmod(0o700)
        except OSError as exc:
            self._prepare_failed(f"Could not create or secure the local player-pack folder: {exc}")
            return
        try:
            out_path = _next_pack_path(out_root, self._instance.name)
        except OSError as exc:
            self._prepare_failed(f"Could not reserve a unique local player-pack filename: {exc}")
            return
        self._archive_path = out_path

        self._prepare_thread = QThread(self)
        self._prepare_worker = _PrepareWorker(
            self._instance,
            out_path,
            self._source.text().strip(),
            self._resourcepacks.isChecked(),
        )
        self._prepare_worker.moveToThread(self._prepare_thread)
        self._prepare_thread.started.connect(self._prepare_worker.run)
        self._prepare_worker.done.connect(self._on_pack_ready)
        self._prepare_worker.done.connect(self._prepare_thread.quit)
        self._prepare_worker.done.connect(self._prepare_worker.deleteLater)
        connect_thread_cleanup(self._prepare_thread, self._prepare_finished)
        self._prepare_thread.start()

    def _prepare_failed(self, message: str):
        self._phase = "error"
        self._result = None
        self._archive_path = None
        self._archive_sha256 = ""
        self._upload_result = None
        self._link.clear()
        self._review.clear()
        self._open_local_btn.setEnabled(False)
        self._copy_link_btn.setEnabled(False)
        self._copy_instructions_btn.setEnabled(False)
        self._open_link_btn.setEnabled(False)
        self._upload_btn.setEnabled(False)
        self._review_ack.setChecked(False)
        self._license_ack.setChecked(False)
        self._public_ack.setChecked(False)
        self._review_ack.setEnabled(False)
        self._license_ack.setEnabled(False)
        self._public_ack.setEnabled(False)
        self._progress.hide()
        self._status.setText(message)
        self._prepare_btn.setText("Build and review ZIP")
        self._prepare_btn.setEnabled(True)
        self._browse_source.setEnabled(True)
        self._source.setEnabled(True)
        self._remember_source.setEnabled(self._instance_manager is not None)

    def _prepare_finished(self):
        self._prepare_thread = None
        self._prepare_worker = None

    def _on_pack_ready(self, result):
        self._progress.hide()
        if not result.ok:
            # An early validation error can leave the zero-byte filename
            # reservation in place. Remove only that empty placeholder.
            reserved = self._archive_path
            if reserved is not None:
                try:
                    if reserved.is_file() and not reserved.is_symlink() and reserved.stat().st_size == 0:
                        reserved.unlink()
                except OSError:
                    pass
            self._result = None
            self._archive_path = None
            self._archive_sha256 = ""
            self._phase = "error"
            self._status.setText("Pack preparation failed. No upload was started.")
            self._review.setPlainText(result.error or "Pack preparation failed.")
            self._prepare_btn.setEnabled(True)
            self._browse_source.setEnabled(True)
            self._source.setEnabled(True)
            self._remember_source.setEnabled(self._instance_manager is not None)
            return

        self._result = result
        self._archive_path = Path(result.path)
        self._archive_sha256 = result.archive_sha256
        self._phase = "review"
        self._open_local_btn.setEnabled(True)

        try:
            with zipfile.ZipFile(self._archive_path, "r") as archive:
                manifest = json.loads(archive.read(result.manifest_path).decode("utf-8"))
        except (OSError, zipfile.BadZipFile, KeyError, ValueError) as exc:
            self._result = None
            self._phase = "error"
            self._status.setText(f"Manifest verification failed; nothing can be uploaded: {exc}")
            self._prepare_btn.setEnabled(True)
            self._browse_source.setEnabled(True)
            self._source.setEnabled(True)
            return

        lines = [
            f"Local ZIP: {self._archive_path}",
            f"Size: {self._archive_path.stat().st_size:,} bytes",
            f"Archive SHA-256: {self._archive_sha256}",
            f"Validated Prism client source used: {self._built_source or '(unknown)'}",
            f"Files listed in manifest: {len(manifest.get('files', []))}",
            f"Manifest path: {result.manifest_path} (the manifest itself is included in the ZIP; the archive checksum covers it)",
            "",
            "EXACT INCLUDED PAYLOAD FILES (path · bytes · SHA-256):",
        ]
        jar_rows = []
        resource_pack_names = set()
        for row in manifest.get("files", []):
            row_path = str(row.get("path", ""))
            lines.append(f"  {row_path} · {row.get('size_bytes', 0):,} · {row.get('sha256', '')}")
            if row_path.lower().endswith(".jar"):
                jar_rows.append(row_path)
            marker = "/.minecraft/resourcepacks/"
            if marker in row_path:
                tail = row_path.split(marker, 1)[1]
                if tail:
                    resource_pack_names.add(tail.split("/", 1)[0])
        lines.extend([
            "",
            "DISTRIBUTION RIGHTS (not automatically verified):",
        ])
        if jar_rows:
            lines.extend(
                f"  - UNVERIFIED JAR — check the author's license/distribution terms: {jar_path}"
                for jar_path in jar_rows
            )
        else:
            lines.append("  - No mod JAR files were included.")
        if resource_pack_names:
            lines.extend(
                f"  - UNVERIFIED RESOURCE PACK — check distribution terms: {pack_name}"
                for pack_name in sorted(resource_pack_names)
            )
        else:
            lines.append("  - No client resource packs were included.")
        privacy_review = manifest.get("privacyReview", {})
        lines.extend([
            "",
            "PRIVACY SCAN:",
            f"  Scope: {privacy_review.get('scope', 'Config/KubeJS text scan only; other payloads may not be inspected.')}",
            f"  Config/KubeJS text-scan coverage complete: {privacy_review.get('coverageComplete', False)}",
        ])
        privacy_warnings = privacy_review.get("warnings", [])
        if privacy_warnings:
            lines.extend(f"  - {warning}" for warning in privacy_warnings)
        else:
            lines.append("  - No heuristic matches were found in scanned text config. This is not proof that no secrets exist.")
        privacy_warning_set = {str(warning) for warning in privacy_warnings}
        other_warnings = [
            warning for warning in result.warnings
            if str(warning) not in privacy_warning_set
        ]
        if other_warnings:
            lines.extend(["", "EXPORT / COMPATIBILITY / DISTRIBUTION WARNINGS:"])
            lines.extend(f"  - {warning}" for warning in other_warnings)
        lines.extend([
            "",
            "REVIEW NOTES:",
            "  - Only the files listed above plus this generated manifest are in the ZIP.",
            "  - Crucible cannot determine all mod redistribution rights from JAR contents; each JAR is marked unverified until you check it.",
            "  - The Gofile share link can be passed to anyone. Free/guest storage may be ephemeral; account retention depends on the selected plan.",
            "  - Crucible discards any guest-management token returned by Gofile. The optional owner API token is kept in memory only and is never saved.",
            "  - The Gofile upload API is beta; no fixed quota or permanent-retention promise is made.",
            "  - Fix any suspicious config in the source and rebuild before uploading if you are unsure.",
        ])
        self._review_text = "\n".join(lines)
        self._review.setPlainText(self._review_text)
        self._status.setText(
            "ZIP ready. Read the complete file list and warnings, then tick all three acknowledgements to enable upload."
        )

        # If a fully validated source was selected, persist it now through the
        # normal registry writer. A failed export never saves the source path.
        self._remember_source_after_success()

        self._review_ack.setText(
            "I reviewed every listed file and privacy finding, and understand the scanner cannot guarantee a secret-free pack."
        )
        self._license_ack.setText(
            f"I checked distribution rights for {len(jar_rows)} bundled JAR(s), "
            f"{len(resource_pack_names)} resource pack(s), and included configuration."
        )
        self._review_ack.setChecked(False)
        self._license_ack.setChecked(False)
        self._public_ack.setChecked(False)
        self._review_ack.setEnabled(True)
        self._license_ack.setEnabled(True)
        self._public_ack.setEnabled(True)
        self._refresh_upload_enabled()
        self._prepare_btn.setText("Rebuild ZIP")
        self._prepare_btn.setEnabled(True)
        self._browse_source.setEnabled(True)
        self._source.setEnabled(True)
        self._remember_source.setEnabled(self._instance_manager is not None)

    def _remember_source_after_success(self):
        if not self._remember_source.isChecked() or self._instance_manager is None:
            return
        raw = self._source.text().strip()
        if not raw:
            return
        source = Path(raw).expanduser()
        pack_file = source / "mmc-pack.json"
        if source.is_symlink() or not source.is_dir() or pack_file.is_symlink() or not pack_file.is_file():
            return
        try:
            canonical = str(source.resolve(strict=True))
        except OSError:
            return
        previous = getattr(self._instance, "prism_source", "") or ""
        if previous:
            try:
                if str(Path(previous).expanduser().resolve()) == canonical:
                    return
            except OSError:
                pass
        try:
            self._instance.prism_source = canonical
            self._instance_manager.update_instance(self._instance)
        except Exception:
            self._instance.prism_source = previous
            self._status.setText(
                "ZIP ready, but Crucible could not save the Prism source path. The archive is still available for review."
            )

    def _refresh_upload_enabled(self, *_):
        approved = (
            self._result is not None
            and self._phase in {"review", "upload-error"}
            and self._review_ack.isChecked()
            and self._license_ack.isChecked()
            and self._public_ack.isChecked()
            and self._archive_path is not None
            and self._archive_path.is_file()
        )
        self._upload_btn.setEnabled(bool(approved))

    def _start_upload(self):
        if not self._upload_btn.isEnabled() or self._result is None or self._archive_path is None:
            return
        self._phase = "uploading"
        self._cancel_event = threading.Event()
        self._upload_btn.setEnabled(False)
        self._upload_btn.setText("Uploading…")
        self._cancel_upload_btn.setEnabled(True)
        self._prepare_btn.setEnabled(False)
        self._browse_source.setEnabled(False)
        self._source.setEnabled(False)
        self._remember_source.setEnabled(False)
        self._use_api_token.setEnabled(False)
        self._api_token.setEnabled(False)
        self._progress.setRange(0, 1000)
        self._progress.setValue(0)
        self._progress.setFormat("Uploading 0 bytes")
        self._progress.show()
        self._status.setText("Verifying the local ZIP checksum, then streaming it to Gofile…")

        self._upload_thread = QThread(self)
        self._upload_worker = _UploadWorker(
            GofileProvider(api_token=self._api_token.text().strip()),
            self._archive_path,
            self._archive_sha256,
            self._cancel_event,
        )
        self._upload_worker.moveToThread(self._upload_thread)
        self._upload_thread.started.connect(self._upload_worker.run)
        self._upload_worker.progress.connect(self._on_upload_progress)
        self._upload_worker.done.connect(self._on_upload_done)
        self._upload_worker.done.connect(self._upload_thread.quit)
        self._upload_worker.done.connect(self._upload_worker.deleteLater)
        connect_thread_cleanup(self._upload_thread, self._upload_finished)
        self._upload_thread.start()

    def _on_upload_progress(self, completed: int, total: int):
        if total > 0:
            ratio = max(0.0, min(1.0, completed / total))
            self._progress.setValue(round(ratio * 1000))
            self._progress.setFormat(f"Uploaded {completed:,} / {total:,} bytes · %p%")

    def _on_upload_done(self, outcome: dict):
        self._progress.hide()
        self._cancel_upload_btn.setEnabled(False)
        self._prepare_btn.setEnabled(True)
        self._browse_source.setEnabled(True)
        self._source.setEnabled(True)
        self._remember_source.setEnabled(self._instance_manager is not None)
        self._use_api_token.setEnabled(True)
        self._api_token.setEnabled(self._use_api_token.isChecked())
        if outcome.get("cancelled"):
            self._phase = "review"
            self._status.setText(
                "Upload cancelled. The local ZIP is preserved. Gofile may have received an incomplete or unconfirmed copy; Crucible cannot retract it. Review and retry the same local ZIP only when ready."
            )
            self._upload_btn.setText("Upload to Gofile")
            self._refresh_upload_enabled()
            return
        if outcome.get("error"):
            self._phase = "upload-error"
            self._status.setText(
                "Upload was not verified. The local ZIP is preserved. A connection failure after sending can leave an unconfirmed remote copy; a retry may create another link. "
                + outcome["error"]
            )
            self._upload_btn.setText("Retry upload (same ZIP)")
            self._refresh_upload_enabled()
            return

        result: GofileUploadResult = outcome["result"]
        self._api_token.clear()
        self._use_api_token.setChecked(False)
        self._upload_result = result
        self._phase = "done"
        self._link.setText(result.download_url)
        self._link.setCursorPosition(0)
        # The owner has explicitly approved the public upload; copying the
        # resulting link completes the low-friction share workflow.
        QApplication.clipboard().setText(result.download_url)
        verification = []
        if result.size_verified:
            verification.append("remote size matches")
        else:
            verification.append("remote size not provided")
        if result.md5_verified:
            verification.append("remote MD5 matches")
        else:
            verification.append("remote MD5 not provided")
        self._status.setText(
            f"Upload complete: Gofile returned a new share link and it was copied to the clipboard. "
            f"Link validity was not independently re-fetched. Local ZIP retained; "
            f"SHA-256: {result.local_sha256}. "
            + "; ".join(verification)
            + ". Any guest-management token in the response is discarded; the owner API token is not retained. Keep the link and local ZIP; free/guest storage may be ephemeral."
        )
        self._copy_link_btn.setEnabled(True)
        self._copy_instructions_btn.setEnabled(True)
        self._open_link_btn.setEnabled(True)
        self._upload_btn.setText("Upload complete")
        self._upload_btn.setEnabled(False)
        self._public_ack.setEnabled(False)
        self._license_ack.setEnabled(False)
        self._review_ack.setEnabled(False)

    def _upload_finished(self):
        self._upload_thread = None
        self._upload_worker = None
        self._cancel_event = None

    def _cancel_upload(self):
        if self._cancel_event:
            self._cancel_event.set()
            self._cancel_upload_btn.setEnabled(False)
            self._status.setText("Cancellation requested. Waiting for the current network write to stop…")

    def _copy_link(self):
        if self._upload_result:
            QApplication.clipboard().setText(self._upload_result.download_url)
            self._status.setText("Copied the Gofile link to the clipboard.")

    def _copy_instructions(self):
        if not self._upload_result:
            return
        instructions = (
            "1. Download the player ZIP from the Gofile link.\n"
            "2. Open Prism Launcher and drag the ZIP onto its window, or use "
            "Add Instance → Import from ZIP.\n"
            "3. Confirm the import and let Prism install any required Minecraft, "
            "loader, libraries and assets.\n"
            "4. Sign in with your own Minecraft account if Prism asks.\n"
            "5. Ask the server host for the server address, then connect in Minecraft.\n\n"
            "You do not need a command line or an API key."
        )
        QApplication.clipboard().setText(instructions)
        self._status.setText("Copied player instructions to the clipboard.")

    def _open_local(self):
        if self._archive_path and self._archive_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._archive_path)))

    def _open_link(self):
        if self._upload_result:
            QDesktopServices.openUrl(QUrl(self._upload_result.download_url))

    def reject(self):
        if self._phase in {"preparing", "uploading"}:
            if self._phase == "uploading":
                self._cancel_upload()
            self._status.setText("The operation is still running. Wait for it to stop before closing.")
            return
        self._api_token.clear()
        super().reject()

    def closeEvent(self, event):
        if self._phase in {"preparing", "uploading"}:
            if self._phase == "uploading":
                self._cancel_upload()
            self._status.setText("The operation is still running. Wait for it to stop before closing.")
            event.ignore()
            return
        self._api_token.clear()
        super().closeEvent(event)
