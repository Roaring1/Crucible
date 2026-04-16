#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Crucible v0.3.2 — Install Script
# Target: Nobara 41–43 / Fedora (dnf) · Python 3.11+
#
# Works from ANYWHERE — Downloads, Desktop, /tmp, it doesn't matter.
# The script copies itself to the right place; you can delete the zip after.
#
# Usage after extracting the zip:
#   bash crucible_v0_3_2/install.sh
#
# Or the one-liner (requires curl):
#   bash <(curl -sL https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"
RED="\033[31m"; CYAN="\033[36m"; DIM="\033[2m"; RESET="\033[0m"

ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
err()  { echo -e "  ${RED}✗${RESET}  $*" >&2; }
info() { echo -e "  ${CYAN}·${RESET}  $*"; }
step() { echo -e "\n${BOLD}── $*${RESET}"; }

# Where the source lives RIGHT NOW (wherever the zip was extracted)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Where it will live permanently (hidden, out of the way)
APP_HOME="$HOME/.local/share/crucible"
LOCAL_BIN="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"

echo -e ""
echo -e "${BOLD}${CYAN}  ╔════════════════════════════════════╗"
echo -e "  ║   C R U C I B L E   v0.3.2        ║"
echo -e "  ║   GTNH Server Manager              ║"
echo -e "  ║   Nobara 41–43  ·  Python 3.11+    ║"
echo -e "  ╚════════════════════════════════════╝${RESET}"
echo -e ""
info "Source: $HERE"
info "Will install to: $APP_HOME"
echo -e ""

# ── Python ────────────────────────────────────────────────────────────────────
step "1 / 5  Python"

PYTHON=""
for c in python3.12 python3.11 python3; do
    if command -v "$c" &>/dev/null; then
        _maj=$("$c" -c "import sys; print(sys.version_info.major)")
        _min=$("$c" -c "import sys; print(sys.version_info.minor)")
        if [[ "$_maj" -ge 3 && "$_min" -ge 11 ]]; then
            PYTHON="$c"; ok "Using $c  ($("$c" --version))"; break
        fi
    fi
done

[[ -z "$PYTHON" ]] && { err "Python 3.11+ required.  sudo dnf install python3.12"; exit 1; }

# ── pip ───────────────────────────────────────────────────────────────────────
step "2 / 5  pip"
"$PYTHON" -m pip --version &>/dev/null || { warn "Installing pip…"; sudo dnf install -y python3-pip; }
ok "pip ready"

# ── PyQt6 ────────────────────────────────────────────────────────────────────
step "3 / 5  PyQt6"
if "$PYTHON" -c "import PyQt6" &>/dev/null 2>&1; then
    ok "PyQt6 already installed"
else
    info "Installing PyQt6…"
    sudo dnf install -y python3-pyqt6 &>/dev/null 2>&1 \
        && ok "Installed python3-pyqt6 via dnf" \
        || { "$PYTHON" -m pip install --user PyQt6 && ok "Installed PyQt6 via pip"; }
fi

# ── Install Crucible ──────────────────────────────────────────────────────────
step "4 / 5  Installing Crucible"

# Remove any existing install (editable or otherwise)
"$PYTHON" -m pip uninstall -y crucible &>/dev/null 2>&1 || true

# Copy source to its permanent home (independent of where you extracted the zip)
if [[ -d "$APP_HOME" ]]; then
    warn "Replacing existing install at $APP_HOME"
    rm -rf "$APP_HOME"
fi
cp -r "$HERE" "$APP_HOME"
ok "Source copied to $APP_HOME"

# Install for real — not editable, so the zip/extracted folder is now disposable
"$PYTHON" -m pip install --user --quiet "$APP_HOME"
ok "Crucible v0.3.2 installed"

# PATH guard
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    warn "~/.local/bin not in PATH yet — adding to ~/.bashrc"
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    export PATH="$LOCAL_BIN:$PATH"
    ok "Added to ~/.bashrc  (takes effect on next login / source ~/.bashrc)"
else
    ok "PATH is good"
fi

# ── Desktop launcher ──────────────────────────────────────────────────────────
step "5 / 5  App launcher entry"
mkdir -p "$APPS_DIR"

cat > "$APPS_DIR/crucible.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Crucible
GenericName=GTNH Server Manager
Comment=GT: New Horizons Server Manager
Exec=$LOCAL_BIN/crucible gui
Terminal=false
Categories=Game;Utility;
Keywords=GTNH;Minecraft;Server;Manager;
StartupWMClass=Crucible
EOF

ok "Added to app launcher  (search 'Crucible' in KDE)"

# ── tmux check ────────────────────────────────────────────────────────────────
echo ""
if command -v tmux &>/dev/null; then
    ok "tmux $(tmux -V) found"
else
    warn "tmux not found — install with: sudo dnf install tmux"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo -e ""
echo -e "${BOLD}${GREEN}┌─────────────────────────────────────────────────────┐"
echo -e "│   Done!  Crucible is installed.                     │"
echo -e "└─────────────────────────────────────────────────────┘${RESET}"
echo -e ""
echo -e "  ${BOLD}Launch:${RESET}  search 'Crucible' in your app launcher"
echo -e "           ${DIM}or${RESET}  ${CYAN}crucible gui${RESET}  in a terminal"
echo -e ""
echo -e "  ${BOLD}First time?${RESET}  Click  ${CYAN}+ Add Server${RESET}  in the GUI"
echo -e "               ${DIM}and browse to your GTNH server folder${RESET}"
echo -e ""
echo -e "  ${DIM}You can now safely delete the zip and extracted folder.${RESET}"
echo -e "  ${DIM}Crucible lives at: $APP_HOME${RESET}"
echo -e ""

# ── Offer to auto-delete the extraction folder ────────────────────────────────
# Only offer if HERE is somewhere throwaway (not already in APP_HOME)
if [[ "$HERE" != "$APP_HOME"* ]]; then
    PARENT="$(dirname "$HERE")"
    echo -e "  ${DIM}Extracted folder:  $HERE${RESET}"
    read -rp "  Delete it now? [Y/n]: " _ans
    _ans="${_ans:-Y}"
    if [[ "$_ans" =~ ^[Yy]$ ]]; then
        rm -rf "$HERE"
        # Also remove parent if it's now empty (e.g. a temp unzip dir)
        rmdir "$PARENT" 2>/dev/null || true
        ok "Cleaned up extraction folder"
    else
        info "Kept at $HERE — you can delete it any time."
    fi
fi

echo -e ""
