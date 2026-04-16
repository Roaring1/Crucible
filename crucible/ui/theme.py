"""
crucible/ui/theme.py

Single source of truth for all colors and the Qt stylesheet.
Dark theme loosely inspired by Catppuccin Mocha with a purple accent.

Palette
───────
  BASE      #1e1e2e  Main window background
  MANTLE    #181825  Sidebar / darker surfaces
  CRUST     #11111b  Tab bar, title bar
  SURFACE0  #313244  Input fields, table rows (alt)
  SURFACE1  #45475a  Borders, separators
  SURFACE2  #585b70  Disabled text
  TEXT      #cdd6f4  Primary text
  SUBTEXT   #a6adc8  Secondary / dim text
  ACCENT    #7c3aed  Purple — buttons, selection, focus
  ACCENT_HO #9458f5  Accent hover
  GREEN     #a6e3a1  Running status
  YELLOW    #f9e2af  Warnings
  RED       #f38ba8  Errors / stopped
  ORANGE    #fab387  Caution / restarting
"""

# ── Raw colors (usable from Python for custom painting) ───────────────────────

BASE      = "#1e1e2e"
MANTLE    = "#181825"
CRUST     = "#11111b"
SURFACE0  = "#313244"
SURFACE1  = "#45475a"
SURFACE2  = "#585b70"
TEXT      = "#cdd6f4"
SUBTEXT   = "#a6adc8"
ACCENT    = "#7c3aed"
ACCENT_HO = "#9458f5"
GREEN     = "#a6e3a1"
YELLOW    = "#f9e2af"
RED       = "#f38ba8"
ORANGE    = "#fab387"

# Status → color mapping (for status dots and labels)
STATUS_COLORS = {
    "running":      GREEN,
    "stopped":      SURFACE2,
    "tmux_missing": YELLOW,
    "starting":     ORANGE,
    "unknown":      SURFACE2,
}

# Console log level → color mapping
LOG_COLORS = {
    "INFO":   TEXT,
    "WARN":   YELLOW,
    "WARNING": YELLOW,
    "ERROR":  RED,
    "SEVERE": RED,
    "FATAL":  RED,
    "DEBUG":  SURFACE2,
}


# ── Qt stylesheet ─────────────────────────────────────────────────────────────

