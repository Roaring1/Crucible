#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# get-crucible.sh  —  One-liner installer for Crucible
#
# Paste this into a terminal and hit Enter:
#
#   bash <(curl -sL https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
#
# What it does:
#   1. Downloads the latest release zip to /tmp
#   2. Extracts it
#   3. Runs install.sh
#   4. Cleans up /tmp automatically
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOLD="\033[1m"; GREEN="\033[32m"; CYAN="\033[36m"; RED="\033[31m"; RESET="\033[0m"

# !! This tag must match your GitHub release tag exactly !!
# GitHub release tag: v0.3.2  →  https://github.com/Roaring1/Crucible/releases/tag/v0.3.2
RELEASE_URL="https://github.com/Roaring1/Crucible/releases/download/v0.3.2/crucible_v0_3_2.zip"

TMP_DIR="$(mktemp -d /tmp/crucible-install-XXXX)"
ZIP="$TMP_DIR/crucible.zip"

echo -e ""
echo -e "${BOLD}${CYAN}  Crucible — one-liner installer${RESET}"
echo -e "  Downloading latest release…"
echo -e ""

# Download
if command -v curl &>/dev/null; then
    curl -L --progress-bar --fail "$RELEASE_URL" -o "$ZIP" || {
        echo -e "${RED}  ✗  Download failed. Check that the release exists at:${RESET}"
        echo -e "     $RELEASE_URL"
        rm -rf "$TMP_DIR"
        exit 1
    }
elif command -v wget &>/dev/null; then
    wget -q --show-progress "$RELEASE_URL" -O "$ZIP" || {
        echo -e "${RED}  ✗  Download failed.${RESET}"
        rm -rf "$TMP_DIR"
        exit 1
    }
else
    echo -e "${RED}  ✗  curl or wget is required. Install with: sudo dnf install curl${RESET}"
    exit 1
fi

# Sanity check — make sure we got an actual zip, not an HTML error page
if ! unzip -t "$ZIP" &>/dev/null; then
    echo -e "${RED}  ✗  Downloaded file is not a valid zip archive.${RESET}"
    echo -e "     This usually means the release tag is wrong in the download URL."
    echo -e "     Expected: $RELEASE_URL"
    rm -rf "$TMP_DIR"
    exit 1
fi

# Extract
unzip -q "$ZIP" -d "$TMP_DIR"
EXTRACTED=$(find "$TMP_DIR" -maxdepth 1 -type d -name "crucible_*" | head -1)

if [[ -z "$EXTRACTED" ]]; then
    echo -e "${RED}  ✗  Could not find extracted folder inside the zip.${RESET}"
    rm -rf "$TMP_DIR"
    exit 1
fi

# Run install
bash "$EXTRACTED/install.sh"

# Cleanup
rm -rf "$TMP_DIR"

echo -e "${BOLD}${GREEN}  All done!${RESET}"
