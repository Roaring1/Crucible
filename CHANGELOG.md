# Changelog

## v0.6.11

- **Added: whole-host crash recovery.** Watchdog only ever detected a crash while Crucible itself was open and polling tmux -- if the entire PC lost power or hard-froze, Crucible, tmux, and the server all died together and nobody was left to notice, so the next launch looked identical to a normal, intentional stop. `crucible/process/crash_recovery.py` now records a small heartbeat (current Linux boot id + tmux session) whenever an instance is confirmed running, and updates it on every graceful stop or live-witnessed crash. On startup, if a heartbeat still says "running" under a *different* boot id than the current one, with tmux confirming the session is gone, Crucible now knows the whole host went down and reports it -- and auto-restarts that instance if Auto-Restart is enabled for it.
- Added `tests/test_crash_recovery.py` (15 tests) covering heartbeat persistence, boot-id reconciliation, and torn-log corroboration. Suite is now 132 tests, all passing.

## v0.6.10

- **Fixed:** importing a Prism source that was already a complete, ready-to-run dedicated server (e.g. an official modpack "Server Pack" download such as GTNH's) silently dropped the real start script and server jar/loader. The importer only ever copied a narrow client-import allowlist (mods/config/etc.), which is correct for a bare Prism *client* instance but was quietly deleting the actual launcher for anyone importing a pre-built server. Crucible now detects this case (start script or server jar/loader present at the top level of the source) and copies the entire folder instead, and never overwrites a real copied start script with its own placeholder.
- Copied start scripts and known launcher script names now have their executable bit restored after import, in case the source zip lost it.
- Added regression tests covering both the bare-client-instance path (still gets the placeholder + warning) and the prebuilt-server path (real jar/script survive import byte-for-byte). Suite is now 117 tests.

## v0.6.9 -- 2026-07-22 -- fix false-positive "Server program is installed" check; warn before auto-installing Forge on legacy/heavily-modded packs
- **Fixed a real false-positive** reported live by a user importing a GT New Horizons Prism client instance: the Setup tab's checklist showed a green checkmark for "Server program is installed" and every other item green too, yet pressing Start immediately failed with exit code 2. Root cause: `ServerInstance.readiness()` treated the mere *existence* of a file named `start.sh` as proof a server program was installed -- but that `start.sh` was Crucible's own placeholder, written by the Prism importer whenever no real dedicated server jar/loader is found, which always exits 2 until a real jar/loader shows up. A pack-only import (client files with no dedicated server) always has this placeholder present, so the checklist never actually flagged the real problem or surfaced the existing "Install server now" button.
- `readiness()` now recognizes Crucible's own placeholder `start.sh` by its unique fallback-error marker text (`_is_placeholder_start_script()` in `crucible/data/instance_model.py`) and only reports the server program as installed when a real server jar/loader is found, or the discovered start script is a genuine pack-provided launcher (GTNH's `ServerStart.sh`, etc.) -- not Crucible's own stub. The checklist item now correctly turns red with an "Install server now" button and an accurate explanation.
- **Added a warning before auto-installing Forge for Minecraft 1.12.2 and earlier** (`crucible/ui/tabs/setup_tab.py`): heavily modified packs like GT New Horizons patch Forge themselves and publish their own official "Server Pack" download that matches their mods exactly -- Crucible's one-click installer can only fetch the plain public Forge build for that Minecraft version, which is not guaranteed to match. The Setup tab now warns about this and recommends the pack's own Server Pack download before proceeding, instead of silently installing a build that may be subtly incompatible.
- Added `tests/test_readiness_placeholder_detection.py` (6 tests) covering: the detector matches the real importer template, a folder with only the placeholder reports not-installed with the right fix hint, a folder with a genuine pack script reports installed, and a real jar dropped in alongside the placeholder still correctly flips the check to installed. Suite is now 112 tests, all passing.

