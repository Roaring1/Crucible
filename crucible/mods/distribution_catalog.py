"""Explicit, artifact-specific redistribution decisions for client-pack exports.

No license or redistribution permission is inferred from a filename, mod ID,
JAR metadata, or a project's reputation. Only exact SHA-256 records curated
with a review date and evidence URL can produce a non-unknown decision.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

CATALOG_SCHEMA_VERSION = 1
MAX_CATALOG_BYTES = 1024 * 1024
MAX_CATALOG_RECORDS = 10_000
_DECISIONS = frozenset({"permitted", "prohibited", "conditional"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class DistributionCatalogError(ValueError):
    """Raised when curated distribution evidence is malformed or unsafe."""


def validate_https_url(value: str, *, required: bool = False) -> str:
    """Accept a normal HTTPS URL with a hostname, no userinfo, and no odd port."""
    if value == "" and not required:
        return ""
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise DistributionCatalogError("catalog URL must be a non-empty HTTPS URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise DistributionCatalogError("catalog URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        raise DistributionCatalogError("catalog URLs must use HTTPS without credentials or non-standard ports")
    return value


def _bounded_text(record: dict, key: str, *, required: bool = False, maximum: int = 4096) -> str:
    value = record.get(key, "")
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (required and not value.strip())
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise DistributionCatalogError(f"catalog field {key!r} is missing or invalid")
    return value.strip()


def _text_list(record: dict, key: str, *, required: bool = False) -> tuple[str, ...]:
    value = record.get(key, [])
    if not isinstance(value, list) or len(value) > 50:
        raise DistributionCatalogError(f"catalog field {key!r} must be a bounded string list")
    items: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > 2048
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
        ):
            raise DistributionCatalogError(f"catalog field {key!r} contains an invalid item")
        items.append(item.strip())
    if required and not items:
        raise DistributionCatalogError(f"catalog field {key!r} must not be empty")
    return tuple(items)


@dataclass(frozen=True)
class DistributionRecord:
    sha256: str
    mod_id: str
    mod_version: str
    decision: str
    project_url: str
    evidence_url: str
    verified_on: str
    basis: str
    license_name: str
    license_url: str
    conditions: tuple[str, ...]
    alternative_download_url: str
    notes: str

    @classmethod
    def parse(cls, value) -> "DistributionRecord":
        if not isinstance(value, dict):
            raise DistributionCatalogError("each distribution catalog record must be an object")
        sha256 = _bounded_text(value, "sha256", required=True, maximum=64).lower()
        if not _SHA256_RE.fullmatch(sha256):
            raise DistributionCatalogError("catalog record sha256 must be a 64-character lowercase SHA-256")
        mod_id = _bounded_text(value, "modId", required=True, maximum=160)
        if not _ID_RE.fullmatch(mod_id):
            raise DistributionCatalogError("catalog modId contains unsupported characters")
        mod_version = _bounded_text(value, "modVersion", maximum=160)
        decision = _bounded_text(value, "decision", required=True, maximum=32).lower()
        if decision not in _DECISIONS:
            raise DistributionCatalogError("catalog decision must be permitted, prohibited, or conditional")
        project_url = validate_https_url(_bounded_text(value, "projectUrl", required=True))
        evidence_url = validate_https_url(_bounded_text(value, "evidenceUrl", required=True))
        license_url = validate_https_url(_bounded_text(value, "licenseUrl"))
        alternative_download_url = validate_https_url(_bounded_text(value, "alternativeDownloadUrl"))
        verified_on = _bounded_text(value, "verifiedOn", required=True, maximum=10)
        try:
            review_date = date.fromisoformat(verified_on)
        except ValueError as exc:
            raise DistributionCatalogError("verifiedOn must be an ISO date (YYYY-MM-DD)") from exc
        if review_date > date.today():
            raise DistributionCatalogError("verifiedOn cannot be a future date")
        basis = _bounded_text(value, "basis", required=True, maximum=2048)
        license_name = _bounded_text(value, "license", maximum=160)
        conditions = _text_list(value, "conditions", required=decision == "conditional")
        notes = _bounded_text(value, "notes", maximum=4096)
        if decision == "permitted" and not (license_name or license_url or "permission" in basis.lower()):
            raise DistributionCatalogError(
                "permitted records require a license identifier/URL or an explicit-permission basis"
            )
        return cls(
            sha256=sha256,
            mod_id=mod_id,
            mod_version=mod_version,
            decision=decision,
            project_url=project_url,
            evidence_url=evidence_url,
            verified_on=verified_on,
            basis=basis,
            license_name=license_name,
            license_url=license_url,
            conditions=conditions,
            alternative_download_url=alternative_download_url,
            notes=notes,
        )

    def review_entry(self, *, archive_path: str, filename: str, bundled: bool) -> dict:
        """Return only reviewed, non-secret fields for the ZIP manifest/UI."""
        return {
            "path": archive_path,
            "filename": filename,
            "sha256": self.sha256,
            "modId": self.mod_id,
            "modVersion": self.mod_version,
            "decision": self.decision,
            "projectUrl": self.project_url,
            "evidenceUrl": self.evidence_url,
            "verifiedOn": self.verified_on,
            "basis": self.basis,
            "license": self.license_name,
            "licenseUrl": self.license_url,
            "conditions": list(self.conditions),
            "alternativeDownloadUrl": self.alternative_download_url,
            "notes": self.notes,
            "bundled": bool(bundled),
        }


@dataclass(frozen=True)
class DistributionCatalog:
    revision: str
    catalog_version: str
    records: dict[str, DistributionRecord]

    def lookup(self, sha256: str) -> DistributionRecord | None:
        digest = (sha256 or "").lower()
        return self.records.get(digest)


def _catalog_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "mod_distribution_catalog.json"


def load_catalog(path: str | Path | None = None) -> DistributionCatalog:
    """Load a bounded reviewed catalog. Invalid catalogs fail closed, never grant permission."""
    catalog_file = Path(path) if path is not None else _catalog_path()
    if catalog_file.is_symlink() or not catalog_file.is_file():
        raise DistributionCatalogError("the curated mod distribution catalog is missing or is a symbolic link")
    try:
        if catalog_file.stat().st_size > MAX_CATALOG_BYTES:
            raise DistributionCatalogError("the curated mod distribution catalog exceeds its size limit")
        raw = catalog_file.read_bytes()
    except OSError as exc:
        raise DistributionCatalogError("the curated mod distribution catalog could not be read") from exc
    if len(raw) > MAX_CATALOG_BYTES:
        raise DistributionCatalogError("the curated mod distribution catalog exceeds its size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DistributionCatalogError("the curated mod distribution catalog is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != CATALOG_SCHEMA_VERSION:
        raise DistributionCatalogError("the curated mod distribution catalog schema version is unsupported")
    catalog_version = _bounded_text(payload, "catalogVersion", required=True, maximum=160)
    rows = payload.get("records")
    if not isinstance(rows, list) or len(rows) > MAX_CATALOG_RECORDS:
        raise DistributionCatalogError("the curated mod distribution catalog record list is missing or too large")
    records: dict[str, DistributionRecord] = {}
    for row in rows:
        record = DistributionRecord.parse(row)
        if record.sha256 in records:
            raise DistributionCatalogError("duplicate SHA-256 records are not allowed in the distribution catalog")
        records[record.sha256] = record
    return DistributionCatalog(
        revision=hashlib.sha256(raw).hexdigest(),
        catalog_version=catalog_version,
        records=records,
    )


def catalog_revision() -> str:
    """Return the raw catalog digest so pack-cache fingerprints invalidate on catalog edits."""
    return load_catalog().revision
