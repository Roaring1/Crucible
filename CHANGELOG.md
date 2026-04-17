# Changelog

## v0.3.4 — 2026-04-17

### Fixed

- **Sidebar is now freely resizable.** Removed the hard 300 px max-width cap;
  the splitter handle is now visible and draggable. The pane can also be
  collapsed completely by double-clicking the handle.

- **Config tab: true/false properties now use a dropdown.** Boolean values in
  `server.properties` show a green/red `QComboBox` (true / false) instead of a
  plain text cell — one click to toggle, no typos possible.

- **Server stuck on "Starting…" — fixed.** Root cause: `_TmuxWorker` was being
  garbage-collected by Python before its QThread had a chance to run it.
  PyQt6 holds only a weak reference to connected bound methods, so the worker
  object vanished, the `finished` signal never fired, and the button stayed
  frozen. Fix: `InstancePanel` now keeps a strong reference to every worker
  until its thread finishes.

- **Console printing no lines while stuck on "Starting…" — fixed.** Same root
  cause as above (thread never ran → tmux session never opened → no log file).
  The console status line now shows "⏳ Waiting for server log…" immediately
  after attach so you can see that the watcher is active.

- **Player heads added.** The "Online Now" list in the Players tab now shows
  each player's current skin face sprite (fetched asynchronously from
  `minotar.net`). Avatars are cached for the session; any fetch failure is
  silently ignored so the tab still works offline.

- **Backups no longer deleted on reinstall.** Backup storage moved from
  `~/.local/share/crucible/backups/` (inside `APP_HOME`, wiped by every
  `install.sh` run) to `~/.local/share/crucible-backups/` (separate directory,
  never touched by the installer). `install.sh` automatically migrates any
  existing backups from the old location on first run.



### Fixed / Improved
- **Install is now genuinely self-contained.** `install.sh` works from anywhere —
  Downloads, Desktop, /tmp — it copies itself to `~/.local/share/crucible/` before
  installing, so the extracted zip folder is fully disposable after install.
- **No more editable-mode (`-e`) install.** The old approach left the source code
  sitting wherever you extracted the zip and required it to stay there forever.
  v0.3.2 does a real install: you can delete the zip and extracted folder immediately.
- **Auto-cleanup prompt.** At the end of install, the script offers to delete
  the extracted folder for you.
- **One-liner installer.** `get-crucible.sh` lets you install with a single
  `bash <(curl …)` command — no manual downloading or extracting.
- **Flat zip structure.** Removed the confusing nested `build/` folder.
  The zip now extracts to `crucible_v0_3_2/` with `install.sh` at the top level.
- **PATH fix is automatic.** If `~/.local/bin` wasn't in your PATH, the installer
  adds it to `~/.bashrc` without asking.

## v0.3.1 — 2026-04-16

- Initial release with GUI and CLI
- tmux-backed server management
- Backup tab, mods tab, config tab, console tab, players tab
- Instance registry at `~/.config/crucible/instances.json`
