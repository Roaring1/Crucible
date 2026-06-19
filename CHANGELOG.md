# Changelog

## v0.4.2 — 2026-06-18

### Added

- **Create a brand-new server from scratch.** New "Create a new server — easiest"
  path in the Add Server dialog (`new_server_dialog.py`) and matching CLI
  (`crucible create-server`, `crucible list-mc-versions`). Vanilla is the
  simplest option; Fabric, Forge, NeoForge, and Quilt are also supported.
- **Server loader installer** (`crucible/importers/serverloader.py`): downloads
  and installs the correct server jar/launcher for a chosen Minecraft version
  and loader, including running the Forge/NeoForge installer jar
  (`--installServer`) when required.
- **Install server loader during Prism import.** `import-prism --install-server`
  and the GUI import path can now fetch a matching server loader automatically
  when the imported pack doesn't already include one.
- Setup tab gained an "Install server loader" action wired to the new
  installer.

## v0.4.1 — 2026-06-18

### Added

- **Mod downloader** (`crucible/importers/downloader.py`): best-effort
  Modrinth/CurseForge mod fetcher — threaded, retries, TLS, atomic writes,
  hash verification, skips server-unsupported mods, never raises.
- `import_prism_source` gained `download_mods` / `download_log_cb` /
  `download_progress_cb`; results are recorded in `import-summary.json` and
  surfaced as warnings.
- "Download from pack" button (Mods tab) with a progress dialog
  (`download_dialog.py`); the Add Server dialog offers a download step right
  after import.
- New easy-mode **Setup tab** (now the first tab): a readiness checklist with
  one-click Accept EULA, Open Folder, Copy Connection Address, and Download
  Mods actions.
- Add Server dialog reworked so Prism/modpack import is the primary path,
  with a new-folder picker; manual registration moved below.
- New `ServerInstance` helpers: `readiness()`, `eula_accepted()` /
  `set_eula_accepted()`, `java_info()`, `has_server_launcher()`,
  `server_port()`.
- New CLI commands: `download-mods`, `accept-eula`, `doctor`; plus
  `import-prism --download-mods`.

### Changed

- `crucible.data` and `crucible.process` now lazy-import their PyQt-backed
  classes, so CLI-only commands (and `--help`) work without PyQt6 installed.

### Fixed

- Hardened `_on_ip_fetched` (None-guard, always returns `ip:port`).
- Fixed a broken Minotar avatar URL; avatar cache moved to
  `~/.local/share/crucible/avatars`.

## v0.4.0 — 2026-06-18

### Added

- **Prism/Modrinth/CurseForge import** (`crucible/importers/prism.py`):
  imports installed Prism/MultiMC instances (`instance.cfg`), Modrinth
  `.mrpack` archives, and CurseForge/Flame pack zips into a server-ready
  folder, copying only server-safe content (`mods/`, `config/`,
  `defaultconfigs/`, `kubejs/`, `scripts/`, `patchouli_books/`, etc.) and
  skipping client-only content (`resourcepacks/`, `saves/`, `options.txt`,
  etc.).
- Generated server wrapper on import: `start.sh` (supports Forge, NeoForge,
  Fabric, Quilt, Paper, vanilla launch layouts), `server.properties`,
  `eula.txt` (defaults to `eula=false` unless `--accept-eula`),
  `.crucible/import-summary.json`, and `CRUCIBLE_IMPORT.md`.
- New CLI commands: `crucible scan-prism <path>` and
  `crucible import-prism <source> <target> [--overwrite] [--accept-eula]`.
- Add Server dialog gained "Import Prism Instance…" and "Import Modpack
  Archive…" actions.
- `ServerInstance` now stores `pack_source`, `minecraft_version`, `loader`,
  `loader_version`, `prism_source`; shown in the Info tab.
- `TmuxManager.start()` passes `java_args` into the generated start script via
  `CRUCIBLE_JAVA_ARGS`, safely shell-quoted.

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
