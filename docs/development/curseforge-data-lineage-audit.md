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

## 3. Owner decision matrix

This is an engineering decision aid, not a statement of permission. The owner can choose whether to seek approval for the existing integration or pursue minimal retention; only CurseForge/Overwolf can clarify the terms and grant any required authorization.

| Category | Provenance / current issue | Least-retention proposal | Exact external clarification required |
|---|---|---|---|
| API-response metadata | Provider-returned IDs, version labels, hashes, URLs, dependency/compatibility fields and names currently flow into persistent identity/history records. | Keep only the fields required in active UI/operation memory, then discard. No API-response cache, provider mapping, history copy, or provider-derived logs. | Does the no-save/no-cache clause permit active-dialog memory or one-operation use? Which fields, locations and retention durations are permitted? |
| API-derived media and output | Search results include icon URLs; the Add Mod view fetches image bytes and retains decoded thumbnails/pixmaps in active UI state. User-visible operation/error text can also contain provider-derived identifiers or names. The reviewed icon path did not identify a disk image cache; this is not a claim that every logging/output path was exhaustively audited. | No disk/cross-session image cache or provider-derived durable logs. Keep an icon only in active UI memory if that use is permitted; sanitize persistent operational output. | Does displaying provider images and keeping decoded thumbnails in memory count as saving/caching? Are CDN image bytes/URLs treated differently from REST response fields? What restrictions apply to local logs, error output, or receipts containing provider-derived text? |
| Locally computed digests | Fingerprints and SHA-1 values can be computed locally. A fingerprint-to-project association exists only after an API match; the association is not merely a local calculation. | Recompute when needed; do not persist provider mappings. Persist a local digest only if specifically authorized and only without the provider association. | May local hashes/fingerprints be retained solely for local integrity checks? Is an API-confirmed fingerprint association restricted even if the raw fingerprint was computed locally? |
| User-supplied Prism manifest values | Imported `manifest.json` may contain `projectID`/`fileID`. The IDs came from user-supplied input at copy time, but Crucible's copy can survive restart and drive later API lookups. | Do not silently rewrite/delete existing files. Until clarified, avoid creating new app-owned copies or automatically using retained IDs for new API lookups; keep the original imported input intact. | Are user-supplied IDs or a copied source manifest treated differently from API-returned data? May the app retain and reuse them for fresh requests? |
| API-derived filenames | A returned `fileName` can become the installed destination and appear in operation receipts. | Don't retain the name as provider metadata. Use a local-generated name only if that remains compatible with Minecraft/mod tooling and is approved; otherwise keep the feature gated. | Is using a provider-returned filename as a durable installed filename or receipt entry covered by the retention restriction? Is a generated local name acceptable? |
| Temporary staging/history data | Replacement staging can write `added-mods.json`; history copies metadata; receipts record filenames. Deleting staging afterward doesn't prove the writes are allowed. | Never stage provider metadata in JSON or logs; keep receipts to local operation IDs/status. Keep history metadata provider-free. | Are transient in-memory fields permitted? Are temporary disk writes permitted even if removed at operation completion? Which local recovery fields may remain? |
| Downloaded/installed artifacts | JAR bytes are downloaded to staging, installed in `mods/`, and may be copied for rollback. Artifacts and filenames persist. | Install/retain only if artifact storage is allowed. Keep API metadata out of receipts/history. Keep JAR backups only if separately approved. | Does the restriction apply to downloaded bytes, temporary files, installed JARs, and rollback copies? What about normal user-requested installation into the user's own server? |

## Owner decision and fallback options

These are product choices, not claims about what the terms permit.

