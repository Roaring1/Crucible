"""
crucible/ui/theme.py -- colors and Qt stylesheet.

Accent is amber (#c8903a) -- fits the crucible/forge name, not a gradient.
Surfaces from a warm near-black, not pure #000.
"""

# raw colors

BASE      = "#1c1c1e"
MANTLE    = "#161618"
CRUST     = "#111113"
SURFACE0  = "#2c2c30"
SURFACE1  = "#3c3c42"
SURFACE2  = "#545460"
TEXT      = "#dddad0"
SUBTEXT   = "#96929e"
ACCENT    = "#c8903a"
ACCENT_HO = "#d9a048"
GREEN     = "#a6e3a1"
YELLOW    = "#f9e2af"
RED       = "#f38ba8"
ORANGE    = "#fab387"

STATUS_COLORS = {
    "running":      GREEN,
    "stopped":      SURFACE2,
    "tmux_missing": YELLOW,
    "starting":     ORANGE,
    "stopping":     YELLOW,
    "unknown":      SURFACE2,
}

LOG_COLORS = {
    "INFO":    TEXT,
    "WARN":    YELLOW,
    "WARNING": YELLOW,
    "ERROR":   RED,
    "SEVERE":  RED,
    "FATAL":   RED,
    "DEBUG":   SURFACE2,
}