## v0.6.8 -- 2026-07-21 -- hotfix: World tab "QThread destroyed while running" crash
- Fix a real, reproducible crash (`QThread: Destroyed while thread '' is still running` / `Aborted (core dumped)`) introduced by v0.6.7's World tab background world-size scanner. `WorldTab._start_stats_worker()` unconditionally created a new `QThread` and overwrote `self._stats_thread`, even when a previous scan's thread was still alive -- reachable through completely ordinary use (opening the World tab and clicking Refresh again before the first scan finished, or a completed backup/swap/reset/wipe triggering its own post-op refresh while the tab-load scan was still running on a large GTNH-scale world).
- `_start_stats_worker()` now queues the newest request in `self._stats_pending` and returns immediately if a scan is still running, instead of touching the live `QThread` reference at all. The queued request is only started from `_stats_thread_finished()`, which only runs after the thread has actually stopped -- never destroying a still-running `QThread`.
- Added `tests/test_world_stats_thread_guard.py` (3 tests) as a permanent regression guard for this exact pattern. Suite is now 106 tests, all passing.

## v0.6.7 -- 2026-07-21 -- World tab overhaul, memory suggestion, tab reorg, UI polish
- **World tab overhaul** (`crucible/ui/tabs/world_tab.py`, full rewrite): added "Set Seed", "Reset World" (start-over, keeps a mandatory safety backup and renames the old world aside so it's recoverable), and "Wipe World" (permanent delete, requires typing `WIPE` to confirm, no safety backup by design) alongside the existing named backup/swap workflow. World size and per-dimension stats are now computed on a background worker thread instead of the GUI thread, with collapsible dimension display once a world has more than 6 dimensions, fixing a UI freeze/"Not Responding" spell when opening the tab on large modpack worlds (e.g. GTNH's many dozens of dimension folders).
- **New world logic in `BackupManager`** (`crucible/data/backup_manager.py`): `reset_world()` + `ResetWorldWorker`, `wipe_world()` + `WipeWorldWorker`, following the same background-thread and safety-backup conventions as the existing swap workflow.
- **Suggested server memory**: the Setup tab's memory editor now shows a "Suggested for this machine" amount with a one-click "Apply suggested" button, plus a **GB/MB unit dropdown** next to each of `-Xms`/`-Xmx` so values can be entered in either unit (`crucible/ui/tabs/setup_tab.py`). The suggestion reserves the larger of 2 GB or 25% of total RAM for the OS/everything else, then clamps the remainder to a sane 1-16 GB range.
- **Tab reorganization**: reordered to Setup, Console, Mods, World, Config, Backups, Players, Notes, Info, and folded the standalone System tab's live performance stats into the bottom of the Info tab instead of a separate tab (`crucible/ui/instance_panel.py`, `crucible/ui/tabs/info_tab.py`). `ConfigTab` gained `reload_from_disk()` so the World tab can refresh Config's in-memory state after a swap/reset/wipe changes `level-name` on disk.
- **Readability/width fixes**: the "Add Server Instance" dialog's register-existing-server fields (Server path, Display name, Version, tmux session) no longer clip their text/placeholders -- the dialog is wider and each field has an explicit minimum width (`crucible/ui/add_dialog.py`).
- **More sidebar right-click options**: "Open server folder", "Open backups folder", and "Copy server path" were added to the instance right-click menu, alongside also repairing a pre-existing text-encoding corruption that had silently turned the "Remove from Crucible..." menu item's trash icon into replacement-character boxes (`crucible/ui/sidebar.py`).
- Expand the suite from 93 to 103 tests, covering World Reset/Wipe (safety backup taken, rename-aside recovery, no-op on an already-empty world, permanent delete with no backup) and the suggested-memory heuristic (default fallback, 512 MB rounding, 1-16 GB clamping, monotonic with more RAM).

## v0.6.5 -- hotfix: World tab crash on launch
- Fix `NameError: name 'WorldTab' is not defined` when opening the GUI. v0.6.4 referenced the new `WorldTab` class in `instance_panel.py` but the import statement at the top of that file was never updated to include it.

