#!/usr/bin/env python3
"""Prepare local UI screenshots for review without encoding images as chat text.

Examples:
    python scripts/prepare_ui_evidence.py ~/Pictures/CrucibleAudit/inbox
    python scripts/prepare_ui_evidence.py ~/Dropbox/Crucible/screenshots --recursive
    python scripts/prepare_ui_evidence.py one.png another.png --output /tmp/crucible-ui-review

The script reads input images only. It writes resized PNG copies, a contact sheet,
and a manifest outside the source folder, making it easy to inspect evidence
directly from the command line or share just the resulting folder path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
MAX_WIDTH = 1600
MAX_HEIGHT = 1200
THUMB_WIDTH = 360
THUMB_HEIGHT = 230
CONTACT_COLUMNS = 3


def _safe_stem(path: Path, index: int) -> str:
    raw = "".join(c if c.isalnum() or c in "-_." else "_" for c in path.stem).strip("._")
    return f"{index:03d}_{raw[:64] or 'screenshot'}"


def _gather(paths: list[Path], recursive: bool) -> list[Path]:
    found: list[Path] = []
    for original in paths:
        path = original.expanduser().resolve()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            found.append(path)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.iterdir()
            found.extend(
                item.resolve() for item in iterator
                if item.is_file() and item.suffix.lower() in IMAGE_SUFFIXES
            )
        elif not path.exists():
            print(f"Skip missing path: {path}", file=sys.stderr)
    unique: list[Path] = []
    seen: set[str] = set()
    for item in found:
        key = str(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    if len(unique) > 200:
        raise ValueError(f"Found {len(unique)} images; pass a narrower folder (limit is 200 per run).")
    return unique


def prepare(paths: list[Path], output: Path, recursive: bool = False) -> dict:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor, QFont, QImage, QPainter, QPen
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    sources = _gather(paths, recursive)
    if not sources:
        raise ValueError("No supported images found. Supported formats: PNG, JPEG, WebP, BMP, GIF.")
    output = output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    thumbnails = []
    hashes: set[str] = set()
    for index, source in enumerate(sources, start=1):
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in hashes:
            continue
        hashes.add(digest)

        image = QImage(str(source))
        if image.isNull():
            print(f"Could not decode image: {source}", file=sys.stderr)
            continue
        original_size = image.size()
        if image.width() > MAX_WIDTH or image.height() > MAX_HEIGHT:
            image = image.scaled(
                MAX_WIDTH, MAX_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        name = _safe_stem(source, index) + ".png"
        dest = output / name
        if not image.save(str(dest), "PNG", 9):
            print(f"Could not save optimized image: {dest}", file=sys.stderr)
            continue

        thumb = image.scaled(
            THUMB_WIDTH, THUMB_HEIGHT,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        entries.append({
            "index": len(entries) + 1,
            "source": str(source),
            "output": str(dest),
            "sha256": digest,
            "source_bytes": len(raw),
            "output_bytes": dest.stat().st_size,
            "source_dimensions": [original_size.width(), original_size.height()],
            "output_dimensions": [image.width(), image.height()],
        })
        thumbnails.append((source.name, thumb))

    if not entries:
        raise ValueError("No readable images were prepared.")

    cell_height = THUMB_HEIGHT + 48
    rows = (len(thumbnails) + CONTACT_COLUMNS - 1) // CONTACT_COLUMNS
    sheet = QImage(
        CONTACT_COLUMNS * (THUMB_WIDTH + 20) + 20,
        rows * cell_height + 20,
        QImage.Format.Format_RGB32,
    )
    sheet.fill(QColor("#202124"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont()
    font.setPointSize(9)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#eeeeee")))
    for i, (label, thumb) in enumerate(thumbnails):
        x = 20 + (i % CONTACT_COLUMNS) * (THUMB_WIDTH + 20)
        y = 20 + (i // CONTACT_COLUMNS) * cell_height
        painter.drawImage(x, y, thumb)
        painter.drawText(
            x, y + THUMB_HEIGHT + 6, THUMB_WIDTH, 38,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
            f"{i + 1:03d}  {label[:48]}",
        )
    painter.end()

    contact_sheet = output / "contact-sheet.png"
    if not sheet.save(str(contact_sheet), "PNG", 9):
        raise OSError(f"Could not write contact sheet: {contact_sheet}")
    manifest = {
        "count": len(entries),
        "contact_sheet": str(contact_sheet),
        "images": entries,
    }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="image files or folders (Dropbox folders work if synced locally)")
    parser.add_argument("--recursive", action="store_true", help="search input folders recursively")
    parser.add_argument("--output", type=Path,
                        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                        / "crucible" / "ui-evidence",
                        help="output folder (default: ~/.cache/crucible/ui-evidence)")
    args = parser.parse_args(argv)
    try:
        manifest = prepare([Path(p) for p in args.paths], args.output, args.recursive)
    except (OSError, ValueError) as exc:
        print(f"Screenshot preparation failed: {exc}", file=sys.stderr)
        return 2
    print(f"Prepared {manifest['count']} unique images.")
    print(f"Contact sheet: {manifest['contact_sheet']}")
    print(f"Manifest: {Path(args.output).expanduser().resolve() / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
