"""Review-first, one-click-to-prepare player pack sharing dialog."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import zipfile
from pathlib import Path

from ..mods import distribution_catalog
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
from ..sharing.gofile import GofileProvider, GofileUploadError, UploadCancelled, GofileUploadResult
from ..sharing.provider import UploadProvider
from . import theme
from .thread_lifecycle import connect_thread_cleanup


def _safe_stem(name: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in " -_." else "_" for ch in name)
    text = " ".join(text.split()).strip(" .")
    return (text or "Minecraft Server")[:100]


def _validate_review_manifest(manifest):
    """Fail closed on malformed review metadata before enabling public upload."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest root is not a JSON object")

    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest file list is missing or malformed")
    file_rows = {}
    for row in files:
        if not isinstance(row, dict):
            raise ValueError("manifest contains a malformed file entry")
        row_path = row.get("path")
        size = row.get("size_bytes")
        digest = row.get("sha256")
        if (
            not isinstance(row_path, str)
            or not row_path
            or any(ord(char) < 32 or ord(char) == 127 for char in row_path)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
            or row_path in file_rows
        ):
            raise ValueError("manifest contains invalid or duplicate file path, size, or SHA-256 metadata")
        file_rows[row_path] = row

    privacy_review = manifest.get("privacyReview")
    if not isinstance(privacy_review, dict):
        raise ValueError("manifest privacy-review section is missing or malformed")
    if not isinstance(privacy_review.get("coverageComplete"), bool):
        raise ValueError("manifest privacy-scan coverage status is missing or malformed")
    warnings = privacy_review.get("warnings")
    if not isinstance(warnings, list) or any(not isinstance(item, str) for item in warnings):
        raise ValueError("manifest privacy-scan warnings are missing or malformed")
    scope = privacy_review.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise ValueError("manifest privacy-scan scope is malformed")

    distribution_keys = {"distributionCatalog", "distributionReview", "requiredDownloads"}
    present_distribution_keys = distribution_keys.intersection(manifest)
    if present_distribution_keys != distribution_keys:
        raise ValueError("manifest must include complete distribution-catalog review sections")
    if present_distribution_keys:
        catalog = manifest.get("distributionCatalog")
        if (
            not isinstance(catalog, dict)
            or not isinstance(catalog.get("version"), str)
            or not catalog["version"]
            or not isinstance(catalog.get("revision"), str)
            or len(catalog["revision"]) != 64
            or any(char not in "0123456789abcdef" for char in catalog["revision"])
        ):
            raise ValueError("manifest distribution-catalog metadata is malformed")
        if catalog["revision"] != distribution_catalog.catalog_revision():
            raise ValueError("the curated distribution catalog changed after this ZIP was prepared; rebuild and review it again")

        review_rows = manifest.get("distributionReview")
        downloads = manifest.get("requiredDownloads")
        if not isinstance(review_rows, list) or not isinstance(downloads, list):
            raise ValueError("manifest distribution review/download lists are malformed")
        review_by_path = {}
        for row in review_rows:
            if not isinstance(row, dict):
                raise ValueError("manifest contains a malformed distribution record")
            row_path = row.get("path")
            filename = row.get("filename")
            digest = row.get("sha256")
            decision = row.get("decision")
            bundled = row.get("bundled")
            if (
                not isinstance(row_path, str)
                or not row_path
                or row_path in review_by_path
                or not isinstance(filename, str)
                or not filename
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or decision not in {"unverified", "permitted", "prohibited", "conditional"}
                or not isinstance(bundled, bool)
            ):
                raise ValueError("manifest contains invalid or duplicate distribution-review metadata")
            if (decision == "prohibited") == bundled:
                raise ValueError("manifest distribution decision conflicts with whether the JAR is bundled")
            if not row_path.lower().endswith(".jar"):
                raise ValueError("distribution-review record is not for a JAR artifact")
            client_export._validate_archive_member_name(row_path)
            if decision != "unverified":
                if not isinstance(row.get("verifiedOn"), str) or not row["verifiedOn"] or not isinstance(row.get("basis"), str) or not row["basis"]:
                    raise ValueError("curated distribution decision is missing its review date or basis")
            for field_name in ("projectUrl", "evidenceUrl", "licenseUrl", "alternativeDownloadUrl"):
                field_value = row.get(field_name, "")
                if not isinstance(field_value, str):
                    raise ValueError(f"manifest distribution field {field_name!r} is malformed")
                if field_value:
                    distribution_catalog.validate_https_url(field_value, required=True)
                elif decision != "unverified" and field_name in {"projectUrl", "evidenceUrl"}:
                    raise ValueError(f"curated distribution record is missing {field_name}")
            conditions = row.get("conditions", [])
            if decision == "conditional" and not conditions:
                raise ValueError("conditional distribution record is missing its conditions")
            if not isinstance(conditions, list) or any(not isinstance(item, str) for item in conditions):
                raise ValueError("manifest distribution conditions are malformed")
            for text_field in ("modId", "modVersion", "license", "notes"):
                text_value = row.get(text_field, "")
                if not isinstance(text_value, str) or len(text_value) > 8192:
                    raise ValueError(f"manifest distribution field {text_field!r} is malformed")
            basis = row.get("basis", "")
            if not isinstance(basis, str) or len(basis) > 8192:
                raise ValueError("manifest distribution basis is malformed")
            if bundled:
                file_row = file_rows.get(row_path)
                if file_row is None or file_row.get("sha256", "").lower() != digest:
                    raise ValueError("bundled JAR distribution record does not match the payload manifest")
            elif decision != "prohibited":
                raise ValueError("only a prohibited artifact may be excluded by the distribution catalog")
            review_by_path[row_path] = row

        for path, row in file_rows.items():
            if path.lower().endswith(".jar") and path not in review_by_path:
                raise ValueError("manifest is missing a distribution decision for a bundled JAR")

        required_by_path = {}
        for item in downloads:
            if not isinstance(item, dict):
                raise ValueError("manifest contains a malformed required-download entry")
            row_path = item.get("path")
            digest = item.get("sha256")
            filename = item.get("filename")
            if (
                not isinstance(row_path, str)
                or row_path in required_by_path
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or not isinstance(filename, str)
                or not filename
            ):
                raise ValueError("manifest required-download metadata is malformed")
            review = review_by_path.get(row_path)
            if (
                review is None
                or review.get("decision") != "prohibited"
                or review.get("bundled") is not False
                or review.get("sha256") != digest
                or row_path in file_rows
                or filename != review.get("filename")
                or item.get("modId", "") != review.get("modId", "")
                or item.get("modVersion", "") != review.get("modVersion", "")
                or item.get("verifiedOn") != review.get("verifiedOn")
                or item.get("notes", "") != review.get("notes", "")
                or item.get("projectUrl") != review.get("projectUrl")
                or item.get("evidenceUrl") != review.get("evidenceUrl")
                or item.get("alternativeDownloadUrl") != review.get("alternativeDownloadUrl")
            ):
                raise ValueError("required-download entry does not match a prohibited distribution record")
            for field_name in ("projectUrl", "evidenceUrl", "alternativeDownloadUrl"):
                field_value = item.get(field_name)
                distribution_catalog.validate_https_url(field_value, required=True)
            for text_field in ("modId", "modVersion", "notes"):
                text_value = item.get(text_field, "")
                if not isinstance(text_value, str) or len(text_value) > 8192:
                    raise ValueError(f"required-download field {text_field!r} is malformed")
            verified_on = item.get("verifiedOn")
            if not isinstance(verified_on, str) or not verified_on:
                raise ValueError("required-download evidence date is missing")
            required_by_path[row_path] = item
        for row_path, row in review_by_path.items():
            if row.get("decision") == "prohibited" and row_path not in required_by_path:
                raise ValueError("prohibited JAR is missing its required alternative-download instructions")

    return manifest


