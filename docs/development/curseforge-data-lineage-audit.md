# CurseForge API data-lineage audit and no-persistence design

Audit target: PR #34, branch work/chat1-provider-lifecycle, reviewed at ccdb5fa plus the follow-on audit commit that adds this document.

**Disposition: BLOCKED pending written CurseForge/Overwolf clarification or a redesigned implementation accepted by them.** This is an engineering data-flow audit, not legal advice. It does not grant API approval or establish compliance.

## 1. Field-by-field lineage

### API responses → in-memory models

| Call / source | API-returned fields used by Crucible | Local transformations / use | Persistent today? |
|---|---|---|---|
| GET /v1/mods/search in crucible/mods/curseforge.py::search | project id, slug, name, summary, downloadCount, logo.thumbnailUrl, author names | Project ID is namespaced as cf:<id>; these populate ModHit and the Add Mod result rows. The slug is used for the official source-page URL. | No explicit disk cache in the reviewed CF dialog path. Results, selected hits, resolved files, icon bytes and pixmaps are held in dialog/worker memory while open. |
| POST /v1/fingerprints/432 in identify_fingerprints | exactMatches[].id, file.fileFingerprint, file.modId, file.id, displayName, fileName, hashes[].value for SHA-1 | Crucible computes a normalized Murmur2 fingerprint locally from each JAR, submits the numeric value, and requires the API exact-match response to agree. The result maps a local filename/fingerprint to a provider project/file identity. | **Yes:** identity fields are merged into .crucible/added-mods.json. |
| GET /v1/mods/{id}/files in list_versions / _file | file id, displayName, fileName, downloadUrl, fileLength, hashes, dependencies, isAvailable, gameVersions | Builds a ModFile: project ID, file/version ID, displayed title/version, filename, download URL, SHA-1, size and required dependency IDs. Availability and compatibility fields gate selection. | Selected release fields are persisted by the shared installer as described below. The download URL itself is not written to the identity record. |
| GET /v1/mods/{id} in project_url | returned project ID and slug | Validates the project association and constructs an official website URL. | Not intentionally persisted; used for navigation. |
| Official CDN response | downloaded JAR bytes; transport response metadata | JAR is streamed to a staging path and locally hashed; current code compares the local SHA-1 to the API-returned SHA-1 before publication. | **The installed JAR is intentionally persisted.** Temporary staged bytes are also written to disk during an operation. Whether filenames or the necessary act of saving a downloaded artifact raise any issue under the broad terms requires Overwolf's clarification; do not assume a filename-only workaround settles it. |

### User-supplied Prism manifests (additional persistence path)

The Prism importer has a distinct source-data path that should not be conflated with an API-response cache:

- `crucible/importers/prism.py` copies an imported Prism pack's `manifest.json` into `<server>/.crucible/source-pack/manifest.json` (the reviewed code is around lines 611–617).
- A supplied `files[]` entry can contain CurseForge `projectID` and `fileID`. At the time the importer copies this file, those values originate in the user's imported pack manifest, not in a CurseForge API response received by Crucible.
- The copied manifest persists across restarts. The Prism downloader later reads the IDs and calls the official API to resolve file details/download information.
- This is therefore an **additional durable source of CurseForge identifiers and future API requests**, but it is not accurate to describe the original copy, by itself, as persistence of a response field returned by Crucible's API call. Whether the API terms restrict retaining or reusing identifiers from a user-supplied manifest is not settled by the public language reviewed here.

Do not silently delete or rewrite imported user input as part of a no-persistence redesign. Ask Overwolf/CurseForge whether the original user-supplied manifest may be retained and used for fresh lookups, and whether retaining only user-provided `projectID`/`fileID` values is treated differently from retaining identifiers returned by the API. If a strict redesign is selected, explicitly decide how future imports and already-copied manifests behave; do not treat this source path as resolved merely by removing entries from `added-mods.json`.

### Local values versus provider values

- **Computed locally:** CurseForge's normalized fingerprint (fingerprint_file), the app's SHA-1 of a local/staged JAR (_sha1), destination/backup paths and operation UUIDs. These calculations do not require the API.
- **Returned by CurseForge:** project ID, file ID, display/version label, remote filename, URL, advertised SHA-1, advertised size, project slug, required dependency IDs, and availability/compatibility metadata.
- **Mixed provenance / conservative treatment:** the fingerprint submitted to the API is locally computed, but the saved identity association is created only because the API returned an exact match. The returned item.id and file.fileFingerprint are also response fields. Do not persist the fingerprint-to-CurseForge-ID association absent clarification, even though the fingerprint number itself can be recomputed from the JAR.
- The current installer stores mf.sha1 (the provider-returned SHA-1), rather than replacing it with the independently computed local digest. The local digest is used to verify the downloaded/staged file.

### Disk persistence sinks found in the reviewed code paths

