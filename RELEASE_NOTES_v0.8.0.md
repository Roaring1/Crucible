# Crucible v0.8.0 — server-manager and player-sharing refinement

Crucible is a Linux desktop manager for local Minecraft dedicated servers. This release consolidates the v0.8 refinement work: safer server/property diagnostics, more reliable lifecycle handling, mod-management safeguards, and a review-first Prism player-pack workflow.

## Highlights

### Server lifecycle and diagnostics

- Check the configured IPv4 TCP port before starting a server. A known collision is reported before Crucible creates the tmux session; if procfs cannot be read, the result is unknown and startup fails closed rather than assuming the port is free.
- Use Java's last-value-wins behavior for duplicate `server-port` entries. Address-copy controls reject blank, malformed, or out-of-range ports instead of generating a misleading address.
- Infer legacy loader metadata only from specific on-disk launcher artifacts when the registry has no explicit loader. Explicit registry metadata remains authoritative. Normalize human-labelled Minecraft version metadata without mistaking a modpack version such as `2.8.4` for a Minecraft version.
- Validate `server.properties` against the registered/detected Minecraft version. Historical keys such as `spawn-animals` and `spawn-npcs` are accepted for versions before 1.21.2 and reported as removed on newer/unknown versions.
- Accept syntactically valid namespaced modded world presets such as `wover:normal`, including Java-properties escaped colons, rather than recommending vanilla terrain for custom world generators.
- Avoid unsafe typo repairs such as renaming `server-id` to `server-ip` or `server-name` to `level-name`; those settings do not mean the same thing. Preserve protection against actual registry edits and concurrent writes while reconciling expected CLI-only lifecycle changes to `last_started`.

### Mod management and desktop workflows

- Browse compatible mod releases and upgrade or downgrade through staged, hash-verified downloads. Keep previous JARs, identity snapshots, and recovery receipts; ordinary publication errors roll back, and unsafe conflicts are rejected.
- Identify matching online mod files by exact identity/hash rather than by similar filenames. Explicit Fabric, Quilt and Forge environment metadata is shown as evidence, not a guarantee that a whole modpack is compatible.
- Add mod search/version/dependency support for the official CurseForge API, with session-only or user-owned private credentials. Credential-bearing requests are host-scoped, redirects cannot leak credentials to other hosts, and author-restricted downloads are not guessed or bypassed.
- Improve console suggestions, player controls, resizable layouts, CPU/memory descriptions, source-log diagnostics, server rename/import workflows, and lazy loading of expensive tabs.

### Review-first player-pack sharing

- Build a Prism/MultiMC ZIP from a selected installed client profile with exact Minecraft and loader-version checks. The source profile supplies client content; server files remain authoritative for conflicting paths and mod identities.
- Reconcile mods by content hash and explicit metadata. Exclude declared server-only JARs, warn about unknown-side JARs, and refuse to create a ZIP when multiple enabled JARs declare the same mod ID. A saved ZIP is not treated as an installed Prism profile.
- Exclude worlds, screenshots, logs, account data, shaderpacks, server scripts, and unrelated files by default. Resource packs are opt-in. Include a per-file size/SHA-256 manifest and local archive checksum.
- Require an owner review of the exact file set, compatibility findings, privacy-scan limits, and distribution rights before public upload. The text scanner is bounded and heuristic; it scans eligible text beneath included `config/` and KubeJS paths only. It does not inspect mod JARs or resource packs for secrets, and binary, unknown-format, or oversized config files require manual review.
- Maintain an exact-JAR-SHA-256 distribution catalog with explicit `permitted`, `conditional`, or `prohibited` decisions and HTTPS evidence. Unknown artifacts remain unverified, and prohibited artifacts are not bundled.
- Stream approved uploads to Gofile without loading the full archive into memory. Verify the local SHA-256 and streamed bytes; verify provider size/MD5 fields when supplied. Show transfer progress, average speed and approximate ETA, support cancellation/manual retry, and retain a private local ZIP and bounded local history.

## Validation

- Full local test suite on Python 3.14.4: **583 passed, 61 subtests passed**.
- GitHub Actions passed the full suite on Python 3.11 and Python 3.13 for the v0.8.0 release candidate commit.
- A generated synthetic Prism ZIP was imported into an isolated Prism Launcher 11.1.1 profile, and manifest/file hashes were checked.
- A small owner-approved synthetic Gofile upload previously passed local checksum and provider size/MD5 checks and the public share-page check. This did not establish end-to-end anonymous download of the ZIP bytes.
- Read-only validation of the live Minecraft 1.21.1 server and the stopped Create+ 2 instance reported no `server.properties` problems. No live world or mod set was changed during this release validation.

## Known limits and owner actions

- **No real-mod redistribution permissions are pre-approved in the shipped catalog.** The catalog is deliberately empty until individual artifact hashes have reviewed evidence. Do not treat an unknown hash, a mod name, or an exportable ZIP as permission to redistribute a mod.
- The actual Create+ pack still has duplicate mod IDs that must be resolved before a complete player ZIP can be generated. The current pack's unverified JAR rights and binary/cache config files also require owner review. No real modpack was uploaded as part of this validation.
- Anonymous download of actual ZIP bytes from Gofile and a large-file cancellation/retry soak test remain open. Gofile guest/free link retention may be ephemeral; do not promise permanent storage or automatic revocation.
- CurseForge approval for Crucible's open-source desktop use, applicable credential/retention permissions, and live access using an approved key remain unverified. No shared API key is bundled.
- The test suite and synthetic Prism import do not prove that every arbitrary mod combination launches or plays correctly. Mod changes do not migrate worlds; back up a world before changing content mods.
- Ordinary file-operation errors roll back. Sudden power loss or process termination during a mod publication can require manual recovery from the retained original files and the operation's receipt.

## Install

The verified one-line installer downloads the latest GitHub Release's exact source ZIP and SHA-256 manifest, checks the digest and archive paths/links/size limits, and runs the staged atomic installer. Requirements: Linux, Python 3.11+, PyQt6 6.5+, tmux, and a suitable Java runtime.

```bash
bash <(curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
```

See `README.md`, `CURSEFORGE.md`, and `docs/development/client-pack-sharing-roadmap.md` for operational detail and remaining work.