## v0.6.4 -- 2026-07-21 -- World Backup & Swap
- Add reliable **world identification** to `ServerInstance` (`crucible/data/instance_model.py`): `world_root_path()` resolves `level-name` from `server.properties` (defaulting to `"world"` when unset/missing, matching vanilla behavior), `world_size_bytes()` recursively sums the world folder's size, and `world_dimension_dirs()`/`dimension_label()` detect `DIM*` sibling folders (e.g. `DIM1` = The End, `DIM-1` = The Nether) nested inside the world root, matching the Forge/Fabric/modern-Paper layout used by modpacks like GTNH.
- Extend `BackupEntry`/`BackupManager` (`crucible/data/backup_manager.py`) with **named world slots**: an optional user-supplied `slot_name` stored in a small backward-compatible sidecar JSON file per backup, `rename_slot()`, and prune-exemption for named slots so an intentional checkpoint (e.g. "Pre-1.20 update") is never silently deleted by count/age-based pruning -- only unnamed auto-backups are prunable by default.
- Add **post-write integrity verification**: every backup is re-opened immediately after writing and checked for a valid, non-empty `level.dat` before being reported as successful; a backup that would have silently truncated now raises instead of being trusted.
- Add a **safe world swap workflow** (`BackupManager.swap_world()` + `SwapWorker`): refuses to run unless the live tmux status is exactly `stopped` (fails closed on `running`, `unknown`, `unmanaged`, `missing`, and `tmux_missing`), always takes an automatic pre-swap safety backup of the current world first, performs an atomic rename-based swap (never in-place file copying), verifies `level.dat` and dimension-folder counts after extraction, and **automatically rolls back** to the pre-swap folder if verification fails at any point -- the live world is never left in a half-swapped state. Old pre-swap folders are kept as an extra recovery net and can be pruned on demand via `prune_pre_swap_dirs()`.
- Add a new **World tab** (`crucible/ui/tabs/world_tab.py`): current-world summary (name, size, dimensions present), a named "Backup this world..." quick action with the same tmux-aware save-flush gating as the Backups tab, a saved-worlds table with per-slot Swap/Rename/Delete actions, an explicit pre-swap confirmation dialog that spells out every safety step before it runs, and a pre-swap-folder cleanup control. Wired into `InstancePanel` alongside the existing tabs, including its own busy-state in the panel's active-operation and close-guard checks.
- Cross-link `ConfigTab`'s existing `level-name` danger warning to the new World tab's Swap action, since editing `level-name` directly bypasses every safety check the swap workflow provides.
- Expand the suite from 78 to 93 tests, covering world identification, named-slot metadata and pruning, backup integrity verification, and world swap (success, rollback on verification failure, mismatched-`level-name` refusal, and first-time swap-in with no prior world).

## v0.6.3 — 2026-07-21 — starting-status fallback detection + in-app memory editor
- Fix GUI status potentially getting stuck on "STARTING..." indefinitely if log-file-based "Done" detection missed the startup line. While status is `starting` and the tmux session is confirmed alive, the panel now also captures the live tmux pane tail and searches it for the same `Done (Xs)!` pattern used by the log watcher (shared from a new Qt-free `crucible/process/startup_patterns.py` module so the two detection paths can never drift apart); a match promotes `starting` -> `running` immediately even if the log-file watcher missed it.
- Add `TmuxManager.capture_pane_tail()` as a public, bounded (200 lines / 8000 chars) way to read the current pane's recent output for this fallback check.
- Add an in-app **server memory (Java heap) editor** to the Setup tab: shows this machine's total RAM, lets you set `-Xms`/`-Xmx` in MB, and saves by rewriting only those two flags inside `java_args` -- every other flag (`@java9args.txt`, IPv4 stack flags, GC tuning, etc.) is preserved exactly. Warns if the requested `-Xmx` exceeds or is close to the machine's installed RAM, and rejects `-Xms` > `-Xmx` or non-positive values before saving. Takes effect on the next server start; does not resize an already-running JVM's heap.
- Add `ServerInstance.get_memory_mb()` / `set_memory_mb()` helpers (`crucible/data/instance_model.py`) backing the new editor, with support for existing `K`/`M`/`G`-suffixed values and for java_args that have no `-Xms`/`-Xmx` tokens yet.
- Expand the suite from 71 to 78 tests, covering the new memory parsing/rewriting logic (unit conversion, flag preservation, insertion when absent, min > max, and non-positive validation).

