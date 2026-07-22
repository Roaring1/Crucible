# Crucible

GUI + CLI for Minecraft dedicated servers on Linux (Arch, Nobara, Fedora), with GT: New Horizons support and Prism Launcher modpack import.

Manages server start/stop/restart via tmux, tails the live log, handles mods,
backups, server.properties editing, player lists, and auto-restart on crash.

---

## Install

### Recommended: verified release installer

```bash
bash <(curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
```

The downloader selects the exact source asset for the latest GitHub Release,
requires its matching SHA-256 manifest, validates archive paths and size limits,
and then runs the staged installer. The installer uses an isolated virtual
environment, smoke-tests the new copy before publication, preserves backups,
and never deletes the folder it was launched from.

Manual release install: download `Crucible-vX.Y.Z-source.zip` and its matching
`Crucible-vX.Y.Z-SHA256.txt`, verify the hash, extract, then run `bash install.sh`.

### Developer install

```bash
git clone https://github.com/Roaring1/Crucible.git
cd Crucible
python3 -m pip install --user -e .
```

Requires Python 3.11+, PyQt6 6.5+, and tmux.

---

## Usage

```bash
crucible          # open the GUI (same as: crucible gui)
crucible list     # list registered servers
crucible start Midtech
crucible stop  Midtech
crucible attach Midtech   # open live console in a terminal
crucible send Midtech "forge tps"
crucible status
```

First launch: click **+ Add Server** and point it at a server folder, or use the Prism import buttons.

---

## Prism Launcher compatibility

Crucible can now import Prism/MultiMC instances and Prism-compatible pack archives as dedicated-server folders:

```bash
crucible scan-prism ~/.local/share/PrismLauncher/instances
crucible import-prism /path/to/PrismInstance /path/to/ServerFolder
crucible import-prism pack.mrpack /path/to/ServerFolder
```

Best results come from importing a fully installed Prism instance, because Modrinth/CurseForge exports often contain remote indexes rather than the downloaded jars. Import copies server-safe content (`mods`, `config`, `defaultconfigs`, `kubejs`, `scripts`, `patchouli_books`) and writes `start.sh`, `server.properties`, `eula.txt`, `.crucible/import-summary.json`, and `CRUCIBLE_IMPORT.md` with warnings and next steps.

The GUI **+ Add Server** dialog also includes **Import Prism Instance…** and **Import Modpack Archive…** buttons.

---

## What it does

- Tracks server processes in named tmux sessions -- closing Crucible never kills a running server
- Tails `logs/fml-server-latest.log` (GTNH 1.7.10 FML format) or `logs/latest.log`
- Reads TPS from `/forge tps` output, shows player join/leave in real time
- Mod tab: enable/disable/delete jars, inspects MANIFEST and mcmod.info for names/versions
- Config tab: editable server.properties with type inference (booleans get dropdowns)
- Backups tab: zip-compress world folders, auto-prune old backups
- Watchdog: optional auto-restart on unexpected crash (configurable per instance)
- Copy external IP button (queries ipify, reads server-port from server.properties)

---

## Config files

| Path | Contents |
|---|---|
| `~/.config/crucible/instances.json` | Registered servers |

Crucible modifies server files only for explicit actions such as setup/import, configuration, mods, player commands, diagnostics, and backups. Destructive actions require confirmation.

---

## Requirements

- Python 3.11+
- PyQt6 6.5+
- tmux
- Linux (tested on Arch + KDE Plasma Wayland, Nobara 41-43)

---

## Changelog

**v0.6.4** -- adds a World Backup & Swap system: named/rememberable world save slots with integrity-verified backups, a new World tab, and a fail-closed swap workflow (stopped-server check, automatic pre-swap safety backup, atomic rename-based swap, and automatic rollback on any verification failure)

**v0.6.3** -- fixes a possible "stuck on STARTING..." GUI status via a tmux-pane-tail fallback that shares the exact same startup-detection pattern as the log watcher; adds an in-app server memory (Java heap -Xms/-Xmx) editor to the Setup tab that preserves all other java_args flags and warns before over-allocating RAM

**v0.6.2** -- fixes the GTNH reboot-wrapper false "Stop looks like a restart" report; Stop now sends Ctrl-C to interrupt startserver-java9.sh/.bat's own 12-second auto-reboot countdown, and the watchdog now detects a real java crash even when that wrapper script keeps the tmux session alive

**v0.6.1** -- corrective runtime-truth release; working tmux console/whitelist/stop targeting, uncertainty-safe status, unmanaged-process detection, and non-destructive stop timeouts

**v0.6.0** -- deep reliability/security audit; atomic backups, safe imports, start-failure reporting, identity/session safeguards, and regression tests

**v0.3.7** -- antivibe GUI pass: amber accent, varied button hierarchy, no uniform border-radius, Unicode separators removed from all source files

**v0.3.6** -- 8 bug fixes: inspect thread zombie, backup thread race, file handle leak, restart main-thread block, watchdog cleanup on close, add-dialog double-accept, log_missing swallow, version strings unified

**v0.3.5** -- status_map() fix: sessions not starting with "gtnh-" prefix always showed OFFLINE; Copy IP button; console state sync; watchdog prefix fix