1. **<server>/.crucible/added-mods.json — durable identity / install record.**
   - crucible/mods/versions.py::identify_files merges the API-derived identity into the record and calls _write_record. For a CurseForge match, fields include project_id (cf:<modId>), version_id (API file ID), version (API display name), filename (API file name), fingerprint (local fingerprint that was matched by the API), identity (Crucible's local constant), and sha1 (API-returned hash when present). Because it merges {**prior, **meta}, prior fields such as title/size can also remain.
   - crucible/mods/modrinth.py::install_files is shared by provider-backed mod installations. It writes a record entry keyed by mf.filename, with project_id, title, version, version_id, sha1, and size. On a CurseForge ModFile, those values originate from the CurseForge response / _file. It writes this record both when an identical file already exists and when a newly downloaded file passed validation.
   - check_update and dependency checks later read this saved association. They are not independent new persistence sinks, but they make persisted provider IDs/version IDs operationally significant after restart.
2. **Temporary staging metadata on disk.** versions.replace_files calls mr.install_files(tmp, files); that creates tmp/.crucible/added-mods.json under a temporary staging directory. The routine subsequently reads those staged records and copies them into the live record. Temporary-directory cleanup removes this file after the operation, but it is still a disk write during the operation; do not treat automatic deletion as proof that it satisfies a no-save/no-cache term.
3. **<server>/.crucible/mod-history/<operation>/added-mods.json.** Before replacement, replace_files copies the previous identity record into operation history. That snapshot can contain CurseForge IDs, version labels, hashes and fingerprints already written by Identify online or prior installations.
4. **<server>/.crucible/mod-history/<operation>/receipt.json.** The receipt has local operation status plus original, backed_up, and installed filename lists. The new installed values derive from the selected ModFile.filename, which currently originates in CurseForge's fileName. The receipt does not currently store project IDs, version IDs, URLs or hashes directly.
5. **Mod JAR staging / final file / backup paths.** Downloaded JAR contents are written to staging and then to mods/; previous installed JARs are copied to history for recovery. Those are file artifacts rather than a JSON metadata cache, but the installed name currently comes from the provider's fileName. The API terms' application to downloaded content and destination naming must be included in the written question.
6. **In-memory-only use observed in the reviewed path.** AddModDialog keeps search hits, selected project IDs, resolved ModFile objects, icon bytes and pixmaps while the dialog is active. The reviewed curseforge.py adapter / download_http.py do not create a response-cache file or persist the download URL. ~/.config/crucible/instances.json is the server registry; the reviewed CurseForge path does not add response metadata there. The user's own key file / environment/session key is credential configuration, not an API-response cache.
7. **`<server>/.crucible/source-pack/manifest.json` — copied imported source manifest.** The Prism importer copies a user-supplied pack manifest that may contain CurseForge `projectID`/`fileID` entries. Those values originate in the imported file at copy time, rather than in the API response; the copied file is durable and the downloader later uses its IDs for API resolution. Treat this as a separately sourced persistence path: the public no-cache wording does not clearly answer whether keeping or reusing user-provided identifiers is restricted. Confirm in writing rather than labelling it categorically permitted or categorically an API-response cache.

This audit maps the reviewed call chain into the known identity and replacement persistence sinks. It is not a claim that any future or third-party extension, OS-level cache, or unrelated module has been independently verified.

## 2. Least-disruptive no-persistence architecture (proposal, not implemented)

**Until written approval arrives, do not ship the active persistent CurseForge flow.** The current PR can remain open for review, but a green test suite is not an authorization decision.

1. **Keep local-only behavior unchanged.** JAR inspection, starting/stopping servers, manually adding an already downloaded JAR, and the Modrinth provider continue to work without CurseForge identity records or a CurseForge key.
2. **Use CurseForge responses only for the immediate foreground action.** Search/project/release/dependency results live only as long as needed for the active view or install transaction, then are discarded. Do not create an API catalog cache, provider-history cache, or cross-session response cache. Because the contract says “save or cache any data,” ask whether even active-dialog in-memory result retention is acceptable for ordinary UI rendering; do not assert that all in-memory state is exempt.
3. **Split installation from identity persistence.** Replace the shared side effect in install_files with separate stages: resolve files in memory; download into a temporary staging directory; use the SHA-1 response only in memory for the immediate integrity comparison (if permitted); compute a local digest from the received/staged bytes for local integrity checks; publish the JAR; discard provider metadata. Do not write a CurseForge added-mods.json entry or copy any provider response fields into a receipt/history record.
4. **Do not replace one cache location with another.** No key-value database, sidecar file, hidden directory, temporary JSON identity map, local “cache”, telemetry or logs of provider response fields. For strictest handling, operation receipts use a local operation UUID, status and locally computed hashes, not provider IDs/labels/URLs. If a provider-supplied filename is currently needed, the strict variant would use a Crucible-generated local filename (for example a local content-digest-based basename) and keep the remote name only in transient UI state. This is more disruptive: mod filenames become less recognizable, and some mods/tools/users may depend on conventional filenames. A more convenient remote filename requires an explicit answer on whether storing it as the installed destination is permitted.
5. **Re-identify on demand after restart.** When online and an authorized key is available, recompute the installed JAR's fingerprint locally, perform a fresh exact lookup, resolve compatible releases/dependencies, act, and discard the response. Do not persist the provider identity mapping. On restart the app has no saved CurseForge project/version link. Offline, JARs still load and can be inspected and the server can run, but CurseForge source links, update selection and authoritative online dependency checks are unavailable until a fresh request succeeds. Repeated lookups increase latency and quota usage; without cached results there is no offline online-catalog view.
6. **Keep recovery data local but strip provider metadata.** Keep prior JAR byte-for-byte backups and enough locally derived information for safe rollback. Redesign receipts so their fields don't preserve API-returned labels or IDs. The exact permitted local filename/recovery scheme depends on Overwolf's answer about filenames and downloaded artifacts.

This is the safest engineering baseline under the literal published wording, but it is not a guarantee of compliance: the terms are broad enough that CurseForge must clarify UI memory, required file downloads/installation, destination filenames, locally computed hashes, and local recovery metadata. A written exception / license for the current persistent design could preserve more behavior than a no-persistence redesign.

## 3. Written support/application request (draft — not submitted)

**Subject:** API application and written clarification: Crucible Linux desktop server manager and no-cache restriction

Hello CurseForge / Overwolf API team,

I maintain Crucible, an open-source Linux desktop application for managing user-owned Minecraft Java dedicated servers. The proposed optional CurseForge provider lets a user search projects, match an installed JAR using a locally computed file fingerprint, select a compatible file and required dependencies, and install/update files into that user's own server folder. JAR bytes are not uploaded. Requests use the official API and approved download endpoints; Crucible does not scrape the site, invent download URLs, bypass author distribution controls, or retry denied downloads through alternate hosts. Modrinth and local/manual JAR workflows remain available independently.

I am preparing/submitting the developer application and would like written clarification before enabling or distributing this integration. Section 3.1 of the published third-party API terms says the developer must not “save or cache any data obtained through the API or SDK.” The current prototype persists response-derived project/file IDs, display/version labels and SHA-1 values in local install records, also snapshots those records into operation history, and records installed filenames in recovery receipts. It also stages selected files on disk to verify and install them.

Could you please confirm in writing:

1. Is this desktop server-management use case eligible for API approval, and are there any application, licensing, attribution, quota, author-consent, or distribution restrictions specific to it?
2. When a user imports a Prism pack whose supplied `manifest.json` contains CurseForge `projectID` and `fileID` values, may Crucible retain that original manifest in the user's own server folder and later use those user-provided identifiers for fresh API lookups? Does your no-save/no-cache clause distinguish user-supplied source files from data returned by the API?
3. Does “save or cache any data” prohibit these local per-server records and history snapshots? What about a response kept only in process memory for an open dialog or one immediate install operation, and temporary metadata files deleted at operation completion?
4. May the app persist a locally computed digest solely to detect whether the user's local JAR later changed, if it does **not** persist a mapping to a CurseForge project/file ID? May it persist any association between an exact local fingerprint and provider identity? Are API-returned filenames permissible as installed destination names or in local recovery receipts?
5. Are users required to apply for and configure their own individually issued API keys, or can an approved developer use an app-specific key in a distributed open-source desktop client? The published terms say a developer's key is unique, non-transferable and may not be shared. We will not bundle or publish a secret. If no end-user key model is supported, what documented authentication arrangement is approved for a desktop client? We will not use a proxy unless you expressly approve the arrangement in writing.
6. Is downloading the official JAR to the user's chosen server folder and retaining the installed file and local rollback copy permitted under the applicable API terms and project distribution setting? If yes, which response metadata must be discarded and when?

We can implement a strict no-provider-metadata-persistence mode if required, but it affects restart behavior, offline use, update/dependency UI and file naming. We will keep the integration disabled for general release until the correct approval and retention design are clear. Please point us to the applicable application form and confirm whether this reply can be treated as written guidance for this specific app.

Thank you.

**Submission:** the official API application guidance directs third-party developers to a reviewed application and requires acceptance of the terms; it says review considers author earnings, API/server/CDN impact, and author consent for third-party distribution. Start at the [CurseForge API application form](https://forms.monday.com/forms/dce5ccb7afda9a1c21dab1a1aa1d84eb?r=use1). For a written support ticket, use [CurseForge Support](https://support.curseforge.com/support/tickets/new).

## 4. Release gates

- Keep PR #34 open and **do not merge/release/deploy the current persistent CurseForge integration** pending resolution.
- Do not treat possession or successful validation of a key as approval for Crucible or as an exception to the retention clause.
- Resume only after (a) Overwolf/CurseForge provides written answers/approval covering the actual app/key arrangement and the disputed data fields, or (b) the product owner explicitly chooses and implements a no-persistence redesign, and any remaining ambiguous operational uses are clarified in writing.
- After that decision, implement targeted changes and new synthetic tests for absence of prohibited persistence across normal install, skipped-identical-file, Identify online, update/rollback, temporary staging cleanup and operation-history recovery. Repeat broader tests only after code changes or when integration with another PR gives a specific reason.
- Do not store or share API credentials, collect credential-bearing screenshots, scrape the website, bypass author restrictions, or use another application's key.
