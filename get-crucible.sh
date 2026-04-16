#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# get-crucible.sh  —  One-liner installer for Crucible
#
# Paste this into a terminal and hit Enter. That's it.
#
#   bash <(curl -sL https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
#
# What it does:
#   1. Downloads the latest release zip to /tmp
#   2. Extracts it to /tmp
#   3. Runs install.sh
#   4. Cleans up /tmp automatically
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOLD="\033[1m"; GREEN="\033[32m"; CYAN="\033[36m"; RESET="\033[0m"

RELEASE_URL="https://github.com/Roaring1/Crucible/releases/download/v0.3.2/crucible_v0_3_2.zip"
TMP_DIR="$(mktemp -d /tmp/crucible-install-XXXX)"
ZIP="$TMP_DIR/crucible.zip"

echo -e ""
echo -e "${BOLD}${CYAN}  Crucible — one-liner installer${RESET}"
echo -e "  Downloading from GitHub…"
echo -e ""

# Download
if command -v curl &>/dev/null; then
    curl -L --progress-bar "$RELEASE_URL" -o "$ZIP"
elif command -v wget &>/dev/null; then
    wget -q --show-progress "$RELEASE_URL" -O "$ZIP"
else
    echo "  ✗  curl or wget required. Install with: sudo dnf install curl"
    exit 1
fi

# Extract
unzip -q "$ZIP" -d "$TMP_DIR"
EXTRACTED=$(find "$TMP_DIR" -maxdepth 1 -type d -name "crucible_*" | head -1)

if [[ -z "$EXTRACTED" ]]; then
    echo "  ✗  Could not find extracted folder. Download may have failed."
    exit 1
fi

# Run install
bash "$EXTRACTED/install.sh"

# Cleanup (install.sh already handles the extracted folder, clean the tmp zip)
rm -f "$ZIP"
rmdir "$TMP_DIR" 2>/dev/null || true

echo -e "${BOLD}${GREEN}  All done!${RESET}"