def _verify_review_archive(archive_path: Path, expected_archive_sha256: str, manifest_path: str, manifest: dict) -> None:
    """Ensure the owner reviews the exact archive and payload described by the manifest."""
    if (
        not isinstance(expected_archive_sha256, str)
        or len(expected_archive_sha256) != 64
        or any(char not in "0123456789abcdef" for char in expected_archive_sha256)
    ):
        raise ValueError("the expected archive SHA-256 is missing or invalid")
    actual_archive_sha256 = client_export._sha256_file(archive_path)
    if actual_archive_sha256 != expected_archive_sha256:
        raise ValueError("the local ZIP changed after preparation; rebuild and review it again")
    if manifest.get("manifestPath") != manifest_path:
        raise ValueError("the manifest path in the ZIP does not match the build result")
    client_export._validate_archive_member_name(manifest_path)
    expected_files = {row["path"]: row for row in manifest["files"]}
    with zipfile.ZipFile(archive_path, "r") as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos if not info.is_dir()]
        if len(names) != len(set(names)):
            raise ValueError("the local ZIP contains duplicate archive paths")
        for info in infos:
            if not info.is_dir():
                client_export._validate_archive_member_name(info.filename)
        if manifest_path not in names or set(names) != set(expected_files) | {manifest_path}:
            raise ValueError("the local ZIP members do not exactly match its manifest")
        for name, row in expected_files.items():
            info = archive.getinfo(name)
            if info.file_size != row["size_bytes"]:
                raise ValueError(f"the local ZIP file size differs from its manifest: {name}")
            digest = hashlib.sha256()
            count = 0
            with archive.open(info, "r") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    count += len(chunk)
                    digest.update(chunk)
            if count != row["size_bytes"] or digest.hexdigest() != row["sha256"].lower():
                raise ValueError(f"the local ZIP payload hash differs from its manifest: {name}")
    # Reading each entry to EOF above also checks its ZIP CRC.


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


