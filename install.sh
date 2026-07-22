#!/usr/bin/env bash
# Crucible v0.6.7 installer — Nobara/Fedora, Python 3.11+
set -Eeuo pipefail

VERSION="0.6.7"
BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"
RED="\033[31m"; CYAN="\033[36m"; DIM="\033[2m"; RESET="\033[0m"
ok()   { printf '  %b✓%b  %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %b⚠%b  %s\n' "$YELLOW" "$RESET" "$*"; }
err()  { printf '  %b✗%b  %s\n' "$RED" "$RESET" "$*" >&2; }
info() { printf '  %b·%b  %s\n' "$CYAN" "$RESET" "$*"; }
step() { printf '\n%b── %s%b\n' "$BOLD" "$*" "$RESET"; }

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
APP_PARENT="$HOME/.local/share"
APP_HOME="$APP_PARENT/crucible"
LOCAL_BIN="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
BACKUP_HOME="$HOME/.local/share/crucible-backups"
STAGE=""
OLD_APP=""
COMMITTED=0

cleanup() {
    local code=$?
    if [[ -n "$STAGE" && -d "$STAGE" ]]; then
        rm -rf -- "$STAGE"
    fi
    if [[ $code -ne 0 && $COMMITTED -eq 0 && -n "$OLD_APP" && -d "$OLD_APP" ]]; then
        rm -rf -- "$APP_HOME"
        mv -- "$OLD_APP" "$APP_HOME" || true
        ln -sfn "$APP_HOME/bin/crucible" "$LOCAL_BIN/crucible" || true
        warn "Restored the previous Crucible installation after failure."
    fi
    return "$code"
}
trap cleanup EXIT

printf '\n%bCrucible v%s — safe installer / updater%b\n\n' "$BOLD$CYAN" "$VERSION" "$RESET"
info "Source: $HERE"
info "Install location: $APP_HOME"

[[ -f "$HERE/pyproject.toml" && -f "$HERE/crucible/__init__.py" ]] || {
    err "This folder is not a complete Crucible source package."
    exit 1
}
[[ -f "$HERE/crucible/assets/crucible.png" ]] || {
    err "Required icon is missing: crucible/assets/crucible.png"
    exit 1
}

step "1 / 6  Python"
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        PYTHON="$candidate"
        break
    fi
done
[[ -n "$PYTHON" ]] || {
    err "Python 3.11 or newer is required. On Nobara/Fedora: sudo dnf install python3"
    exit 1
}
ok "Using $($PYTHON --version 2>&1)"

step "2 / 6  System dependencies"
if ! "$PYTHON" -m venv --help >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
        info "Installing Python virtual-environment support…"
        sudo dnf install -y python3
    else
        err "Python's venv module is required. Install the Python venv package for this distribution."
        exit 1
    fi
fi
if ! "$PYTHON" -c 'import PyQt6' >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
        info "Installing Fedora/Nobara PyQt6 package…"
        sudo dnf install -y python3-pyqt6
    else
        warn "System PyQt6 not found; the isolated environment will install it with pip."
    fi
fi
if ! command -v tmux >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
        info "Installing tmux…"
        sudo dnf install -y tmux
    else
        warn "tmux is required to run servers; install it with your package manager."
    fi
fi

step "3 / 6  Stage and validate"
mkdir -p -- "$APP_PARENT" "$LOCAL_BIN" "$APPS_DIR" "$ICON_DIR" "$BACKUP_HOME"
STAGE="$(mktemp -d "$APP_PARENT/.crucible-stage.XXXXXX")"
mkdir -p -- "$STAGE/app/source"
# Copy only distributable source. This never copies a checkout's .git directory,
# caches, build output, or a previous virtual environment.
tar -C "$HERE" \
    --exclude='./.git' --exclude='./.mypy_cache' --exclude='./.ruff_cache' \
    --exclude='./.pytest_cache' --exclude='./build' --exclude='./dist' \
    --exclude='./*.egg-info' --exclude='__pycache__' --exclude='*.pyc' \
    -cf - . | tar -C "$STAGE/app/source" -xf -

"$PYTHON" -m venv --system-site-packages "$STAGE/app/venv"
VENV_PY="$STAGE/app/venv/bin/python"
VENV_PIP="$STAGE/app/venv/bin/pip"
if ! "$VENV_PY" -c 'import PyQt6' >/dev/null 2>&1; then
    info "Installing PyQt6 into Crucible's isolated environment…"
    "$VENV_PIP" install --disable-pip-version-check 'PyQt6>=6.5'
