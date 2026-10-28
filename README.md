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

Crucible provides **Prism-style local server management and client-pack exchange**—not a cloud control plane or router configurator.

## Install

```bash
bash <(curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
```

The release downloader requires exact source/checksum assets, verifies SHA-256, CRC, archive paths, links, and limits, then runs an atomic staged installer. Requirements: Linux, Python 3.11+, PyQt6 6.5+, tmux, and suitable Java.

## First run

1. Open Crucible or run `crucible gui`.
2. Select **+ Add server**.
3. Choose **Create a server**, **Install a modpack**, or **Import existing files**.
4. Review **Setup**, then select **Start**.

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

## Developer diagnostics

- Inspect the latest GitHub Actions test run, including redacted failure excerpts: `python scripts/ci_report.py`.
- Inspect a notification's exact run: `python scripts/ci_report.py --run-id RUN_ID`.
- Capture the main UI and setup screens offscreen: `python scripts/capture_ui_previews.py`.
- Prepare a folder of screenshots for review without copying base64 into chat: `python scripts/prepare_ui_evidence.py ~/.cache/crucible/ui-previews`.

See [CI and UI evidence](docs/development/ci-and-ui-evidence.md) for details. Screenshot folders synchronized locally by Dropbox can be passed directly to the tool.

## Everyday controls (v0.8)

- **Rename:** select a server and press F2, or right-click its row and choose **Rename server…**. Display names do not move files or change tmux sessions.
- **Import:** drop a server folder or ZIP/mrpack anywhere in the main window. Your chosen folder name becomes the imported server name.
- **Console:** type to see suggestions above the input; use arrows to select, Tab/Shift+Tab to cycle, Escape to dismiss. Arrow keys use command history when suggestions are hidden. Suggestions are a local vocabulary, not a server-provided command tree.
- **Mods:** select a mod and choose Change version to upgrade or downgrade. Identify online matches exact Modrinth file hashes; custom builds with no match remain unlinked. Set the installed Minecraft/loader versions with Server version.
- **CurseForge:** choose CurseForge in Add a mod. Supply a session-only API key in the app or CURSEFORGE_API_KEY before launch. A private ~/.config/crucible/curseforge-api-key file (mode 600) is loaded automatically for both browsing and pack imports. Restricted files must be downloaded from the author’s page; no CDN URL is guessed. API/CDN authentication is host-scoped and redirects cannot leak credentials. Missing/rejected keys and restricted downloads fail explicitly; no shared application key is bundled. Crucible-specific approval and distribution/retention permissions remain open; see [CURSEFORGE.md](CURSEFORGE.md).
- **Compatibility:** Runs on badges reflect declared loader metadata. Unknown means unverified. Never assume a dependency-side declaration describes the whole mod.
- **Players:** drag the horizontal divider to resize Online now; right-click/double-click players for actions.
- **Resources:** CPU is the server’s fraction of all logical CPUs, from 0–100%. Hover to see the one-core value. Memory is resident RAM, not configured Java heap.
- **Clients:** Make client exports as Prism, mrpack or CurseForge-style archives. An optional matching installed Prism instance contributes declared client-only extras. Test the export in the target launcher before sharing and respect mod licenses. The planned one-click **Share with players** workflow is tracked in [the client-pack sharing roadmap](docs/development/client-pack-sharing-roadmap.md) and [issue #5](https://github.com/Roaring1/Crucible/issues/5).

### Mod-change safety

Stop the server before changing mods. Downloads and required dependencies are staged and SHA-1 verified before publication. Previous JARs and an identity-record snapshot are retained in `<server>/.crucible/mod-history/<operation>/`, alongside a receipt. Normal publication failures roll back; sudden power loss may require recovery from that receipt. Back up worlds separately before changing content mods: replacing a JAR does not migrate a world.

No software can guarantee every third-party mod combination or future upstream release. See RELEASE_NOTES_v0.8.0.md for the tested scope and limits.
