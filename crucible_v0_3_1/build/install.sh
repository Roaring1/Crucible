#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Crucible — GTNH Server Manager  ·  Install Script
# Target: Nobara Linux 41–43 (Fedora-based, dnf) · Python 3.11+
#
# Usage:
#   bash install.sh
#
# What this does:
#   1. Checks Python 3.11+
#   2. Checks / installs pip
#   3. Installs PyQt6 and crucible in editable mode
#   4. Adds a crucible.desktop launcher to ~/.local/share/applications/
#   5. Prints first-run instructions
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
RESET="\033[0m"

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*" >&2; }
info() { echo -e "  ${CYAN}·${RESET}  $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e ""
echo -e "${BOLD}${CYAN}  ╔═══════════════════════════════╗"
echo -e "  ║   C R U C I B L E             ║"
echo -e "  ║   GTNH Server Manager  v0.3   ║"
echo -e "  ╚═══════════════════════════════╝${RESET}"
echo -e ""

# ── Step 1: Python version ────────────────────────────────────────────────────

echo -e "${BOLD}── Step 1: Python version${RESET}"
PYTHON=""
for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c "import sys; print(sys.version_info[:2])")
        major=$("$candidate" -c "import sys; print(sys.version_info.major)")
        minor=$("$candidate" -c "import sys; print(sys.version_info.minor)")
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PYTHON="$candidate"
            ok "Found $candidate  ($(${candidate} --version))"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    err "Python 3.11 or newer is required."
    info "Install with:  sudo dnf install python3.12"
    exit 1
fi

# ── Step 2: pip ───────────────────────────────────────────────────────────────

echo -e "\n${BOLD}── Step 2: pip${RESET}"
if ! "$PYTHON" -m pip --version &>/dev/null; then
    warn "pip not found — installing…"
    sudo dnf install -y python3-pip
fi
ok "pip available"

# ── Step 3: PyQt6 ─────────────────────────────────────────────────────────────

echo -e "\n${BOLD}── Step 3: PyQt6${RESET}"
if "$PYTHON" -c "import PyQt6" &>/dev/null 2>&1; then
    ok "PyQt6 already installed"
else
    info "Installing PyQt6 via pip…"
    # Try system package first (faster, no Wayland weirdness)
    if sudo dnf install -y python3-pyqt6 &>/dev/null 2>&1; then
        ok "Installed python3-pyqt6 via dnf"
    else
        "$PYTHON" -m pip install --user PyQt6
        ok "Installed PyQt6 via pip"
    fi
fi

# ── Step 4: Install crucible ──────────────────────────────────────────────────

echo -e "\n${BOLD}── Step 4: Install Crucible${RESET}"

# If a previous version is installed, uninstall it cleanly first
if "$PYTHON" -m pip show crucible &>/dev/null 2>&1; then
    PREV_VER=$("$PYTHON" -m pip show crucible 2>/dev/null | grep ^Version | awk '{print $2}')
    warn "Found existing Crucible installation (v${PREV_VER}) — upgrading…"
    "$PYTHON" -m pip uninstall -y crucible &>/dev/null
    ok "Previous version removed"
fi

"$PYTHON" -m pip install --user -e "$SCRIPT_DIR"
ok "Crucible installed in editable mode"

# Ensure ~/.local/bin is on PATH
LOCAL_BIN="$HOME/.local/bin"
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    warn "~/.local/bin is not in your PATH."
    warn "Add this to ~/.bashrc or ~/.zshrc:"
    echo  "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    warn "Then run: source ~/.bashrc"
fi

# ── Step 5: Desktop entry ─────────────────────────────────────────────────────

echo -e "\n${BOLD}── Step 5: Desktop launcher${RESET}"
APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"

CRUCIBLE_BIN="$LOCAL_BIN/crucible"
DESKTOP_FILE="$APPS_DIR/crucible.desktop"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Crucible
GenericName=GTNH Server Manager
Comment=GT: New Horizons Server Manager
Exec=${CRUCIBLE_BIN} gui
Terminal=false
Categories=Game;Utility;
Keywords=GTNH;Minecraft;Server;Manager;
StartupWMClass=Crucible
EOF

ok "Desktop entry written: $DESKTOP_FILE"
info "You can now launch Crucible from KDE's application launcher"

# ── Step 6: tmux check ────────────────────────────────────────────────────────

echo -e "\n${BOLD}── Step 6: tmux${RESET}"
if command -v tmux &>/dev/null; then
    ok "tmux is installed  ($(tmux -V))"
else
    warn "tmux not found — Crucible needs it to manage servers."
    info "Install with:  sudo dnf install tmux"
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo -e ""
echo -e "${BOLD}${GREEN}── Installation complete! ──────────────────────────────${RESET}"
echo -e ""
echo -e "  Launch the GUI:"
echo -e "    ${CYAN}crucible gui${RESET}"
echo -e ""
echo -e "  Register your server:"
echo -e "    ${CYAN}crucible add /home/roaring/Desktop/Midtech/GT_New_Horizons_2.8.4_Server_Java_17-25 \\"
echo -e "      --name \"Midtech\" --session gtnh --version 2.8.4${RESET}"
echo -e ""
echo -e "  Or use the GUI's '+ Add Server' button and browse to that path."
echo -e ""
echo -e "  CLI reference:   ${CYAN}crucible --help${RESET}"
echo -e "  Registry lives:  ${CYAN}~/.config/crucible/instances.json${RESET}"
echo -e ""