STYLESHEET = f"""

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

/* splitter */
QSplitter::handle {{
    background-color: {SURFACE1};
    width: 1px;
}}
QSplitter::handle:hover {{
    background-color: {SURFACE2};
}}

/* sidebar */
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

/* instance list */
QListWidget {{
    background-color: {MANTLE};
    border: none;
    padding: 4px 0px;
}}
QListWidget::item {{
    padding: 8px 12px;
    border-radius: 4px;
    margin: 1px 6px;
    color: {TEXT};
}}
QListWidget::item:hover {{
    background-color: {SURFACE0};
}}
QListWidget::item:selected {{
    background-color: {SURFACE0};
    border-left: 2px solid {ACCENT};
    color: {TEXT};
}}

/* buttons -- base style is flat, minimal */
QPushButton {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 3px;
    padding: 4px 12px;
    font-size: 13px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {SURFACE1};
    border-color: {SURFACE2};
}}
QPushButton:pressed {{
    background-color: {SURFACE1};
}}
QPushButton:disabled {{
    background-color: {SURFACE0};
    color: {SURFACE2};
    border-color: {SURFACE1};
}}

/* Start -- primary action, accent fill */
QPushButton#PrimaryButton {{
    background-color: {ACCENT};
    color: {CRUST};
    border: none;
    font-weight: 600;
    padding: 4px 14px;
}}
QPushButton#PrimaryButton:hover {{
    background-color: {ACCENT_HO};
}}
QPushButton#PrimaryButton:disabled {{
    background-color: {SURFACE0};
    color: {SURFACE2};
    border: 1px solid {SURFACE1};
    font-weight: 500;
}}

/* Stop -- danger, filled red when enabled */
QPushButton#DangerButton {{
    background-color: transparent;
    color: {RED};
    border: 1px solid {RED};
    font-weight: 600;
}}
QPushButton#DangerButton:hover {{
    background-color: {RED};
    color: {CRUST};
}}
QPushButton#DangerButton:disabled {{
    background-color: transparent;
    color: {SURFACE2};
    border: 1px solid {SURFACE1};
    font-weight: 500;
}}

/* Restart -- secondary, no fill */
QPushButton#RestartButton {{
    background-color: transparent;
    color: {SUBTEXT};
    border: 1px solid {SURFACE1};
    padding: 4px 10px;
    font-weight: 400;
}}
QPushButton#RestartButton:hover {{
    background-color: {SURFACE0};
    color: {TEXT};
}}
QPushButton#RestartButton:disabled {{
    color: {SURFACE2};
    border-color: {SURFACE1};
}}

/* Console/Attach -- least important, text-only feel */
QPushButton#AttachButton {{
    background-color: transparent;
    color: {SUBTEXT};
    border: 1px solid {SURFACE1};
    padding: 4px 10px;
    font-weight: 400;
}}
QPushButton#AttachButton:hover {{
    background-color: {SURFACE0};
    color: {TEXT};
}}
QPushButton#AttachButton:disabled {{
    color: {SURFACE2};
    border-color: {SURFACE1};
}}

/* tab bar */
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
    padding: 7px 16px;
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

/* console log */
QPlainTextEdit#ConsoleView {{
    background-color: {CRUST};
    color: {TEXT};
    font-family: "JetBrains Mono", "Fira Code", "Cascadia Code", "Hack",
                 "DejaVu Sans Mono", monospace;
    font-size: 12px;
    border: none;
    padding: 6px;
    selection-background-color: {SURFACE1};
}}

/* inputs */
QLineEdit {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 3px;
    padding: 5px 8px;
    font-size: 13px;
    selection-background-color: {SURFACE1};
}}
QLineEdit:focus {{
    border-color: {ACCENT};
}}
QLineEdit#CommandInput {{
    font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 12px;
}}

QTextEdit {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 3px;
    padding: 8px;
    font-size: 13px;
    selection-background-color: {SURFACE1};
}}
QTextEdit:focus {{
    border-color: {ACCENT};
}}

/* table */
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

/* scrollbars */
QScrollBar:vertical {{
    background-color: {MANTLE};
    width: 7px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background-color: {SURFACE1};
    border-radius: 3px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background-color: {SURFACE2};
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
    height: 7px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background-color: {SURFACE1};
    border-radius: 3px;
    min-width: 30px;
}}
QScrollBar::handle:horizontal:hover {{
    background-color: {SURFACE2};
}}
QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {{
    background: none;
    width: 0px;
}}

/* status bar */
QStatusBar {{
    background-color: {CRUST};
    color: {SUBTEXT};
    font-size: 11px;
    border-top: 1px solid {SURFACE1};
}}
QStatusBar::item {{
    border: none;
}}

/* checkboxes */
QCheckBox {{
    spacing: 6px;
    color: {TEXT};
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {SURFACE1};
    border-radius: 2px;
    background-color: {SURFACE0};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}

/* tooltips */
QToolTip {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    padding: 4px 8px;
    font-size: 12px;
}}

/* named labels */
QLabel#HeaderName {{
    font-size: 18px;
    font-weight: 700;
    color: {TEXT};
}}
QLabel#HeaderVersion {{
    font-size: 11px;
    color: {SUBTEXT};
    background-color: {SURFACE0};
    border-radius: 3px;
    padding: 2px 6px;
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

/* sidebar add button */
QPushButton#SidebarAddButton {{
    background-color: transparent;
    color: {SUBTEXT};
    border: 1px dashed {SURFACE1};
    border-radius: 3px;
    margin: 4px 8px;
    padding: 6px;
    font-size: 12px;
    text-align: center;
}}
QPushButton#SidebarAddButton:hover {{
    background-color: {SURFACE0};
    color: {TEXT};
    border-color: {ACCENT};
    border-style: solid;
}}

/* dialogs */
QDialog {{
    background-color: {BASE};
}}
QMessageBox {{
    background-color: {BASE};
}}
QDialogButtonBox QPushButton {{
    min-width: 80px;
}}

/* spinner */
QSpinBox {{
    background-color: {SURFACE0};
    color: {TEXT};
    border: 1px solid {SURFACE1};
    border-radius: 3px;
    padding: 3px 6px;
}}
QSpinBox:focus {{
    border-color: {ACCENT};
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background-color: {SURFACE1};
    border: none;
    width: 16px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: {SURFACE2};
}}

/* progress bar */
QProgressBar {{
    background-color: {SURFACE0};
    border: 1px solid {SURFACE1};
    border-radius: 3px;
    text-align: center;
    font-size: 11px;
    color: {TEXT};
    height: 14px;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 2px;
}}
"""
