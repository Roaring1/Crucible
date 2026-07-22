"""
crucible/ui/properties_dialog.py

Friendly “did you mean —” repair dialog for server.properties.

When Crucible detects bad settings (the classic blank ``server-port=`` that makes
Minecraft crash with ``NumberFormatException: For input string: ""``, plus typos
and invalid values) this dialog lists every problem in plain language and offers
a drop-down of safe replacement values for each one.  Start-blocking problems are
pre-selected and highlighted; the owner just clicks “Apply fixes”.

Everything is defensive — a missing/garbled file degrades to a friendly message
rather than crashing the app.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QComboBox, QFrame, QScrollArea, QWidget, QMessageBox,
)

from . import theme
from ..data import properties as P


class PropertiesFixDialog(QDialog):
    """Lists server.properties issues with per-issue replacement menus."""

    def __init__(self, properties_path, parent=None) -> None:
        super().__init__(parent)
        self._path = Path(properties_path)
        self._rows: list[tuple[P.PropIssue, QComboBox]] = []
        self.applied = False
        self.setWindowTitle("Check server settings")
        self.setMinimumWidth(560)
        self._build_ui()

    # ---- UI ----

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        issues = P.validate_file(self._path)
        errors = [i for i in issues if i.is_error]

        title = QLabel("Server settings check")
        title.setStyleSheet(f"font-size: 17px; font-weight: 700; color: {theme.TEXT};")
        outer.addWidget(title)

        if not issues:
            good = QLabel("✓  Everything looks good — no problems found in server.properties.")
            good.setStyleSheet(f"color: {theme.GREEN}; font-size: 13px;")
            good.setWordWrap(True)
            outer.addWidget(good)
            btn = QPushButton("Close")
            btn.clicked.connect(self.reject)
            outer.addWidget(btn, alignment=Qt.AlignmentFlag.AlignRight)
            return

        if errors:
            warn = QLabel(
                f"⚠  {len(errors)} setting(s) will crash the server before it starts "
                "(this is the cause of “NumberFormatException: For input string”). "
                "Pick a replacement below and click Apply fixes."
            )
            warn.setStyleSheet(f"color: {theme.RED}; font-size: 12px;")
        else:
            warn = QLabel(
                "These are advisory — the server should still start, but you may want "
                "to clean them up."
            )
            warn.setStyleSheet(f"color: {theme.YELLOW}; font-size: 12px;")
        warn.setWordWrap(True)
        outer.addWidget(warn)

        # Scrollable list of issues
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        scroll.setWidget(container)
        grid = QGridLayout(container)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(1, 1)

        # Sort: errors first
        issues.sort(key=lambda i: (0 if i.is_error else 1, i.key))

        for row, issue in enumerate(issues):
            dot = QLabel("✗" if issue.is_error else "—")
            dot.setStyleSheet(
                f"color: {theme.RED if issue.is_error else theme.YELLOW}; "
                "font-weight: 700; font-size: 14px;"
            )
            dot.setFixedWidth(16)
            grid.addWidget(dot, row, 0, Qt.AlignmentFlag.AlignTop)

            text = QVBoxLayout()
            text.setSpacing(1)
            key_lbl = QLabel(issue.key)
            key_lbl.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600; font-size: 13px;")
            text.addWidget(key_lbl)
            msg = QLabel(issue.message)
            msg.setWordWrap(True)
            msg.setStyleSheet(f"color: {theme.SUBTEXT}; font-size: 11px;")
            text.addWidget(msg)
            wrap = QWidget()
            wrap.setLayout(text)
            grid.addWidget(wrap, row, 1)

            combo = self._build_combo(issue)
            grid.addWidget(combo, row, 2, Qt.AlignmentFlag.AlignTop)
            self._rows.append((issue, combo))

        outer.addWidget(scroll, stretch=1)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        apply_btn = QPushButton("Apply fixes")
        apply_btn.setObjectName("PrimaryButton")
        apply_btn.clicked.connect(self._apply)
        btn_row.addWidget(apply_btn)
        outer.addLayout(btn_row)

    def _build_combo(self, issue: P.PropIssue) -> QComboBox:
        combo = QComboBox()
        combo.setMinimumWidth(170)
        # "Keep current" is always an option (do nothing for this issue).
        choices: list[tuple[str, object]] = []
        # Preferred suggestion first.
        if issue.kind == "unknown_key":
            if issue.suggestion:
                choices.append((f"Rename → {issue.suggestion}", ("rename", issue.suggestion)))
            for opt in issue.options:
                if opt != issue.suggestion:
                    choices.append((f"Rename → {opt}", ("rename", opt)))
            choices.append(("Delete this line", ("delete", None)))
        elif issue.kind == "duplicate_key":
            choices.append(("Leave as-is (last value wins)", ("keep", None)))
        else:
            seen = set()
            ordered = []
            if issue.suggestion is not None:
                ordered.append(issue.suggestion)
            for o in issue.options:
                if o not in ordered:
                    ordered.append(o)
            if issue.default is not None and issue.default not in ordered:
                ordered.append(issue.default)
            for val in ordered:
                if val in seen:
                    continue
                seen.add(val)
                label = f"{val}" + ("  (recommended)" if val == issue.suggestion else "")
                choices.append((label, ("set", val)))
        choices.append((f"Keep current ({issue.current})", ("keep", None)))

        for label, payload in choices:
            combo.addItem(label, payload)
        # Default selection: recommended fix for errors; "keep" for advisory.
        combo.setCurrentIndex(0 if issue.is_error else max(0, len(choices) - 1))
        return combo

    # ---- apply ----

    def _apply(self) -> None:
        try:
            doc = P.PropertiesDoc.load(self._path)
        except Exception as exc:
            QMessageBox.warning(self, "Server settings", f"Could not read the file:\n{exc}")
            return

        changed = 0
        for issue, combo in self._rows:
            action, value = combo.currentData()
            if action == "keep":
                continue
            if action == "set" and value is not None:
                doc.set(issue.key, value)
                changed += 1
            elif action == "rename" and value:
                if doc.get(value) is None:
                    doc.rename_key(issue.key, value)
                    changed += 1
            elif action == "delete":
                doc.set(issue.key, doc.get(issue.key) or "")  # no-op safety
                # actual delete:
                doc._lines = [t for t in doc._lines
                              if not (t[0] == "kv" and t[1] == issue.key)]
                changed += 1

        if changed == 0:
            self.reject()
            return
        try:
            # keep a one-time backup
            bak = self._path.with_suffix(self._path.suffix + ".bak")
            if not bak.exists():
                import shutil
                shutil.copy2(self._path, bak)
        except OSError:
            pass
        try:
            doc.save(self._path)
        except Exception as exc:
            QMessageBox.warning(self, "Server settings", f"Could not save changes:\n{exc}")
            return
        self.applied = True
        QMessageBox.information(
            self, "Server settings",
            f"Applied {changed} fix(es). A backup was saved as server.properties.bak.",
        )
        self.accept()