fi
mkdir -p "$STAGE/app/bin"
cat > "$STAGE/app/bin/crucible" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
SELF="$(readlink -f -- "${BASH_SOURCE[0]}")"
APP_ROOT="$(cd -- "$(dirname -- "$SELF")/.." && pwd -P)"
export PYTHONPATH="$APP_ROOT/source${PYTHONPATH:+:$PYTHONPATH}"
exec "$APP_ROOT/venv/bin/python" -m crucible "$@"
EOF
chmod 0755 "$STAGE/app/bin/crucible"
"$VENV_PY" -m compileall -q "$STAGE/app/source/crucible"
"$STAGE/app/bin/crucible" --help >/dev/null
PYTHONPATH="$STAGE/app/source${PYTHONPATH:+:$PYTHONPATH}" \
    "$VENV_PY" -c 'import PyQt6; import crucible; print(crucible.__version__)' \
    | grep -Fx "$VERSION" >/dev/null
ok "Staged copy passed import, compile, and CLI smoke tests"

step "4 / 6  Preserve data and replace atomically"
# Backups are outside APP_HOME. Copy any legacy in-tree backups without
# overwriting files already migrated by an earlier release.
if [[ -d "$APP_HOME/backups" ]]; then
    cp -a -n "$APP_HOME/backups/." "$BACKUP_HOME/" || true
    ok "Legacy backups copied to $BACKUP_HOME"
fi
if [[ -e "$APP_HOME" ]]; then
    OLD_APP="$APP_PARENT/.crucible-previous.$$"
    mv -- "$APP_HOME" "$OLD_APP"
fi
if ! mv -- "$STAGE/app" "$APP_HOME"; then
    err "Could not publish the staged installation."
    [[ -n "$OLD_APP" && -d "$OLD_APP" ]] && mv -- "$OLD_APP" "$APP_HOME"
    exit 1
fi
# Smoke-test the final path before deleting the rollback copy.
"$APP_HOME/bin/crucible" --help >/dev/null
ln -sfn "$APP_HOME/bin/crucible" "$LOCAL_BIN/.crucible.new"
mv -Tf "$LOCAL_BIN/.crucible.new" "$LOCAL_BIN/crucible"
ok "Crucible v$VERSION published"

step "5 / 6  Desktop integration"
cp -- "$APP_HOME/source/crucible/assets/crucible.png" "$ICON_DIR/crucible.png"
cat > "$APPS_DIR/crucible.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Crucible
GenericName=Minecraft Server Manager
Comment=Manage Minecraft dedicated servers
Exec=$APP_HOME/bin/crucible gui
TryExec=$APP_HOME/bin/crucible
Terminal=false
Categories=Game;Utility;
Keywords=Minecraft;Server;GTNH;Manager;
StartupWMClass=crucible
StartupNotify=true
Icon=crucible
EOF
chmod 0644 "$APPS_DIR/crucible.desktop"
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
command -v update-desktop-database >/dev/null 2>&1 && \
    update-desktop-database "$APPS_DIR" >/dev/null 2>&1 || true
ok "KDE application entry and icon installed"

step "6 / 6  PATH and final check"
if [[ ":$PATH:" != *":$LOCAL_BIN:"* ]]; then
    if ! grep -Fqx 'export PATH="$HOME/.local/bin:$PATH"' "$HOME/.bashrc" 2>/dev/null; then
        printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$HOME/.bashrc"
    fi
    warn "Added ~/.local/bin to ~/.bashrc; it applies in new terminals."
fi
[[ -x "$APP_HOME/bin/crucible" ]] || { err "Final launcher check failed"; exit 1; }
command -v tmux >/dev/null 2>&1 && ok "tmux $(tmux -V)" || warn "Install tmux before starting a server."
COMMITTED=1
if [[ -n "$OLD_APP" && -d "$OLD_APP" ]]; then
    rm -rf -- "$OLD_APP"
    OLD_APP=""
fi

printf '\n%bCrucible v%s is installed.%b\n' "$BOLD$GREEN" "$VERSION" "$RESET"
printf '  Launch from KDE, or run: %s\n' "$LOCAL_BIN/crucible gui"
printf '  Backups: %s\n' "$BACKUP_HOME"
printf '  Installed source: %s\n' "$APP_HOME/source"
printf '  This installer never deletes the folder it was launched from.\n\n'
