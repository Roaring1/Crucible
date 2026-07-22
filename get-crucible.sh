#!/usr/bin/env bash
# Download, verify, and install the exact source asset from the latest release.
set -Eeuo pipefail

REPO="Roaring1/Crucible"
API_URL="https://api.github.com/repos/${REPO}/releases/latest"
BOLD="\033[1m"; GREEN="\033[32m"; CYAN="\033[36m"; RED="\033[31m"; RESET="\033[0m"
TMP_DIR=""
cleanup() {
    [[ -n "$TMP_DIR" && -d "$TMP_DIR" ]] && rm -rf -- "$TMP_DIR"
}
trap cleanup EXIT
fail() { printf '%b✗  %s%b\n' "$RED" "$*" "$RESET" >&2; exit 1; }

printf '\n%bCrucible — verified one-line installer%b\n\n' "$BOLD$CYAN" "$RESET"
for command_name in curl python3 unzip sha256sum; do
    command -v "$command_name" >/dev/null 2>&1 || \
        fail "$command_name is required. On Nobara/Fedora: sudo dnf install curl python3 unzip coreutils"
done

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/crucible-install.XXXXXX")"
RELEASE_JSON="$TMP_DIR/release.json"

curl --proto '=https' --tlsv1.2 --fail --show-error --silent --location \
    --retry 3 --retry-delay 1 --connect-timeout 15 --max-time 60 \
    -H 'Accept: application/vnd.github+json' \
    -H 'X-GitHub-Api-Version: 2022-11-28' \
    "$API_URL" -o "$RELEASE_JSON" || fail "Could not query the latest GitHub release."

# Print: tag, exact source asset name+URL, exact checksum asset name+URL.
# Use a real file rather than process substitution: EXIT traps are inherited by
# process-substitution shells and could otherwise remove TMP_DIR too early.
RELEASE_META="$TMP_DIR/release-meta.txt"
if ! python3 - "$RELEASE_JSON" "$REPO" > "$RELEASE_META" <<'PY'
import json
import re
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
repo = sys.argv[2]
tag = payload.get("tag_name", "")
if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?", tag):
    raise SystemExit("release tag is missing or unsafe")
assets = {a.get("name", ""): a.get("browser_download_url", "") for a in payload.get("assets", [])}
source_name = f"Crucible-{tag}-source.zip"
checksum_name = f"Crucible-{tag}-SHA256.txt"
for name in (source_name, checksum_name):
    url = assets.get(name, "")
    expected_prefix = f"https://github.com/{repo}/releases/download/"
    if not url.startswith(expected_prefix):
        raise SystemExit(f"required release asset is missing or has an unsafe URL: {name}")
print(tag)
print(source_name)
print(assets[source_name])
print(checksum_name)
print(assets[checksum_name])
PY
then
    fail "Latest release does not contain the exact source and checksum assets."
fi
mapfile -t RELEASE < "$RELEASE_META"

[[ ${#RELEASE[@]} -eq 5 ]] || fail "Could not parse release asset metadata."
TAG="${RELEASE[0]}"
SOURCE_NAME="${RELEASE[1]}"
SOURCE_URL="${RELEASE[2]}"
CHECKSUM_NAME="${RELEASE[3]}"
CHECKSUM_URL="${RELEASE[4]}"
ZIP="$TMP_DIR/$SOURCE_NAME"
SUMS="$TMP_DIR/$CHECKSUM_NAME"

printf '  %b·%b  Release: %b%s%b\n' "$CYAN" "$RESET" "$BOLD" "$TAG" "$RESET"
printf '  %b·%b  Asset: %s\n' "$CYAN" "$RESET" "$SOURCE_NAME"

curl --proto '=https' --tlsv1.2 --fail --show-error --location \
    --retry 3 --retry-delay 1 --connect-timeout 15 --max-time 1800 \
    --max-filesize 536870912 "$SOURCE_URL" -o "$ZIP" || fail "Source download failed."
curl --proto '=https' --tlsv1.2 --fail --show-error --silent --location \
    --retry 3 --retry-delay 1 --connect-timeout 15 --max-time 60 \
    --max-filesize 1048576 "$CHECKSUM_URL" -o "$SUMS" || fail "Checksum download failed."

EXPECTED="$(awk -v n="$SOURCE_NAME" '$2 == n || $2 == "*" n {print $1; exit}' "$SUMS")"
[[ "$EXPECTED" =~ ^[0-9a-fA-F]{64}$ ]] || fail "Checksum manifest has no valid entry for $SOURCE_NAME."
printf '%s  %s\n' "$EXPECTED" "$ZIP" | sha256sum -c - >/dev/null || fail "SHA-256 verification failed."
printf '  %b✓%b  SHA-256 verified\n' "$GREEN" "$RESET"

# Treat release contents as untrusted until paths, links, and expansion are checked.
python3 - "$ZIP" <<'PY'
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1])
max_members = 100_000
max_total = 4 * 1024**3
max_single = 2 * 1024**3
with zipfile.ZipFile(archive) as zf:
    infos = zf.infolist()
    if not infos or len(infos) > max_members:
        raise SystemExit("release ZIP has an unsafe member count")
    total = 0
    installers = 0
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        mode = (info.external_attr >> 16) & 0xFFFF
        if "\x00" in name or path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe release ZIP path: {name!r}")
        if stat.S_ISLNK(mode):
            raise SystemExit(f"release ZIP contains a symbolic link: {name!r}")
        if info.file_size > max_single:
            raise SystemExit(f"release ZIP member is too large: {name!r}")
        total += info.file_size
        if total > max_total:
            raise SystemExit("release ZIP expands beyond the safety limit")
        if path.name == "install.sh" and not info.is_dir():
            installers += 1
    if installers != 1:
        raise SystemExit(f"release ZIP must contain exactly one install.sh, found {installers}")
    bad = zf.testzip()
    if bad:
        raise SystemExit(f"release ZIP failed CRC verification at {bad!r}")
PY

unzip -q "$ZIP" -d "$TMP_DIR/extracted"
INSTALLER="$(find "$TMP_DIR/extracted" -maxdepth 3 -type f -name install.sh -print -quit)"
[[ -n "$INSTALLER" ]] || fail "Verified archive did not extract an install.sh."
chmod u+x "$INSTALLER"
bash "$INSTALLER"

printf '\n%bAll done — %s installed from a verified release.%b\n\n' "$BOLD$GREEN" "$TAG" "$RESET"