def _cache_metadata_path(archive_path: Path) -> Path:
    return archive_path.with_suffix(archive_path.suffix + ".crucible-cache.json")


def _is_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _write_cache_metadata(result: client_export.ExportResult) -> None:
    """Atomically persist non-secret cache metadata beside a private local ZIP."""
    if not result.ok or not result.input_fingerprint or not result.archive_sha256:
        return
    archive_path = Path(result.path)
    if archive_path.is_symlink() or not archive_path.is_file():
        return
    sidecar = _cache_metadata_path(archive_path)
    if sidecar.is_symlink():
        return
    if len(result.skipped) > 1000 or len(result.warnings) > 1000:
        return
    if any(not isinstance(item, str) or len(item) > 8192 for item in result.warnings):
        return
    if any(not isinstance(item, str) or len(item) > 2048 for item in result.skipped):
        return
    metadata = {
        "schemaVersion": 1,
        "inputFingerprint": result.input_fingerprint,
        "archiveSha256": result.archive_sha256,
        "format": result.fmt,
        "manifestPath": result.manifest_path,
        "modCount": result.mod_count,
        "skipped": list(result.skipped),
        "warnings": list(result.warnings),
    }
    serialized_metadata = json.dumps(metadata, ensure_ascii=True, separators=(",", ":")) + "\n"
    if len(serialized_metadata.encode("utf-8")) > 1024 * 1024:
        return
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".crucible-cache-", suffix=".tmp", dir=str(archive_path.parent)
        )
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized_metadata)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, sidecar)
        temporary_name = None
    except OSError:
        # The ZIP remains usable; absent metadata merely means a future run
        # will rebuild instead of trusting a cache entry it cannot validate.
        return
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _verified_cached_pack(archive_path: Path, expected_fingerprint: str):
    """Return a reusable Prism ZIP only after full archive and manifest validation."""
    sidecar = _cache_metadata_path(archive_path)
    try:
        if (
            archive_path.is_symlink()
            or not archive_path.is_file()
            or archive_path.stat().st_size <= 0
            or sidecar.is_symlink()
            or not sidecar.is_file()
            or sidecar.stat().st_size > 1024 * 1024
        ):
            return None
        if os.name == "posix" and (archive_path.stat().st_mode & 0o077 or sidecar.stat().st_mode & 0o077):
            return None
        metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict) or metadata.get("schemaVersion") != 1:
            return None
        if metadata.get("inputFingerprint") != expected_fingerprint or metadata.get("format") != "prism":
            return None
        expected_archive_sha = metadata.get("archiveSha256")
        manifest_path = metadata.get("manifestPath")
        mod_count = metadata.get("modCount")
        warnings = metadata.get("warnings")
        skipped = metadata.get("skipped")
        if (
            not _is_sha256(expected_archive_sha)
            or not isinstance(manifest_path, str)
            or not manifest_path
            or isinstance(mod_count, bool)
            or not isinstance(mod_count, int)
            or mod_count < 0
            or not isinstance(warnings, list)
            or any(not isinstance(item, str) for item in warnings)
            or not isinstance(skipped, list)
            or any(not isinstance(item, str) for item in skipped)
        ):
            return None
        actual_archive_sha = client_export._sha256_file(archive_path)
        if actual_archive_sha != expected_archive_sha:
            return None

        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            member_names = [item.filename for item in infos if not item.is_dir()]
            all_names = [item.filename for item in infos]
            if len(all_names) != len(set(all_names)):
                return None
            for info in infos:
                if not info.is_dir():
                    client_export._validate_archive_member_name(info.filename)
            if manifest_path not in member_names:
                return None
            manifest = json.loads(archive.read(manifest_path).decode("utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("manifestPath") != manifest_path
                or manifest.get("format") != "prism"
                or manifest.get("inputFingerprint") != expected_fingerprint
                or not isinstance(manifest.get("files"), list)
            ):
                return None
            rows = manifest["files"]
            expected_files = {}
            for row in rows:
                if not isinstance(row, dict):
                    return None
                row_path = row.get("path")
                size = row.get("size_bytes")
                digest = row.get("sha256")
                if (
                    not isinstance(row_path, str)
                    or not row_path
                    or row_path == manifest_path
                    or not _is_sha256(digest)
                    or isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 0
                    or row_path in expected_files
                ):
                    return None
                client_export._validate_archive_member_name(row_path)
                expected_files[row_path] = (size, digest)
            if set(member_names) != set(expected_files) | {manifest_path}:
                return None
            for name, (expected_size, expected_sha) in expected_files.items():
                info = archive.getinfo(name)
                if info.file_size != expected_size:
                    return None
                digest = hashlib.sha256()
                count = 0
                with archive.open(info, "r") as member:
                    for chunk in iter(lambda: member.read(1024 * 1024), b""):
                        count += len(chunk)
                        digest.update(chunk)
                if count != expected_size or digest.hexdigest() != expected_sha:
                    return None

        privacy_review = manifest.get("privacyReview")
        if not isinstance(privacy_review, dict):
            return None
        return client_export.ExportResult(
            path=str(archive_path),
            fmt="prism",
            mod_count=mod_count,
            skipped=skipped[:1000],
            warnings=warnings[:1000],
            manifest_path=manifest_path,
            included_files=list(expected_files),
            archive_sha256=actual_archive_sha,
            input_fingerprint=expected_fingerprint,
        )
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, RuntimeError):
        return None