| Option | What it means | Benefits and costs | Gate |
|---|---|---|---|
| **A. Seek written approval for the current design** | Submit the application/support request below, disclose the current retained fields and source-manifest flow, and ask for an explicit approval or license covering the app, key model, API/CDN use and persistence categories. | Preserves the richest search, identity, update, dependency and recovery behavior if approved. Requires review and may be denied or impose conditions. | Do not enable/distribute the current persistent path unless written approval actually covers it. A working key alone is not approval. |
| **B. Implement minimal provider-metadata retention** | Keep response values transient, stop writing CurseForge identity to install records/history/receipts, and perform fresh lookups after restart. Do not silently mutate or delete existing imported manifests. Preserve their original content, but do not automatically use IDs from Crucible's copied manifest for new provider lookups until that use is clarified. For new imports, process source manifest values in memory rather than making a new durable app-managed manifest copy, unless retention is approved. | Leaves the server, local JAR inspection, manual JAR workflows and Modrinth available. CurseForge links, saved update identity and authoritative dependency state are not available offline and must be resolved again online. Repeated lookups add latency/quota use. Generated filenames may harm usability or compatibility. | This option is **not automatically compliant**: written clarification is still required for active-dialog memory, local digests, staging, filenames, download/install artifact retention and rollback copies. If artifact retention is not allowed, this option cannot preserve CurseForge installation. Existing stored data needs an explicit migration decision; no silent cleanup. |
| **C. Disable CurseForge integration until approval** | Keep CurseForge browsing, fingerprint matching, provider-based installs/updates and automatic use of retained CurseForge IDs disabled. Keep the independent Modrinth and local/manual JAR workflows working. Do not delete or rewrite existing source manifests; do not send their IDs to CurseForge automatically while approval is unresolved. | Most conservative release fallback and avoids assuming transient or artifact storage is exempt. The CurseForge-specific features are unavailable until written approval or a reviewed design is ready. | This is the recommended immediate release posture while API terms and downloaded-artifact retention remain unresolved. |

**Decision rule:** choose A if the owner wants to preserve current functionality and seek permission. Choose B only after the provider clarifies the disputed operational uses and artifact retention; B is an engineering design, not a contractual safe harbor. Choose C for the release in the meantime. The product owner decides which path to pursue; CurseForge/Overwolf decides the approval, permitted key arrangement, data-retention interpretation and artifact permissions.


## 4. Contact status — clarification email sent 2026-10-29

**Status source:** The maintainer reported that the email below was sent. This repository record does not independently prove delivery or receipt, and no reply has been recorded here as of this update.

- **To:** `developers@overwolf.com`
- **Subject:** `Crucible — existing CurseForge API key and usage authorization`
- **Channel:** Direct email; no API key was included.
- **Purpose:** Confirm whether the existing key/account has an approved third-party application; clarify retention of response metadata, local digests, user-supplied Prism IDs and installed/staged/rollback JARs; and ask whether the multi-source Modrinth + CurseForge server manager is permitted under the competition restriction.
- **Official API application status:** **Unverified by the records available here.** The desktop “CurseForge API Key” field is configured and the key succeeded in earlier read-only local probes, but neither fact establishes whether the official application form was submitted or whether Crucible received approval. Do not state that the form was submitted or not submitted without checking the maintainer's application/account records. Do not submit a duplicate application merely because this status is unclear.

### Verification check — 2026-10-29 (no new external response evidence)

This review did **not** find an actual Overwolf/CurseForge reply or official application-status evidence in the connected sources available to this conversation:

- No Gmail, Outlook, iCloud Mail, or Hostinger Mail inbox was connected. The available plugin discovery results showed those mail integrations were not installed/connected, so the sent mailbox, spam folder, and replies cannot be inspected here.
- Conversation/Library search for the exact recipient, the project authorization email, an Overwolf reply, and an application reference surfaced the earlier Crucible handoff and this project's records, but no provider reply, form-submission receipt, application reference, or approval notice.
- This absence of discoverable evidence is **not** evidence that no reply exists, that the form was not submitted, or that the application was rejected. Inbox and account status remain unknown.

