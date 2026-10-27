# Crucible v0.8.0 — Server-manager refinement

## What changed

- Live console suggestions above the command box. Tab/Shift+Tab cycle, arrows choose suggestions, Escape dismisses, and history remains available when suggestions are hidden. Gamerules and boolean values, known players, loader commands and caret-aware replacement are supported.
- Compatible mod release selection, including downgrades. Untracked JARs can be identified by exact Modrinth SHA-1; similar names are never silently treated as the same project.
- Official CurseForge search/version/dependency adapter. The app accepts a session-only API key or CURSEFORGE_API_KEY. A user-owned mode-600 ~/.config/crucible/curseforge-api-key file is also loaded automatically by both browsing and pack imports. Author-restricted download URLs are not guessed or bypassed. An already-downloaded matching file can establish its provider identity.
- Staged, SHA-1-verified mod/dependency installs and version changes. Previous files and identity records are retained under .crucible/mod-history with recovery receipts. Normal publication errors roll back; conflicting pinned dependencies, unexpected original-file changes and stopped-status changes are rejected.
- Explicit execution-side badges from Fabric, Quilt and Forge metadata. Dependency-side declarations are not mistaken for whole-mod compatibility. Unknown is explicitly unverified.
- Rename selected/F2 updates the existing row and header without moving files, changing sessions or reloading dirty tabs. Imports respect the chosen name. Server-folder/archive drops route across child views without interfering with JAR drops or sidebar reorder.
- Resizable Online now player list. The header works at the 900×600 minimum. Import fields have real minimum heights and a scroll fallback.
- CPU is shown as a 0–100% share of all logical CPUs, with a one-core tooltip. Memory is resident RAM, distinct from the Java heap limit. Resource information appears first in Info.
- Loading diagnostics open their actual source log and recognize Java-version errors, occupied ports, out-of-memory, full disks and corrupt launch JARs. No safe repair means no silent file changes.
- Client exports use explicit side metadata, exclude declared server-only mods and can supplement declared client-only extras from a matching installed Prism instance. Exact Minecraft/loader checks, compatibility warnings and redistribution reminders replace a promise of automatic playability.
- Importing the same local JAR twice no longer creates a suffixed duplicate that Minecraft would load twice.

## Validation

- Full offline regression suite: **383 passed, 31 subtests passed**.
- Added 51 behavior cases; the real Qt subprocess also covers keyboard completion, cursor preservation, player resizing, sorted mod results, chained network-worker retirement, provider choice, rename persistence/header refresh, external drops and import-field height.
- Live public Modrinth test: Fabric API compatible releases, verified download, exact-file lookup, downgrade from 0.116.17+1.21.1 to 0.102.0+1.21.1 and retained-original receipt. All in a temporary directory.
- Repeated the real-Qt interaction/teardown sequence 15 times after fixing an intermittent Python 3.14/SIP finalization crash; all passed.
- Rendered and inspected the changed desktop workflows with an isolated synthetic server and registry, including 1200×760 and 900×600 layouts.
- Existing uncommitted on-disk work was preserved, included and regression-tested. No user worlds or installed server mod sets were changed during testing.

## Limits and recovery

- CurseForge authentication/downloads have mocked coverage but were not live-tested with an API key.
- Suggestions use a local vocabulary, not the live server's Brigadier tree. Modded arguments and Minecraft-version-specific commands are not exhaustively discoverable.
- Arbitrary third-party mod combinations, in-game performance and exported client gameplay were not validated. Client-only metadata can be absent, especially in old Forge mods; unknown remains unknown.
- JAR changes do not migrate worlds. Make a world backup before changing content mods.
- Ordinary I/O failures roll back. A power loss/process kill during publication can require manual recovery from .crucible/mod-history/<operation>/receipt.json and its retained original files. A stale mod-change.lock intentionally blocks another change until custody is reviewed.
- This is a tested refinement, not a guarantee that Minecraft, upstream providers or every modpack will never require maintenance.
