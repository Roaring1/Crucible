#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# get-crucible.sh  —  One-liner installer for Crucible
#
# Paste this into a terminal and hit Enter:
#
#   bash <(curl -sL https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
#
# What it does:
#   1. Asks the GitHub API for the latest release (no hardcoded version needed)
#   2. Downloads the release zip to /tmp
#   3. Extracts it and runs install.sh
#   4. Cleans up /tmp automatically
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BOLD="\033[1m"; GREEN="\033[32m"; CYAN="\033[36m"; RED="\033[31m"; RESET="\033[0m"

REPO="Roaring1/Crucible"
API_URL="https://api.github.com/repos/${REPO}/releases/latest"

echo -e ""
echo -e "${BOLD}${CYAN}  Crucible — one-liner installer${RESET}"
echo -e "  Checking latest release…"
echo -e ""

# ── Require curl ─────────────────────────────────────────────────────────────
if ! command -v curl &>/dev/null; then
    echo -e "${RED}  ✗  curl is required. Install it with: sudo apt install curl${RESET}"
    exit 1
fi

# ── Ask GitHub API for the latest release asset URL ──────────────────────────
RELEASE_JSON=$(curl -sL \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$API_URL")

# Pull out the first .zip asset's browser_download_url
RELEASE_URL=$(echo "$RELEASE_JSON" \
    | grep -o '"browser_download_url": *"[^"]*\.zip"' \
    | head -1 \
    | grep -o '"https://[^"]*"' \
    | tr -d '"')

TAG=$(echo "$RELEASE_JSON" \
    | grep -o '"tag_name": *"[^"]*"' \
    | head -1 \
    | grep -o '"[^"]*"$' \
    | tr -d '"')

if [[ -z "$RELEASE_URL" ]]; then
    echo -e "${RED}  ✗  Could not find a release zip on GitHub.${RESET}"
    echo -e "     Check: https://github.com/${REPO}/releases"
    exit 1
fi

echo -e "  ${CYAN}·${RESET}  Latest release: ${BOLD}${TAG}${RESET}"
echo -e "  ${CYAN}·${RESET}  Downloading: $(basename "$RELEASE_URL")"
echo -e ""

# ── Download ──────────────────────────────────────────────────────────────────
TMP_DIR="$(mktemp -d /tmp/crucible-install-XXXX)"
ZIP="$TMP_DIR/crucible.zip"

curl -L --progress-bar --fail "$RELEASE_URL" -o "$ZIP" || {
    echo -e "${RED}  ✗  Download failed.${RESET}"
    echo -e "     URL: $RELEASE_URL"
    rm -rf "$TMP_DIR"
    exit 1
}

# ── Sanity check ──────────────────────────────────────────────────────────────
if ! unzip -t "$ZIP" &>/dev/null; then
    echo -e "${RED}  ✗  Downloaded file is not a valid zip archive.${RESET}"
    echo -e "     This usually means the release asset is missing or corrupt."
    rm -rf "$TMP_DIR"
    exit 1
fi

# ── Extract ───────────────────────────────────────────────────────────────────
unzip -q "$ZIP" -d "$TMP_DIR"

# Find the extracted folder — handles both flat and single-wrapper zips
EXTRACTED=$(find "$TMP_DIR" -maxdepth 2 -name "install.sh" | head -1 | xargs dirname 2>/dev/null || true)

if [[ -z "$EXTRACTED" ]]; then
    echo -e "${RED}  ✗  Could not find install.sh inside the zip.${RESET}"
    rm -rf "$TMP_DIR"
    exit 1
fi

# ── Install ───────────────────────────────────────────────────────────────────
bash "$EXTRACTED/install.sh"

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -rf "$TMP_DIR"

echo -e ""
echo -e "${BOLD}${GREEN}  All done!${RESET}"
