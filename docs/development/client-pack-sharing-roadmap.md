# One-click Share with players: implementation roadmap

**Status:** Planned, with one immediate exporter-safety fix included in this change.
**Tracking issue:** [Crucible #5](https://github.com/Roaring1/Crucible/issues/5).

## Product outcome

Crucible should do the complicated work once. The owner selects **Share with
players** and receives a ready-to-send link. Friends download one ZIP, drag it
onto Prism Launcher, confirm the import, sign in to their own Minecraft account
if necessary, and ask the owner for the server address. Recipients should not
need a command line, another modpack manager, loader/version guesswork, or API
credentials.

Prism documents importing compatible \`.zip\` and \`.mrpack\` files. The default
artifact for this workflow should be a Prism/MultiMC instance ZIP because the
experience is intended to feel like importing a tested client instance—not
choosing among export formats. The existing \`.mrpack\` and CurseForge outputs
remain under an advanced export path.

## What exists today

- \`crucible/exporters/client_export.py\` builds Prism/MultiMC ZIP, Modrinth
  \`.mrpack\`, and CurseForge-style ZIP archives locally.
- \`crucible/ui/client_export_dialog.py\` exposes a format selector and a
  destination-file picker. It does not upload or create a share link.
- An optional matching Prism source is accepted for adding mods that metadata
  explicitly identifies as client-only. The source is not yet a saved,
  per-server setting.
- Minecraft and loader compatibility are checked, but ZIP creation is not
  proof that a clean Prism install can import and launch the pack.
- The exporter previously included \`defaultconfigs/\` while its checkbox named
  only \`config/\` and \`kubejs/\`. This mismatch is fixed by the accompanying
  change: \`defaultconfigs/\` is excluded by default and a warning is shown if it
  exists. This avoids silently publishing server defaults as ordinary player
  configuration.

## Implementation order

### P0 — Make the client pack trustworthy

- [ ] Persist a known-good Prism/MultiMC source per server; validate that it is
  a complete instance with \`mmc-pack.json\` and exact Minecraft/loader versions.
- [ ] Compare source and server mods by hashes and known metadata, not filenames
  alone. Deduplicate identical JARs. Flag version conflicts and mods whose side
  is unknown; never infer a mod's side from its name.
- [x] Make the config checkbox truthful: only include \`config/\` and \`kubejs/\`
  by default, exclude \`defaultconfigs/\`, and warn when it was excluded.
- [ ] Add a pre-share privacy scan for likely tokens, webhooks, credentials,
  private addresses and sensitive config. Show the included paths and findings
  before any upload; do not upload silently.
- [ ] Keep worlds/saves, screenshots, logs, crash reports, personal launcher
  data, account/auth files, unrelated graphics preferences and unrelated
  resource/shader packs out by default.
- [ ] Produce a manifest containing relative path, size and SHA-256 for every
  included file. Reject unsafe paths, symlinks and special files; never include
  files by traversing outside the chosen source roots.
- [ ] Keep mod distribution/license status in the review. Do not silently
  promise a fully bundled pack when redistribution is not allowed or a download
  is restricted; explain the exception and the permitted alternative.

**Exit criterion:** a local-only player ZIP has an explained file list, matching
versions, known unresolved compatibility warnings, privacy results and a
reproducible manifest.

### P1 — Make the default Prism ZIP import for real

- [ ] Make Prism instance ZIP the default artifact for **Share with players**.
  Keep \`.mrpack\` and CurseForge export options in an advanced menu.
- [ ] Check the ZIP layout and metadata: instance folder, \`instance.cfg\`,
  \`mmc-pack.json\`, \`.minecraft/mods/\`, and selected client configuration.
  Pin exact Minecraft and loader component versions.
- [ ] Add structural tests for paths, metadata, component versions, SHA-256
  manifest, missing files, and archive integrity.
- [ ] Run an integration test that imports into a clean Prism instance with its
  supported import path/CLI where available. A successfully written ZIP is not
  enough; record Prism version and import result.
- [ ] Keep server address optional and owner-controlled. Never include world
  saves or any player's Microsoft/Minecraft account material.
- [ ] Avoid guessing Java requirements or memory settings. Let Prism resolve
  compatible Java/runtime defaults unless a tested requirement justifies a
  recommendation.

**Exit criterion:** a clean Prism instance imports the produced ZIP and has the
expected mods/configuration without manual version selection.

### P2 — Add Gofile upload and the one-click share experience

- [ ] Define a small upload-provider interface, keeping archive generation
  independent from Gofile. Implement Gofile first after verifying the exact
  API/domain, authentication and current plan behavior.
- [ ] Keep provider credentials private to the owner, if required. Recipients
  must not need API keys or account credentials to download the archive.
- [ ] Upload from a file stream on a worker thread, with byte-level progress,
  cancellation, finite timeouts, bounded retries and clear error recovery.
  Never read a potentially gigabyte-sized ZIP into memory just for upload.
- [ ] Compute SHA-256 as part of the build and preserve the local ZIP. Verify
  remote metadata/checksum when the provider exposes enough information; do not
  claim server-side checksum verification when the API does not offer it.
- [ ] On success, show **Copy link**, **Copy player instructions**, and
  **Open local ZIP**. Tell the owner whether the link is new, reused or unknown
  to be valid, and retain the local artifact when the upload fails.
- [ ] Treat a link as shareable by anyone who obtains it. Require an explicit
  pre-upload confirmation after showing the exact file list and privacy/mod
  distribution warnings.
- [ ] Confirm current file-size, traffic and retention limits against the exact
  Gofile host/plan used. Do not bake a marketing quota into the UI or promise
  permanent retention. Keep the local ZIP as the durable copy.

**Exit criterion:** a realistic large upload can be cancelled/retried without
freezing the UI or rebuilding the pack, the returned link is copied reliably,
and errors leave a usable local archive.

### P3 — Caching, expiry and recovery

- [ ] Fingerprint the pack inputs: Minecraft/loader versions, source instance,
  hashes of included mods, selected config file hashes and export options.
- [ ] Reuse a cached local ZIP only if the full fingerprint matches. Verify its
  ZIP integrity and manifest before using it.
- [ ] Reuse a previously uploaded link only if the provider can confirm it is
  still valid. Otherwise explain that a new upload/link was created.
- [ ] Keep a bounded export/upload history with fingerprint, local path, size,
  checksum, provider, link, creation time, last verification, outcome and error.
  Never persist provider secrets in the manifest or history.
- [ ] Add cleanup and invalidation policies for old local artifacts and expired
  provider links. Make deletion visible and never remove the only valid local
  copy automatically.
- [ ] Test no-change reuse, changed-mod rebuild, changed-config rebuild, expired
  link recovery, interrupted upload, checksum mismatch, and retry.

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
- **Upload host:** Gofile first, behind a provider adapter. Do not assume a
  stable monthly allowance, maximum upload size or retention period: the
  published pages for \`gofile.io\` and \`gofile.is\` currently describe different
  plan limits. Reconfirm the selected host, its documented API and policies at
  implementation time.
- **Distribution rights:** JAR files are not automatically redistributable just
  because Crucible can read them. Preserve attribution/manifest information and
  respect author permissions and restricted-download rules. A non-redistributable
  mod must be explained, with a supported download/import exception when
  available.
- **Live verification:** online docs and mocked uploads are not substitutes for
  testing a real Gofile upload, link expiry/retention behavior and Prism import
  on the installed launcher version.

## References

- [Prism ZIP import](https://prismlauncher.org/wiki/help-pages/Zip-import/)
- [Prism command-line interface](https://prismlauncher.org/wiki/getting-started/command-line-interface/)
- [MultiMC instance export structure](https://github.com/MultiMC/Launcher/wiki/Export-Instance)
- [Modrinth MRPACK format](https://support.modrinth.com/en/articles/8802351-modrinth-modpack-format-mrpack)
- [Gofile.io plans](https://gofile.io/premium) and [API](https://gofile.io/api); reconfirm current behavior before implementation.