The official public [API application guidance](https://support.curseforge.com/support/solutions/articles/9000208346-about-the-curseforge-api-and-how-to-apply-for-a-key) says a third-party developer submits an application; Overwolf reviews it; upon review and approval, a unique key is generated and the developer is contacted by email. The page was modified 2024-12-03 when checked on 2026-10-29. The published [3rd Party API Terms](https://support.curseforge.com/support/solutions/articles/9000207405-curseforge-3rd-party-api-terms-and-conditions), modified 2024-08-14, state that each Developer is issued a unique non-transferable key and prohibit saving/caching API/SDK data and using API materials to build a competing service. They provide no public per-field interpretation for the retention questions in this audit.

Keep these evidence categories separate:

| Evidence category | Verified status | What it does and does not establish |
|---|---|---|
| Clarification email sent | **Maintainer-reported sent on 2026-10-29** to `developers@overwolf.com`; exact recipient, subject and message are recorded above. | Establishes the maintainer's report of sending; delivery, receipt and reply are not independently verified here. |
| Official API application submitted | **Unknown / not evidenced in available records.** | The email is not the application form. No submission confirmation or application reference was found. |
| Official application approved | **Unverified.** | No approval notice, approved app record, or written Crucible-specific authorization was available. |
| API key authentication | **Earlier read-only probes succeeded on 2026-10-29** for the specific endpoints/CDN request described in `CURSEFORGE.md`. | Shows that the configured key authenticated those particular requests at that time; does not prove who issued it, what application/account it belongs to, that Crucible is approved, or that the use/retention model is permitted. No additional authenticated request was made in this review. |

**No new external answers were received or verified for any of the substantive questions.** Key authorization, permitted API-derived metadata retention, image/media/icon URLs and bytes, in-memory thumbnail/pixmap retention, downloaded JARs, destination filenames, staging files, rollback/history copies, and multi-source/competition treatment all remain unanswered by a Crucible-specific written response. The public terms establish the broad restrictions above but do not define these implementation-specific boundaries.
- **Current decision:** Keep PR #34 open/unmerged and keep the current persistent CurseForge path out of release/distribution while awaiting written clarification. Independent Modrinth/local-JAR work may continue. No provider-code or test change is implied by this contact update.

### Copy of the message reported sent

> Hello Overwolf Developer Support,
>
> I'm the maintainer of Crucible, an open-source Linux desktop application for managing users' own Minecraft Java servers.
>
> Project: https://github.com/Roaring1/Crucible
>
> Crucible has a desktop setting labelled “CurseForge API Key”, and we have successfully used a key for local testing. I want to confirm that our application's use is properly authorized. I do not currently have an application reference available, and I don't want to assume that a working key alone confirms approval.
>
> Could you please clarify three things?
>
> 1. **Existing key and approval:** Can you help confirm whether the relevant developer/account has an approved third-party API application for Crucible? If a separate application or approval step is still required, please tell me exactly which one.
> 2. **Data retention:** Crucible currently retains some CurseForge project/file IDs, version information and checksums in local management or history records. What may we retain under the API's no-save/no-cache restriction? In particular, how does it apply to locally calculated SHA-1 fingerprints, user-provided Prism manifest IDs, and JAR files that users explicitly choose to install, including normal rollback copies?
> 3. **Multi-source design:** Crucible supports CurseForge alongside Modrinth and local JARs. Is this type of multi-source server-management application permitted under the API terms' restriction concerning competing products or services?
>
> The integration is documented in the public repository. We want to ensure our use is authorized and understand what changes are required before proceeding further.
>
> I have not included the API key in this message, but can provide non-secret application or account details if needed.
>
> Thank you for your help,
>
> Crucible maintainer

## 5. Historical long-form support/application draft (not sent; superseded by Section 4)

**Subject:** API application and written terms clarification for Crucible desktop Minecraft server manager

Hello CurseForge / Overwolf API team,

We maintain [Crucible](https://github.com/Roaring1/Crucible), an open-source Linux desktop application for managing user-owned Minecraft Java dedicated servers. We are seeking approval and written guidance before enabling or distributing its optional CurseForge integration.

The proposed integration uses the official CurseForge API for project search, exact fingerprint matching of locally installed JARs, compatible file/version selection, required dependency resolution, and user-initiated installation or update through the official download infrastructure. Crucible computes file fingerprints locally; it does not upload the JAR contents for fingerprint identification. It does not scrape CurseForge pages, invent download URLs, bypass distribution controls, or retry denied requests against alternate hosts. Modrinth and local/manual JAR workflows are independent.

The published third-party API terms prohibit saving or caching data obtained through the API/SDK. We want to understand precisely what the terms permit before deciding whether to seek an app-specific authorization or redesign the integration for minimal retention.

Please answer these questions in writing:

1. **Application eligibility and key arrangement.** Is an open-source Linux desktop server manager that operates on users' own Minecraft servers an eligible third-party application? Must each user obtain and configure their own API key, or can an approved application use a developer-issued key in a distributed open-source desktop client? The terms describe each developer's key as unique and non-transferable; we will not bundle or publish a secret. Please specify the approved key model, any quota or separate licensing requirements, attribution/branding requirements, author-consent rules, and whether the same key is accepted for REST API and direct CDN downloads.

2. **No-save/no-cache interpretation.** Which of the following are allowed: holding response fields in process memory while a search dialog is visible; keeping them in memory for one user-initiated install/update; writing temporary metadata to disk during staging and deleting it at the end; and retaining any fields in local operation logs or receipts? If allowed, please state any field, location, or time limits.
   Please also clarify search-result icon URLs, image bytes retrieved from CurseForge media/CDN hosts, the active view's decoded thumbnail/pixmap cache, and user-visible error/output text that includes provider fields or identifiers.

3. **Locally computed data.** May Crucible compute fingerprints or SHA-1 digests locally and retain a digest solely for local integrity/change detection if it does not retain any CurseForge project/file association? May it retain an exact locally-computed fingerprint-to-project/file association returned by a successful fingerprint match? Please distinguish the raw locally computed value from the identity association created by the API response.

4. **User-supplied Prism manifests.** A user may import a Prism pack whose supplied `manifest.json` already contains CurseForge `projectID` and `fileID` values. May Crucible retain an exact copy of that user-supplied manifest in the user's server-management folder and use the user-provided IDs to make fresh API lookup requests later? Does the no-save/no-cache restriction treat values from user-supplied source files differently from values returned by the API? We will not silently modify or delete source manifests to work around the terms.

5. **Filenames, downloaded artifacts and recovery copies.** Is Crucible permitted to download an official JAR to the user's selected server folder and retain the installed JAR? Are temporary staging files and byte-for-byte rollback copies permitted? Is using the API-returned `fileName` as a durable destination filename or in a recovery receipt considered saving API data? If downloaded artifacts are permitted, which metadata must be discarded, and when? If they are not permitted, please state the permitted installation model.

6. **Author distribution controls.** Where the project's third-party distribution setting allows distribution, is a user-requested download/install via Crucible permitted, or is separate author consent, licensing, or attribution required? We will respect restrictions and will not seek alternate download URLs for unavailable files.

We can remove persistent provider metadata and perform fresh online lookups after restart if that is the approved approach, but it would reduce offline identification, saved update selection and dependency resolution. We will keep the current persistent integration unmerged and undistributed until the approved key arrangement and retention treatment are clear. Please confirm whether the application should be submitted through the official [API application form](https://forms.monday.com/forms/dce5ccb7afda9a1c21dab1a1aa1d84eb?r=use1), and whether this request can be associated with that application for written review.

Thank you,
Crucible maintainers

**Submission route:** [CurseForge API application form](https://forms.monday.com/forms/dce5ccb7afda9a1c21dab1a1aa1d84eb?r=use1) · [CurseForge Support ticket](https://support.curseforge.com/support/tickets/new)

## 6. Release gates


- Keep PR #34 open and **do not merge/release/deploy the current persistent CurseForge integration** pending resolution.
- Do not treat possession or successful validation of a key as approval for Crucible or as an exception to the retention clause.
- Resume only after (a) Overwolf/CurseForge provides written answers/approval covering the actual app/key arrangement and the disputed data fields, or (b) the product owner explicitly chooses and implements a no-persistence redesign, and any remaining ambiguous operational uses are clarified in writing.
- After that decision, implement targeted changes and new synthetic tests for absence of prohibited persistence across normal install, skipped-identical-file, Identify online, update/rollback, temporary staging cleanup and operation-history recovery. Repeat broader tests only after code changes or when integration with another PR gives a specific reason.
- Do not store or share API credentials, collect credential-bearing screenshots, scrape the website, bypass author restrictions, or use another application's key.