STYLESHEET = f"""

/* ── Global ──────────────────────────────────────────── */
QWidget {{
    background-color: {BASE};
    color: {TEXT};
    font-family: "Inter", "Noto Sans", "Segoe UI", sans-serif;
    font-size: 13px;
    border: none;
    outline: none;
}}

QMainWindow {{
    background-color: {BASE};
}}

/* ── Splitter ────────────────────────────────────────── */
QSplitter::handle {{
    background-color: {SURFACE1};
    width: 1px;
}}
QSplitter::handle:hover {{
    background-color: {ACCENT};
}}

/* ── Sidebar (left panel) ────────────────────────────── */
#Sidebar {{
    background-color: {MANTLE};
    border-right: 1px solid {SURFACE1};
}}

#SidebarTitle {{
    background-color: {CRUST};
    color: {SUBTEXT};
    font-size: 11px;
    letter-spacing: 1.5px;
    font-weight: 600;
    padding: 8px 12px 6px 12px;
    border-bottom: 1px solid {SURFACE1};
}}

/* ── Instance list (sidebar) ─────────────────────────── */
QListWidget {{
    background-color: {MANTLE};
    border: none;
    padding: 4px 0px;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-radius: 6px;
    margin: 1px 6px;
    color: {TEXT};
}}
QListWidget::item:hover {{
    background-color: {SURFACE0};
}}
QListWidget::item:selected {{
    background-color: {SURFACE0};
    border-left: 3px solid {ACCENT};
    color: {TEXT};
}}

/* ── Buttons ─────────────────────────────────────────── */
QPushButton {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 6px;
    padding: 5px 14px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {SURFACE1};
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {ACCENT};
    color: white;
    border-color: {ACCENT};
}}
QPushButton:disabled {{
    background-color: {SURFACE0};
    color: {SURFACE2};
    border-color: {SURFACE1};
}}

/* Primary action button (Start) */
QPushButton#PrimaryButton {{
    background-color: {ACCENT};
    color: white;
    border: none;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background-color: {ACCENT_HO};
}}
QPushButton#PrimaryButton:disabled {{
    background-color: {SURFACE0};
    color: {SURFACE2};
}}

/* Danger button (Stop / force actions) */
QPushButton#DangerButton {{
    background-color: transparent;
    color: {RED};
    border: 1px solid {RED};
}}
QPushButton#DangerButton:hover {{
    background-color: {RED};
    color: {CRUST};
}}
/* Must explicitly override disabled — ID specificity beats QPushButton:disabled */
QPushButton#DangerButton:disabled {{
    background-color: transparent;
    color: {SURFACE2};
    border: 1px solid {SURFACE1};
}}

/* ── Tab bar ─────────────────────────────────────────── */
QTabWidget::pane {{
    background-color: {BASE};
    border: none;
    border-top: 1px solid {SURFACE1};
}}
QTabBar {{
    background-color: {CRUST};
}}
QTabBar::tab {{
    background-color: {CRUST};
    color: {SUBTEXT};
    padding: 7px 18px;
    border: none;
    font-size: 13px;
}}
QTabBar::tab:selected {{
    background-color: {BASE};
    color: {TEXT};
    border-top: 2px solid {ACCENT};
}}
QTabBar::tab:hover:!selected {{
    background-color: {SURFACE0};
    color: {TEXT};
}}

/* ── Console / log view ──────────────────────────────── */
QPlainTextEdit#ConsoleView {{
    background-color: {CRUST};
    color: {TEXT};
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", "Hack",
                 "DejaVu Sans Mono", monospace;
    font-size: 12px;
    border: none;
    padding: 6px;
    selection-background-color: {ACCENT};
}}

/* ── Input fields ────────────────────────────────────── */
QLineEdit {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 5px;
    padding: 5px 8px;
    font-size: 13px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit#CommandInput {{
    font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 12px;
}}

/* ── Text edit (Notes) ───────────────────────────────── */
QTextEdit {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 5px;
    padding: 8px;
    font-size: 13px;
    selection-background-color: {ACCENT};
}}
QTextEdit:focus {{
    border-color: {ACCENT};
}}

/* ── Table ───────────────────────────────────────────── */
QTableWidget {{
    background-color: {BASE};
    gridline-color: {SURFACE0};
    border: none;
    alternate-background-color: {MANTLE};
    selection-background-color: {SURFACE0};
    selection-color: {TEXT};
}}
QTableWidget::item {{
    padding: 4px 8px;
    border: none;
}}
QTableWidget::item:selected {{
    background-color: {SURFACE0};
    color: {TEXT};
}}
QHeaderView::section {{
    background-color: {CRUST};
    color: {SUBTEXT};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
    padding: 5px 8px;
    border: none;
    border-right: 1px solid {SURFACE1};
    border-bottom: 1px solid {SURFACE1};
}}

/* ── Scrollbars ──────────────────────────────────────── */
QScrollBar:vertical {{
    background-color: {MANTLE};
    width: 8px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {SURFACE1};
    border-radius: 4px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {ACCENT};
}}
QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{
    background: none;
    height: 0px;
}}
QScrollBar:horizontal {{
    background-color: {MANTLE};
    height: 8px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background-color: {SURFACE1};
    border-radius: 4px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {ACCENT};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: none;
    width: 0px;
}}

/* ── Status bar ──────────────────────────────────────── */
QStatusBar {{
    background-color: {CRUST};
    color: {SUBTEXT};
    font-size: 12px;
    border-top: 1px solid {SURFACE1};
}}
QStatusBar::item {{
    border: none;
}}

/* ── Checkboxes ──────────────────────────────────────── */
QCheckBox {{
    spacing: 6px;
    color: {TEXT};
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {SURFACE1};
    border-radius: 3px;
    background-color: {SURFACE0};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

/* ── Tooltips ────────────────────────────────────────── */
QToolTip {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}}

/* ── Labels ──────────────────────────────────────────── */
QLabel#HeaderName {{
    font-size: 18px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#HeaderVersion {{
    font-size: 11px;
    color: {SUBTEXT};
    background-color: {SURFACE0};
    border-radius: 4px;
    padding: 2px 7px;
}}
QLabel#StatusLabel {{
    font-size: 12px;
    font-weight: 600;
}}
QLabel#SectionLabel {{
    font-size: 11px;
    font-weight: 600;
    color: {SUBTEXT};
    letter-spacing: 1px;
}}

/* ── Sidebar add button ──────────────────────────────── */
QPushButton#SidebarAddButton {{
    background-color: transparent;
    color: {SUBTEXT};
    border: 1px dashed {SURFACE1};
    border-radius: 6px;
    margin: 4px 8px;
    padding: 6px;
    font-size: 12px;
    text-align: center;
}}
QPushButton#SidebarAddButton:hover {{
    background-color: {SURFACE0};
    color: {TEXT};
    border-color: {ACCENT};
}}

/* ── Dialog / MessageBox ─────────────────────────────── */
QDialog {{
    background-color: {BASE};
}}
QMessageBox {{
    background-color: {BASE};
}}
QDialogButtonBox QPushButton {{
    min-width: 80px;
}}
"""
