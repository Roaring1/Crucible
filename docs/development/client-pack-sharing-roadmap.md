# One-click Share with players: implementation roadmap

**Status:** P0, P1's clean Prism-import validation, and the P2 Gofile adapter/review-first sharing UI are merged to `main`. The release candidate's CI passed on Python 3.11 and 3.13; local regression tests cover provider/UI behavior. On 2026-10-27, an owner-approved small synthetic Gofile upload passed local SHA-256 plus remote size/MD5 checks; the public share page returned HTTP 200, and that test's remote folder and local ZIP were removed. A second 348-byte synthetic-only upload was attempted on 2026-10-28: upload integrity checks passed, but the separate guest's contents listing returned `error-notPremium` (HTTP 401), a retry hit `error-rateLimit`, and cleanup of this second upload has not been confirmed. No real pack/world/config data was uploaded in those synthetic tests. The owner has since reported successful real-world use of a separate `username admin server` player pack: created through Crucible, imported into Prism, uploaded to Gofile, and downloaded by recipients. Local share history does not link the provided Gofile code to an exact ZIP fingerprint, so this is an owner-reported pass for that specific link, not proof that every retained revision is identical. Cleanup of the second synthetic upload remains unconfirmed; its private recovery state is retained on the test machine. Do not repeat live API retries until the rate limit clears. The large-file cancellation/retry soak test also remains open.
**Tracking issue:** [Crucible #5](https://github.com/Roaring1/Crucible/issues/5).

## Product outcome

Crucible should do the complicated work once. The owner selects **Share with
players** and receives a ready-to-send link. Friends download one ZIP, drag it
onto Prism Launcher, confirm the import, sign in to their own Minecraft account
if necessary, and ask the owner for the server address. Recipients should not
need a command line, another modpack manager, loader/version guesswork, or API
credentials.

Prism documents importing compatible `.zip` and `.mrpack` files. The default
artifact for this workflow should be a Prism/MultiMC instance ZIP because the
experience is intended to feel like importing a tested client instance—not
choosing among export formats. The existing `.mrpack` and CurseForge outputs
remain under an advanced export path.

## What exists today

- `crucible/exporters/client_export.py` builds Prism/MultiMC ZIP, Modrinth
  `.mrpack`, and CurseForge-style ZIP archives locally.
- `crucible/ui/client_export_dialog.py` remains the advanced local export
  dialog (Prism ZIP, MRPACK and CurseForge-style ZIP), including an archive
  SHA-256/manifest path and the **Remember this Prism source** option.
- `crucible/ui/share_players_dialog.py` is the default **Share with players**
  workflow. With a saved Prism source it starts building the default Prism ZIP
  when opened; otherwise the owner can choose a client profile and build it.
  It displays the full included-file list and per-file hashes, privacy warnings,
  scan-coverage status and artifact-specific distribution review. Exact SHA-256
  records may mark a JAR permitted or conditional with evidence; no record means
  unverified. A known-prohibited JAR is never bundled. With a curated alternative
  URL it is omitted and the ZIP contains manual-install instructions; without an
  alternative, export stops. Upload stays disabled until all review, distribution
  responsibility and public-link acknowledgements are checked.
- `crucible/sharing/provider.py` defines the hosting interface and
  `crucible/sharing/gofile.py` implements guest uploads by default. If Gofile
  requires account authentication, the owner can enter an optional API token in
  a masked field; it exists only in the dialog/worker memory and is never saved.
  The adapter streams multipart data from disk, checks local SHA-256 before
  sending and again over the exact bytes streamed, and compares remote size/MD5
  when provided. It uses finite socket timeouts and cancellation; manual retry
  reuses the same local ZIP. On POSIX the sharing cache folder is mode 0700 and
  completed ZIP files are mode 0600.
- A saved matching Prism source contributes explicitly client-only mods and
  fills in client-profile-only `config/` and `kubejs/` files. The server's
  copy wins when a relative path conflicts, with a warning; identical content
  is deduplicated. KubeJS is restricted to `client_scripts/`,
  `startup_scripts/`, `assets/` and `config/`; server scripts, data dumps
  and unrecognized folders are excluded with warnings.
- Client resource packs are excluded by default. The owner can explicitly opt
  in to including the selected profile's `resourcepacks/` folder, after which
  each included pack is shown in the license review. Shaderpacks are never
  included by this workflow. Version/source conflicts and unknown side metadata
  are reported instead of guessed.
- The exporter embeds a per-file size/SHA-256 manifest and performs bounded
  heuristic scans on eligible text files beneath included `config/` and
  KubeJS roots, including common JSONC, SNBT, CSS, locale and text-backup
  formats. The manifest and owner review state that this scan does **not**
  inspect mod JARs, resource packs or other payloads for secrets. Unsupported
  binary/config types and oversized files are flagged for manual review, and
  secret values are never echoed. The scan is not proof that a pack contains no
  secrets.
- After an approved upload succeeds, the returned Gofile share link is copied
  automatically and remains available for manual copy/open actions. Crucible
  does not retain Gofile guest-management tokens or locally store the optional
  owner API token. Upload progress reports average payload throughput and an
  approximate ETA; bounded 256 KiB network writes make cancellation checks more
  frequent on slow links while finite socket timeouts still prevent indefinite
  hangs.
- A real Prism Launcher 11.1.1 import has been verified in an isolated fresh
  data directory using the current scope-annotated ZIP. Its 8 manifest payload
  entries were hash-checked against the archive and imported files; Prism
  preserved Minecraft 1.21.1 / NeoForge 21.1.233, client-only config/KubeJS
  survived, server config won collisions, and server KubeJS/data/resourcepacks/
  defaultconfigs/worlds/screenshots stayed out. This verifies archive import,
  not whether a real modded game launches successfully.
- The exporter previously included `defaultconfigs/` while its checkbox named
  only `config/` and `kubejs/`. This mismatch is fixed by the accompanying
  change: `defaultconfigs/` is excluded by default and a warning is shown if it
  exists. This avoids silently publishing server defaults as ordinary player
  configuration.

## Implementation order

### P0 — Make the client pack trustworthy

- [x] Persist a known-good Prism/MultiMC source per server after a successful
  export; validate `mmc-pack.json` and exact Minecraft/loader versions.
- [x] Compare source and server mods by content hashes and known metadata,
  deduplicate identical JARs, and report source/version conflicts and unknown
  side information rather than guessing.
- [x] Merge missing client-profile `config/` and client-safe KubeJS files;
  server files remain authoritative for conflicting paths and conflicts are
  reported. Only KubeJS `client_scripts/`, `startup_scripts/`, `assets/`
  and `config/` are copied; `server_scripts/`, `data/`, `exported/` and
  other unrecognized folders are excluded with warnings.
- [x] Make config selection truthful: include `config/` and the client-safe
  KubeJS subset from validated roots, exclude `defaultconfigs/`, and warn
  when it was excluded. Exclude client resourcepacks by default, with a separate
  opt-in that lists pack names and license status; shaderpacks stay excluded.
- [x] Scan included text config/KubeJS content for common tokens, webhooks,
  credentials, private keys and private addresses, including common `.jsonc`,
  `.snbt`, `.bak`, `.css`, and `.local` text formats found in real profiles.
  Values are never echoed in warnings. The scan has explicit size/finding
  bounds and records incomplete coverage (including binary config assets) in
  the manifest; it is heuristic, not a guarantee of no secrets.
- [x] Export only mods and the selected `config/` and `kubejs/` paths. Worlds,
  screenshots, logs, crash reports, personal launcher/account data, unrelated
  graphics settings, and shader/resource packs are not copied by this exporter.
- [x] Produce an embedded manifest containing each included relative path, size
  and SHA-256. Reject unsafe archive paths and special files, reject linked
  source roots, and exclude nested symlinks without following them.
- [x] Never infer redistribution permission from a mod name or JAR metadata.
  A versioned exact-artifact SHA-256 catalog records explicit `permitted`,
  `prohibited`, or `conditional` outcomes with HTTPS project/evidence links and
  a verification date. Unknown hashes remain unverified and require owner review.
- [x] Block a curated-prohibited JAR from the ZIP. Where its record supplies a
  reviewed alternative download page, omit the JAR and include a manual-install
  guide plus the catalog evidence in the ZIP manifest/review; otherwise fail
  the export rather than creating a known-incomplete pack without a next step.
- [ ] Populate and maintain records for the actual mods owners commonly bundle.
  The shipped catalog currently has **no curated permission entries**, so this
  mechanism does not claim that any real mod has been cleared. Build further
  sanctioned download/import flows where a manual alternative is insufficient.

**Exit criterion:** a local-only player ZIP has an explained file list, matching
versions, known unresolved compatibility warnings, privacy results and a
reproducible manifest.

### P1 — Make the default Prism ZIP import for real

- [x] Make Prism instance ZIP the default artifact for **Share with players**.
  Keep `.mrpack` and CurseForge export options in **Advanced export**.
- [x] Check the instance folder, `instance.cfg`, `mmc-pack.json`,
  `.minecraft/mods/` and selected config paths, with exact Minecraft/loader
  metadata.
- [x] Add structural tests for path safety, metadata, component versions,
  manifest hashes, exclusions, config privacy warnings and archive integrity.
- [x] Import the generated ZIP into a clean Prism Launcher 11.1.1 data root and
  verify the resulting instance metadata and unchanged payload hashes. This
  proves import compatibility for the tested fixture, not a real pack's gameplay
  launch or every third-party mod combination.
- [x] Do not include a server address or player account material by default.
- [x] Do not impose Java requirements or memory settings in the export; Prism
  remains responsible for choosing a compatible runtime when the instance is
  launched.

**Exit criterion:** a clean Prism instance imports the produced ZIP and has the
expected mods/configuration without manual version selection. The structural
and clean-import checks pass on the current fixture; validate against a real
working modded client before promising every pack can launch successfully.

### P2 — Add Gofile upload and the one-click share experience

- [x] Define a small `UploadProvider` protocol, keeping archive generation
  independent from Gofile; implement the official `upload.gofile.io/uploadfile`
  endpoint in guest mode. The API is beta, so response validation is strict.
- [x] Keep provider credentials out of Crucible's settings and generated ZIP.
  Guest mode is the default; an optional owner token is memory-only, and the
  returned guest-management token is discarded. Recipients do not need API
  keys or account credentials.
- [x] Stream from disk on a worker thread, report byte-level progress, average
  throughput and approximate ETA, support cancellation, use finite socket
  timeouts, and offer manual retry from the same ZIP. Network writes are bounded
  to 256 KiB so cancellation is checked more often on slow links. No automatic
  retry is performed because a lost response can leave an unconfirmed public
  upload and duplicates would be misleading.
- [x] Verify the local SHA-256 against the build result before sending. Compare
  Gofile's reported size and MD5 when present; the remote MD5 is not described
  as a SHA-256 check. If metadata is absent, the UI says so.
- [x] Show **Copy link**, **Copy player instructions**, **Open local ZIP** and
  **Open link** after upload. Copy the new link automatically on success; say
  that it has not been independently re-fetched for validity.
- [x] Show exact included paths, sizes and hashes, privacy findings/scan gaps,
  compatibility warnings, exact-artifact catalog decisions/evidence/conditions,
  remaining unverified JARs, any required manual mod downloads and each included
  resource pack before upload. Require three explicit acknowledgements; treat
  the link as public to anyone who obtains it.
- [x] Warn that free Gofile storage is ephemeral and do not hard-code a monthly
  traffic/file-size quota or promise permanent retention.
- [x] Run a small owner-approved synthetic live Gofile upload; verify local
  SHA-256 and Gofile-reported size/MD5; confirm the public share page responds;
  delete the synthetic remote folder and local test ZIP. This does not prove an
  anonymous recipient can download the actual ZIP bytes.
- [x] Record owner-reported real-world pack validation: the `username admin server` pack was created through Crucible, imported into Prism, uploaded to Gofile, and downloaded successfully by recipients. The owner provided the tested public link, but local share history does not associate it with an exact retained ZIP SHA-256, so this result is not tied to v1–v6 or a reproducible manifest fingerprint.
- [ ] Run a large-file cancellation/retry soak test. The separate synthetic upload attempted on 2026-10-28 still has unconfirmed remote cleanup; do not create another test upload until that incident is reconciled.

**Exit criterion:** mocked-provider tests validate streaming request
construction, hash/size/MD5 checks, cancellation, safe-link parsing and error
recovery. The small live synthetic upload also passed SHA-256, remote size/MD5,
share-page HTTP response and cleanup checks. The owner has since reported that recipients successfully downloaded a real player pack after it was created through Crucible, imported into Prism, and uploaded to Gofile. This is an owner-reported end-to-end pass for that link; Crucible's local history does not identify the exact ZIP fingerprint behind it. The large upload/cancel/retry soak test remains open. A separate 2026-10-28 synthetic upload's remote cleanup remains unconfirmed, so no further synthetic live uploads should be attempted until it is reconciled.

### P3 — Caching, expiry and recovery

- [x] Fingerprint Prism-pack inputs using exact Minecraft/loader versions,
  source-profile and server identity, content hashes and relative names for
  candidate mod/config/KubeJS files, inclusion options, and resource/shader-pack
  review-warning state. The manifest stores only the opaque SHA-256 fingerprint,
  not the local absolute paths. Prism exports compare the inputs again before
  finalizing and fail if the source changed during the build.
- [x] Reuse a cached local ZIP only when its complete input fingerprint matches.
  A private atomic sidecar records cache metadata; before reuse Crucible checks
  owner-only file permissions, the whole-archive SHA-256, manifest identity,
  exact archive-member/manifest agreement, every payload size/SHA-256 and ZIP CRC.
  Damaged/malformed cache entries are ignored and rebuilt; review acknowledgements
  are still required for every sharing session. Cached artifacts remain local.
- [x] Keep a bounded local event history (last 50 events) for pack preparation
  and Gofile upload outcomes, including source/archive fingerprints, local path,
  size, provider, returned link, UTC timestamp, outcome category and remote
  size/MD5 verification flags. History files/directories are owner-private and
  atomic; only allowlisted fields are stored and raw exceptions/tokens are never
  persisted. The UI clearly labels saved links as not re-verified.
- [x] Never silently reuse a saved remote link: history marks saved links as
  "not re-verified", manual copy explicitly says validity was not checked, and
  upload completion announces that Gofile returned a new link. Provider-aware
  expiry/link-status confirmation remains open in the next item.
- [x] Add explicit, user-confirmed cleanup for a selected local ZIP from share
  history. Deletion is restricted to a regular ZIP in Crucible's player-packs
  folder and requires its SHA-256 to match the history record; its cache sidecar
  is removed with it. No ZIP is deleted automatically, clearing history never
  deletes files, and neither action deletes or revokes remote Gofile content.
- [ ] Add provider-aware expiry/link-status handling and visible management of
  expired remote links. Saved links stay labelled as not re-verified until a
  provider can confirm current validity; local cleanup is not remote deletion.
- [x] Test bounded retention, no-secret persistence, safe-link validation, history
  clear without ZIP deletion, and local-link/path display. Cache tests also cover
  no-change reuse, changed-config rebuild, stale-fingerprint rejection and a
  corrupted-cache rebuild.
- [ ] Test link expiry/recovery, interrupted upload, checksum mismatch, and retry
  across a real owner-approved Gofile upload and large-file soak run.

## Acceptance criteria for the complete workflow

- One primary action creates a tested Prism-compatible ZIP and a ready-to-send
  link without asking the owner to pick a format every time.
- Build/upload work runs off the UI thread; status and byte progress are
  truthful and cancellation leaves the application and local ZIP usable.
- The archive uses exact Minecraft/loader versions and includes shared mods once
  plus explicitly client-only additions from the saved source.
- Unknown mod side, version conflicts, privacy findings and redistribution
  exceptions are surfaced rather than silently guessed or omitted.
- No world saves, screenshots, logs, crash reports, account/auth data or
  unreviewed server addresses are included by default.
- The included file manifest and local ZIP are retained; failed upload retries
  reuse that ZIP rather than redoing packaging needlessly.
- The owner sees the new/current link state and has copyable player instructions.
- Tests validate structural correctness and a real clean Prism import; a unit
  test of ZIP creation alone does not count as full validation.

## Provider and format decisions

- **Default archive:** Prism/MultiMC instance ZIP. Prism's official help states
  that its import flow accepts compatible ZIP and MRPACK files; the integration
  test must verify the exact ZIP format Crucible produces.
- **Upload host:** Gofile first, behind a provider adapter. The adapter uses
  the official `https://upload.gofile.io/uploadfile` upload endpoint and
  validates the `gofile.io/d/` download-page URL and returned file metadata.
  The API documentation labels the service beta. Free storage is advertised as
  ephemeral; no traffic/file-size quota is hard-coded. A real account's present
  plan-specific limits and live retention remain to be validated.
- **Distribution rights:** JAR files are not automatically redistributable just
  because Crucible can read them. Preserve attribution/manifest information and
  respect author permissions and restricted-download rules. A non-redistributable
  mod must be explained, with a supported download/import exception when
  available.
- **Live verification:** a small synthetic upload was tested live on 2026-10-27.
  Gofile returned matching size and MD5, the public page responded with HTTP 200,
  and cleanup succeeded. Separately, the owner reports recipients downloaded an
  actual `username admin server` pack created through Crucible, imported into Prism,
  and uploaded to Gofile. Local share history does not bind that public link to a
  particular archive SHA-256, so we record this as an owner-reported end-to-end pass
  for that link. Link expiry/retention and large-file cancellation/retry remain open.
  A second synthetic upload on 2026-10-28 has unconfirmed remote cleanup; no further
  synthetic upload attempts should be made until that incident is reconciled.

## References

- [Prism ZIP import](https://prismlauncher.org/wiki/help-pages/Zip-import/)
- [Prism command-line interface](https://prismlauncher.org/wiki/getting-started/command-line-interface/)
- [MultiMC instance export structure](https://github.com/MultiMC/Launcher/wiki/Export-Instance)
- [Modrinth MRPACK format](https://support.modrinth.com/en/articles/8802351-modrinth-modpack-format-mrpack)
- [Gofile.io plans](https://gofile.io/premium) and [API](https://gofile.io/api); reconfirm current behavior before implementation.