## v0.6.2 — 2026-07-21 — GTNH reboot-wrapper crash/stop detection
- Root cause of "Stop looks like it's being restarted": GTNH's own `startserver-java9.sh`/`.bat` wrap java in a `while true` loop that auto-reboots java ~12 seconds after ANY exit (crash or graceful stop) unless Ctrl-C is sent during the countdown. Because `probe_running()` only checked `tmux has-session` (which the wrapper keeps alive forever), Crucible could never see this.
- Add `TmuxManager.pane_current_command()` and `TmuxManager.is_java_foreground()` to inspect the tmux pane's actual foreground process instead of only the session's existence.
- `stop()` now sends `Ctrl-C` to the pane the first time it observes java is no longer the foreground process (while the session is still alive), interrupting the wrapper's reboot countdown before it can relaunch java; a clearer failure message is reported if the session still doesn't close afterward.
- `Watchdog._poll()` now also treats `is_java_foreground() == False` held for `CRASH_CONFIRM_POLLS` consecutive polls as a confirmed crash, even while `probe_running()` stays `True` under a reboot-wrapper session, so real crashes are detected and reported instead of silently missed.
- Confirmed the existing `TmuxManager.start()` / `is_running()` guard already prevents a duplicate/competing java launch if Crucible's own auto-restart fires while the wrapper script's own countdown is also about to relaunch java.
- Diagnosed two distinct, unrelated `SIGSEGV` crashes reported against GTNH 2.8.4 on OpenJDK Temurin 25+36: a JIT/C2 compiler crash in `ShapedRecipes.func_77569_a`, and a native `libc __strchr_avx2` segfault — both crash types are independent of this wrapper-loop fix; consider Java 17/21 LTS as a more battle-tested runtime if either recurs.
- Expand the suite from 60 to 68 tests, covering pane-foreground detection, Stop's Ctrl-C interrupt behavior, and watchdog crash detection under a surviving reboot-wrapper session.

## v0.6.1 — 2026-07-21 — runtime truth and tmux console correction
- Fix the root cause of GUI console, whitelist, save-flush, TPS, and Stop failures: tmux pane commands require exact target-pane syntax `=session:`, while v0.6.0 incorrectly passed target-session syntax `=session` and received `can't find pane`.
- Add a real tmux integration regression that creates a live session, sends `whitelist add Roaring4`, presses Enter, and verifies the exact received input; also verify capture-pane, has-session, and kill-session target forms.
- Treat tmux query timeout/error as `unknown`, never as offline; transient query failures no longer trigger false GUI offline state, transition completion, watchdog crashes, restarts, or destructive decisions.
- Detect matching Java server processes outside the configured tmux session as `unmanaged` rather than stopped; disable unsafe Start/Restart and explain why Crucible cannot control that console.
- Return actionable tmux stderr in GUI console, whitelist/op, backup save-flush, Stop, and CLI command failures.
- Make graceful Stop permanently non-destructive: timeout never silently force-kills. The GUI must ask explicitly before a no-world-save kill.
- Restore watchdog monitoring and online state after a cancelled/failed Stop or Restart stop phase.
- If an accepted typed `stop` does not actually stop within 120 seconds, re-check live tmux truth and resume monitoring instead of remaining stuck in a fictional stopping state.
- Fix duplicate/eager Info-tab refresh work and a duplicate `unknown` theme key that hid the intended warning color.
- Expand the suite from 54 to 60 tests, including live tmux command delivery, unknown status, unmanaged processes, watchdog uncertainty, and no implicit force-kill.