def _find_reusable_pack(directory: Path, expected_fingerprint: str):
    candidates = []
    try:
        for path in directory.glob("*.zip"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                candidates.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
    except OSError:
        return None
    for _, path in sorted(candidates, key=lambda pair: pair[0], reverse=True):
        result = _verified_cached_pack(path, expected_fingerprint)
        if result is not None:
            return result
    return None


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
            fingerprint = client_export.compute_input_fingerprint(
                self._instance,
                client_source=self._client_source,
                include_config=True,
                include_resourcepacks=self._include_resourcepacks,
                fmt="prism",
            )
            result = _find_reusable_pack(self._output.parent, fingerprint)
            if result is not None:
                # This reservation was created before the worker started. A
                # cache hit should reuse the existing immutable ZIP instead.
                try:
                    if (
                        self._output != Path(result.path)
                        and self._output.is_file()
                        and not self._output.is_symlink()
                        and self._output.stat().st_size == 0
                    ):
                        self._output.unlink()
                except OSError:
                    pass
                result.warnings = [
                    "Reused an existing local ZIP whose source fingerprint, archive checksum, manifest and payload hashes were verified."
                ] + result.warnings
            else:
                result = client_export.export(
                    self._instance,
                    self._output,
                    fmt="prism",
                    include_config=True,
                    client_source=self._client_source,
                    include_resourcepacks=self._include_resourcepacks,
                    expected_input_fingerprint=fingerprint,
                )
                if result.ok:
                    _write_cache_metadata(result)
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
        except GofileUploadError as exc:
            # Adapter errors are intentionally user-safe and actionable.
            self.done.emit({"result": None, "error": str(exc), "cancelled": False})
        except Exception:
            # Never surface arbitrary transport/provider exceptions: some
            # libraries include request headers or other sensitive context.
            self.done.emit({
                "result": None,
                "error": "Unexpected upload error. Sensitive details were suppressed; the local ZIP is preserved.",
                "cancelled": False,
            })


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
        self._status.setText("Checking source fingerprint/cache, then building or validating the ZIP and reviewing configuration…")

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
            if not isinstance(result.manifest_path, str) or not result.manifest_path:
                raise ValueError("manifest path is missing")
            with zipfile.ZipFile(self._archive_path, "r") as archive:
                manifest = json.loads(archive.read(result.manifest_path).decode("utf-8"))
            manifest = _validate_review_manifest(manifest)
            _verify_review_archive(
                self._archive_path,
                result.archive_sha256,
                result.manifest_path,
                manifest,
            )
        except (OSError, zipfile.BadZipFile, KeyError, TypeError, ValueError) as exc:
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
        distribution_review = manifest.get("distributionReview", [])
        distribution_by_path = {
            item["path"]: item for item in distribution_review if isinstance(item, dict) and isinstance(item.get("path"), str)
        }
        required_downloads = manifest.get("requiredDownloads", [])
        lines.extend([
            "",
            "DISTRIBUTION RIGHTS (curated, exact-artifact SHA-256 catalog):",
        ])
        if jar_rows:
            for jar_path in jar_rows:
                decision = distribution_by_path.get(jar_path)
                if not decision or decision.get("decision") == "unverified":
                    lines.append(
                        f"  - UNVERIFIED JAR — no reviewed catalog record for these exact bytes; check the author's terms: {jar_path}"
                    )
                    continue
                status = decision["decision"].upper()
                lines.append(
                    f"  - CURATED {status} RECORD for exact SHA-256: {jar_path} "
                    f"(SHA-256 {decision['sha256']}; checked {decision['verifiedOn']})"
                )
                lines.append(f"      Basis: {decision['basis']}")
                lines.append(f"      Project: {decision['projectUrl']}")
                lines.append(f"      Evidence: {decision['evidenceUrl']}")
                if decision.get("license"):
                    lines.append(f"      License: {decision['license']}")
                if decision.get("licenseUrl"):
                    lines.append(f"      License text: {decision['licenseUrl']}")
                if decision.get("notes"):
                    lines.append(f"      Curator notes: {decision['notes']}")
                for condition in decision.get("conditions", []):
                    lines.append(f"      CONDITION: {condition}")
        else:
            lines.append("  - No mod JAR files were included in this archive.")
        if resource_pack_names:
            lines.extend(
                f"  - UNVERIFIED RESOURCE PACK — check distribution terms separately: {pack_name}"
                for pack_name in sorted(resource_pack_names)
            )
        else:
            lines.append("  - No client resource packs were included.")

        if required_downloads:
            lines.extend([
                "",
                "NOT BUNDLED — REQUIRED MANUAL MOD DOWNLOADS:",
                "  These exact JARs were excluded because the curated catalog marks them prohibited.",
                "  This player instance may not launch correctly until the required mod is installed.",
            ])
            for item in required_downloads:
                lines.extend([
                    f"  - {item['filename']} ({item.get('modId') or 'mod ID not recorded'} {item.get('modVersion') or ''})".rstrip(),
                    f"      Reviewed alternative download: {item['alternativeDownloadUrl']}",
                    f"      Project: {item['projectUrl']}",
                    f"      Catalog evidence: {item['evidenceUrl']} (checked {item['verifiedOn']})",
                ])
            lines.append(
                "  The ZIP also contains CRUCIBLE-MOD-DOWNLOADS.txt with these instructions. "
                "Download the compatible version from the listed page and install it into .minecraft/mods/ before launch."
            )
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
            "  - A curated decision applies only to the exact JAR SHA-256 shown. No matching record means unverified; still confirm the evidence is current and all listed conditions are met.",
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
            f"I reviewed curated/unknown distribution status for {len(jar_rows)} bundled JAR(s), "
            f"{len(required_downloads)} required manual mod download(s), {len(resource_pack_names)} resource pack(s), "
            "and included configuration; I will follow all listed conditions and install any required mods before launch."
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
