# Crucible

A focused Linux desktop app for creating, importing, running, and protecting Minecraft dedicated servers. Servers run in named tmux sessions, so closing Crucible does **not** stop Minecraft.

## What you see

| Area | Purpose |
|---|---|
| Dashboard | Clear state, Start/Stop/Restart, readiness checks, and CPU/memory bars |
| Console | Responsive live log, commands, search, TPS/MSPT, and player activity |
| Setup/import | Existing folders, Prism/MultiMC, `.mrpack`/ZIP, or guided installation |
| Mods/players | Mod inspection and controls; whitelist, ban, op, and player lists |
| World safety | Verified backups, named slots, safe swap/rollback, reset, and wipe confirmation |

Crucible intentionally remains a **local server manager**—not a cloud control plane, router configurator, or Prism Launcher replacement.

## Install

```bash
bash <(curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
```

The release downloader requires exact source/checksum assets, verifies SHA-256, CRC, archive paths, links, and limits, then runs an atomic staged installer. Requirements: Linux, Python 3.11+, PyQt6 6.5+, tmux, and suitable Java.

## First run

1. Open Crucible or run `crucible gui`.
2. Choose **+ Add Server**.
3. Select a server folder, import a pack, or create a server.
4. Review **Setup**, then press **Start**.

The GUI is primary. Recovery/script commands remain available:

```bash
crucible list
crucible status
crucible start "My Server"
crucible stop "My Server"
crucible attach "My Server"
```

## Safety and performance

- Uncertain status never authorizes destructive work.
- Graceful Stop never silently becomes Force Kill.
- World swaps validate first, create a mandatory safety backup, rename the old world aside, verify the restore, and roll back on failure.
- Restore rejects traversal, links, special files, duplicate destinations, wrong roots, truncation, and insufficient free space.
- Large logs are bounded and batch-rendered; server switching reuses persistent workers; expensive tabs are lazy-loaded.
- State uses words/icons as well as color; resource cards include numbers and accessible progress bars.

Data locations:

- Registry: `~/.config/crucible/instances.json`
- Backups: `~/.local/share/crucible-backups/`

See `CHANGELOG.md` and GitHub Release notes for full history.