## v0.6.0 — 2026-07-21 — final release
- Fix installed launcher self-location through `~/.local/bin/crucible` symlinks.
- Fix the `Watchdog.unwatch` PyQt slot signature crash and guard all slot arities.
- Never destroy a still-running QThread after a timed wait; add lifecycle regression scans.
- Lazy-load expensive server tabs and reuse background health status so switching servers does not synchronously scan mods, backups, players, processes, and validation data all at once.
- Move focused TPS sampling off the GUI thread and bound console tmux command timeouts.
- Interpret an exact console `stop` or `/stop` as lifecycle intent, unwatch crash recovery, show `stopping`, and then let log/tmux observation confirm the actual process exit. Similar text such as `say stop` and `stopsound` is not misclassified.
- Report externally deleted server directories as `missing`, while keeping a still-running tmux session controllable even if its files vanished.
- Make add/remove/update/reorder registry commits disk-first and transactional so failed writes cannot create phantom or lost in-memory rows.
- Before either unregistering or deleting a server, probe the exact tmux session live and fail closed on running sessions, timeouts, or unexpected tmux errors; never trust a potentially stale sidebar dot for destruction.
- Detect outside edits/replacements/deletion of `instances.json`, block silent clobbering, and require a safe restart to reconcile; malformed/duplicate registry rows disable writes rather than destroying evidence.
- Expand the regression suite to 52 tests, including the real installed-launcher symlink path.

## v0.6.0 — deep reliability and safety audit
- Prompt to save/discard/cancel unsaved server.properties edits before reload, switch, or removal.
- Block instance switching during active work and safely roll the sidebar selection back.
- Make recursive server deletion transactional, rollback-capable, and unavailable while running.
- Bound crash reports, JAR metadata, registry files, quarantine history, and player JSON reads.

- Detect immediate start-script failure without an unbounded duplicate log.
- Prevent config boolean/key desynchronization and fix Prism export crash.
- Reject modpack path traversal, symlink escapes, and non-HTTPS download URLs.
- Make backups atomic, unique, and ZIP-verified; warn and flush before live backup.
- Reject ambiguous IDs, invalid/duplicate tmux sessions, and unsafe recursive deletes.
- Never fabricate player UUIDs; use the running Minecraft server to resolve profiles.
- Remove generated package metadata/patch artifacts and add regression tests.
- Move server installation and Prism/archive imports off the GUI thread.
- Force exact tmux session targets and send console commands as literal text.
- Fix KDE Konsole/terminal attach argument handling.
- Run periodic tmux health checks off the GUI thread.
- Isolate backup completion from rapid instance switching.
- Defer dialog close/accept until every worker thread has actually exited.
- Replace the duplicated legacy installer with a staged, smoke-tested, rollback-safe updater.
- Add a verified one-line downloader with exact release assets, SHA-256 checks, and safe ZIP validation.
- Package the application icon explicitly and add release-script regression tests.
- Preserve watchdog crash counts across automatic restarts and reset only after stable uptime.
- Require repeated tmux misses before declaring a crash and enforce the configured loop limit.
- Bound log, public-IP, and avatar reads; publish avatar cache files atomically.
- Prevent closing dialogs/app while live workers could be destroyed.
- Route Prism export through the privacy-safe client exporter (never worlds/admin files).
- Bound archive expansion/network downloads and require verified HTTPS.

## v0.5.1 — 2026-06-21

Follow-up to the v0.5.0 release: audited the v0.5.0 fixes (all confirmed good)
and added the quality-of-life tools you actually reach for while running a
modded server.

### Added

- **Console search (Ctrl+F).** A find bar with next/previous, wrap-around, and
  live incremental matching — invaluable for hunting a stack trace or a specific
  mod ID in a noisy modpack log. Esc closes it.
- **"Hide TPS poll output" toggle (on by default).** The focused-only TPS poll
  runs every 30s; its responses (`tick query` / `tps` / `Mean tick time`) are
  now parsed for the readout but kept *out* of the console so they don't bury
  real log lines. Untick to see them.
