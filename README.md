# Crucible

GUI + CLI for GT: New Horizons dedicated servers on Linux (Arch, Nobara, Fedora).

Manages server start/stop/restart via tmux, tails the live log, handles mods,
backups, server.properties editing, player lists, and auto-restart on crash.

---

## Install

```bash
pip install --user git+https://github.com/Roaring1/Crucible.git
```

Or clone and install in editable mode:

```bash
git clone https://github.com/Roaring1/Crucible.git
cd Crucible
pip install --user -e .
```

Requires Python 3.11+ and tmux. PyQt6 is pulled in automatically.

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

First launch: click **+ Add Server** and point it at your GTNH server folder.

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

Server files are never modified by Crucible except when you use the Mods or Config tabs.

---

## Requirements

- Python 3.11+
- PyQt6 6.5+
- tmux
- Linux (tested on Arch + KDE Plasma Wayland, Nobara 41-43)

---

## Changelog

**v0.3.7** -- antivibe GUI pass: amber accent, varied button hierarchy, no uniform border-radius, Unicode separators removed from all source files

**v0.3.6** -- 8 bug fixes: inspect thread zombie, backup thread race, file handle leak, restart main-thread block, watchdog cleanup on close, add-dialog double-accept, log_missing swallow, version strings unified

**v0.3.5** -- status_map() fix: sessions not starting with "gtnh-" prefix always showed OFFLINE; Copy IP button; console state sync; watchdog prefix fix
