# Changelog

## v0.4.8 — 2026-06-19

### Fixed

- **Client-only mods no longer crash dedicated servers (auto-fix).** A new
  loading-crash diagnostics engine recognises the classic
  `Attempted to load class …/Screen for invalid dist DEDICATED_SERVER`
  failure (e.g. *Status Effect Bars* in Create+), reads the crash report,
  figures out which jar provides the offending mod id (Fabric `fabric.mod.json`
  + NeoForge/Forge `mods.toml`), and quarantines just that client-only jar by
  renaming it to `*.jar.disabled` — your server-side mods are never touched.
  Re-enable any time from the Mods tab. It also recognises missing/unsupported
  mandatory dependencies and duplicate mods for clearer guidance.

### Added

- **"Fix loading errors…" (right-click a server in the sidebar).** Diagnoses
  the latest crash and offers to disable the culprit client-only mod(s) with one
  click, then tells you to start again.
- **`crucible fix-loading <name>` CLI.** `--apply` to quarantine, `--scan` for a
  static client-only scan (no crash needed), `--restore` to re-enable previously
  quarantined mods, and `--log FILE` to analyse a specific crash/log file.
- **Sidebar drag & drop.** Drag servers in the left pane to reorder them (the
  order is saved). Drop a Prism/MultiMC instance, `.mrpack`, `.zip`, or server
  folder onto the list to import it. Drag a server *out* of the list to copy its
  folder into a file manager, and use the new **"Export for Prism…"** right-click
  action to package a server as a `.zip` you can import via Prism's
  *Add Instance → Import from zip*.

## v0.4.7 — 2026-06-19

### Added

- **One-click server-side modpack hosting.** A new "Install a modpack instead…"
  button in the New Server dialog opens a Modrinth modpack browser. Pick a pack
  and Crucible does everything: downloads the correct dedicated-server loader
  (Fabric/Forge/NeoForge/Quilt/Vanilla) at the pack's Minecraft version, pulls
  every server-side mod from the pack manifest (skipping client-only mods),
  applies the pack's `server-overrides`/`overrides` (configs, server.properties,
  etc.), writes the start script and EULA, and registers the instance — no
  manual mod wrangling. Also available headless via
  `crucible install-modpack <id-or-slug>` and `--mrpack <file>`.
- **Add-a-mod browser: infinite scroll.** Results now load 30 at a time and
  fetch more automatically as you scroll, instead of a single fixed page.
- **Add-a-mod browser: Client / Server filter.** A filter row at the top lets
  you show client- and/or server-side mods (both on by default).
- **Mods tab: per-mod actions.** Right-click any installed mod to Enable/
  Disable it, Check for updates (Modrinth), Verify dependencies, open its
  Modrinth page, or Delete it. Dependency verification scans every recorded mod
  and reports anything missing.

## v0.4.6 — 2026-06-19

### Added

- **Mod browser reworked into a Prism-style UI** (`add_mod_dialog.py`): opens
  to the most popular mods compatible with the server's MC version/loader
  (`modrinth.browse_popular`), shows icons, author, download/follower counts,
  and a detail panel with description, server-compatibility tag
  (client-only / server-required / server-ready), and a live dependency
  preview before installing. Icons and dependency resolution load on
  background threads so the UI never blocks.
- `crucible/mods/modrinth.py` gained `browse_popular()`, `fetch_bytes()`
  (for icons), `humanize_count()` (754M / 134K style formatting), and richer
  `ModHit` fields (`author`, `icon_url`, `follows`, `client_side`,
  `server_side`).

### Changed

- **New Server dialog**: the destination folder now auto-derives from the
  display name as you type, until you manually edit the folder field or use
  Browse… (`new_server_dialog.py`) — previously the name was guessed from the
  folder, which was backwards for the common case.
- Disabled-button contrast improved in the theme (was unreadably dim).

## v0.4.5 — 2026-06-19

### Fixed

- **Server fails to bind its port on machines with IPv6 disabled.** New
  `crucible/process/netfix.py` forces the JVM onto the IPv4 stack
  (`-Djava.net.preferIPv4Stack=true` / `-Djava.net.preferIPv6Addresses=false`)
  on every start, fixing Netty bind error `-97`
  (`Address family not supported by protocol`) — which looks like a
  port-in-use problem but isn't, and changing the port does nothing. Applied
  both via `CRUCIBLE_JAVA_ARGS` (Crucible's own `start.sh`) and by patching
  `user_jvm_args.txt` for modern Forge/NeoForge run scripts that read JVM
  flags from there instead.
- **Faster start/stop status updates.** A new fast transition poll
  (`_TRANSITION_POLL_MS` = 1.2s) resolves the "starting…"/"stopping…" header
  as soon as the tmux session actually settles, instead of waiting up to the
  5s health-check interval.

## v0.4.4 — 2026-06-19

### Added

- **"Copy connection address" now offers loopback / LAN / public separately**
  (`crucible/data/netinfo.py`): instantly shows `127.0.0.1:<port>` (this PC
  only) and the detected LAN IP (e.g. `192.168.x.x:<port>`, for other devices
  on the same network) with no network calls, and fetches the public WAN IP
  (needs port-forwarding) as a third option. Replaces the old single
  ipify-only public-IP button, which was misleading when testing from the
  same network as the server (NAT hairpinning doesn't loop back to it).
- **Player actions menu** (Players tab): right-click or double-click an
  online player for op/deop, gamemode, teleport to spawn, give item,
  whisper, kick, ban, and pardon — sent as console commands via tmux.
- **Resource monitor** (`crucible/process/resource_monitor.py`): dependency-free
  per-server CPU% / RSS sampling read from `/proc`, surfaced in a new
  **System tab** (`system_tab.py`).
- **Modrinth mod browser/search** (`crucible/mods/modrinth.py`) and an
  "Add mod" dialog (`add_mod_dialog.py`) for searching and installing mods
  directly from the Mods tab.
- **Client pack export** (`crucible/exporters/client_export.py` +
  `client_export_dialog.py`): builds a Prism/MultiMC-importable client pack
  from a running server's mods/config so players can join with matching mods.
- **Drag-and-drop import.** The main window now accepts dropped Prism
  instances, modpack archives, or server folders directly onto the sidebar.
- Console tab gained quick-command affordances.

## v0.4.3 — 2026-06-19

### Added

- **`server.properties` validator and auto-repair** (`crucible/data/properties.py`):
  detects invalid values (e.g. a blank `server-port=` that crashes the server
  with a `NumberFormatException` before it boots), suggests fixes for unknown
  keys, and can auto-correct crash-causing values (advisory ones need
  `--all`). Always writes a `.bak` backup before changing anything.
- **Pre-flight auto-repair before server start.** Both the GUI (Start/Restart
  buttons in `instance_panel.py`) and CLI now run the properties check before
  launch and silently fix blocking errors, surfacing what changed.
- New GUI dialog `properties_dialog.py` (`PropertiesFixDialog`) and a
  "Check settings" button on the Setup tab.
- New CLI command: `crucible fix-properties NAME [--apply] [--all]`.

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