- **Copy log button.** Copies the whole console buffer to the clipboard — handy
  for sharing a crash when asking for help.
- **Player "Quick effects" menu.** Right-click an online player → *Quick effects*:
  Heal, Feed, Fire resistance, Night vision, Water breathing, and Clear all
  effects — the everyday admin actions, one click away (particles hidden).

### Verified

- Re-audited every v0.5.0 change line by line: the NeoForge/1.21 `tick query`
  TPS fix, Minecraft-style Tab completion, recently-played persistence, teleport
  dialog, and focused-only polling are all correct and intact.

## v0.5.0 — 2026-06-21  (RELEASE)

The server is confirmed working end-to-end (Create+ 1.21.1 NeoForge boots and
plays). This is the first build tagged as a RELEASE.

### Fixed

- **NeoForge 1.21 no longer spams "Unknown or incomplete command".** The
  background TPS poll was always sending `forge tps`, which NeoForge 1.21
  doesn't understand — so the console logged an error every 30 seconds. The TPS
  command is now chosen by *both* Minecraft version and loader:
  - Minecraft **1.21+** uses the built-in vanilla `tick query` (works on every
    loader: vanilla, Fabric, Forge, NeoForge).
  - Older Forge uses `forge tps`; older NeoForge uses `neoforge tps`.
  - Paper-family servers keep `tps`.
  Vanilla/Fabric below 1.21 are still left alone (no command exists).

### Added

- **Minecraft-style Tab autocomplete in the console.** Press Tab to complete the
  command under the cursor and Tab/Shift+Tab to cycle matches, 1:1 with the
  in-game feel. Completes command names, subcommands (e.g. `tick → query`,
  `gamemode → creative`, `forge/neoforge → tps`), dimensions after `in`, and the
  names of online players for player-targeting commands. ↑/↓ history still works.
- **"Recently played" player state.** Players who leave (or who were online when
  the server stops) are kept in a dim *recently played* section instead of just
  disappearing, with "last seen" times. Persisted per-server so it survives app
  restarts. Right-click → *Forget* to remove one.
- **Player teleport dialog.** Right-click a player → *Teleport…* to send them to
  X/Y/Z in a chosen dimension (Overworld / Nether / End / current) or directly
  to another online player.
- **Player info / stats.** Right-click → *Player info / stats…* shows first/last
  seen, sessions, tracked playtime, plus world stats (play time, deaths, mob &
  player kills, jumps, damage, distance) read on demand from the world save.
- **More detail when focused:** the console TPS readout now also shows MSPT when
  the server reports it.

### Changed

- **Stop gathering info nobody's looking at.** TPS is now polled *only* while the
  server is running **and** the Console tab is focused; switching tabs stops it.
  The System tab was already focus-gated. Player stats are read only when you
  open the info dialog — nothing is collected in the background that isn't used.

## v0.4.9 — 2026-06-19

### Fixed

- **"Fix loading errors" now actually finds the culprit mod.** The previous
  release detected the `invalid dist DEDICATED_SERVER` crash but reported "the
  crash log did not name which mod" on real crash *reports*, because the parser
  only understood the console wording (`- Name (id) has failed to load`). It now
  reads every format FML/NeoForge emits:
  - `Failure message: Name (id) has failed to load` (crash-report block)
  - `Mod file: …/<jar>.jar` — the offending jar named directly
  - `Failed to create mod instance. ModID: <id>`
  - `TRANSFORMER/<id>@<version>` stack frames
- **Auto-quarantine is now reliable.** Crucible disables the offending jar found
  by any of the above signals (the `Mod file:` path is matched against the
  installed jars), then — if the crash truly names nothing — falls back to
  scanning `mods/` for client-only jars and disabling those automatically.
- **No more false positives.** Loader/engine jars and `Using Mod File:` JarJar
  dependency warnings are ignored, and a jar is no longer mistaken for the
  loader just because its version string contains "NeoForge" (e.g.
  `statuseffectbars-1.21.1-NeoForge-1.0.2.jar` is correctly treated as a mod).

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
