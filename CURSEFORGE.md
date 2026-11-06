# CurseForge integration: conditional provider support

Reviewed 2026-10-29. This is implementation guidance, not legal advice or a claim of CurseForge approval.

## Conservative release fallback (this branch)

This branch prepares a conservative fallback while Crucible-specific API authorization and data-retention questions remain unanswered. It is a **candidate branch only**; `main` and installed releases are unchanged.

- The Add Mod dialog exposes Modrinth only; the CurseForge provider selector and API-key entry are hidden.
- The adapter has a default-off gate. CurseForge search/version requests fail locally before reading a key or making an HTTP request.
- The shared download request builder blocks CurseForge API and CDN hosts while disabled. CurseForge media/icon fetches are also blocked.
- Existing saved `cf:` project identities cannot trigger version/dependency lookups. A stale, already-resolved CurseForge `ModFile` cannot be installed through the shared replacement path.
- Modrinth browsing/version/dependency paths remain enabled. Local mod inspection, enable/disable/remove actions, manually obtained JARs through **Add .jar**, and ordinary local server management remain available.
- Local Prism/CurseForge-style archive parsing/export is left intact where it operates on files supplied by the user and does not make CurseForge API/CDN requests. An index-only CurseForge archive still does not cause the app to fetch its remote files.
- The fallback does not delete, rewrite, or migrate existing JARs, `.crucible/added-mods.json`, history receipts, or user-supplied source files. Existing `cf:` records remain local but cannot be refreshed online. The owner can manually obtain an authorized file and add it as a local JAR.

This is a product fallback, **not a legal determination** that the prior implementation did or did not comply. The disabled provider code is retained for review and future decision-making; re-enabling it requires an explicit owner decision after written Overwolf/CurseForge clarification and a fresh review of API-response, media, identity, filename, staging, installed-artifact, and rollback-retention paths.

### Contact and evidence status

The maintainer reports sending an email on 2026-10-29 to `developers@overwolf.com`, subject **“Crucible — existing CurseForge API key and usage authorization.”** It asks about the existing key/account's third-party application approval, metadata and artifact retention, and multi-source integration.

**Follow-up received from Overwolf Developer Relations (owner-provided email update, 2026-11-01 at 3:15 AM):** the team thanked the maintainer and directed all CurseForge matters to the CurseForge Support team, providing <https://support.curseforge.com/support/tickets/new>. The reply did not answer the authorization, retention, or competing-product questions and is not an approval. It establishes the correct next contact route. A CurseForge support ticket has not yet been confirmed as submitted; its draft and field mapping are recorded below for the maintainer to review and submit.

The published application guidance says a third-party developer submits an application, Overwolf reviews it, and a unique key is generated after approval. Therefore:
- Successful authentication with the configured desktop key is evidence only that the tested request authenticated.
- It does not prove that an official third-party application was submitted, that Overwolf approved Crucible, or that the particular key is approved for Crucible's distributed application.
- A sent email and the subsequent routing reply are not an application submission or approval.
- The provider questions remain unanswered until CurseForge Support gives written guidance or points to the applicable approval record.

Outstanding written answers remain: (1) the key arrangement authorized for this distributed open-source client and direct CDN use; (2) retention of API-derived project/file IDs, labels, checksums, locally calculated SHA-1 fingerprints and their associations, user-supplied Prism IDs, icon URLs/bytes and decoded in-memory thumbnails; (3) retention/installation of downloaded JARs, filenames, temporary staging and normal rollback copies; and (4) whether a multi-source server manager supporting Modrinth as well as CurseForge falls within the competition restriction.

### Next contact: CurseForge Support ticket (draft; not submitted)

Use the CurseForge Support contact form at <https://support.curseforge.com/support/tickets/new>. The first-choice route in the page's **Get help** triage is **Something else**, because the request concerns third-party API authorization and terms for a separate desktop application rather than a mod-author project, website bug, or ordinary player issue. If the full ticket form instead presents the broader support split, choose **Mod user related (CurseForge app, Website, Account etc.)** rather than Authors related, then choose the closest available general/other category. Do not guess a more specific API category if it is not offered.

Suggested fields:
- **App name:** Crucible
- **Subject:** Third-party Linux desktop app - API authorization, data retention, and multi-source integration
- **What is this about? (quick-help triage):** Something else
- **What kind of support do you need? (full ticket form, if shown):** Mod user related (CurseForge app, Website, Account etc.)
- **Game / platform:** Minecraft / PC (Java), if those choices are required by the form
- **CF username:** Fill only if the maintainer's relevant CurseForge account username is known; otherwise leave blank unless required
- **CF - Project link:** <https://github.com/Roaring1/Crucible>
- **Description:** Use the draft below. Do not attach or paste an API key, token, or other secret.

Ticket description draft:

> Hello CurseForge Support,
>
> Overwolf Developer Relations directed me to this support team on November 1, 2026, for questions concerning CurseForge API use.
>
> I maintain Crucible, an open-source Linux desktop application for managing users' own Minecraft Java servers: https://github.com/Roaring1/Crucible
>
> Crucible has an optional CurseForge integration and a desktop setting for a CurseForge API key. We have successfully used a key for local testing, but I do not have an application reference or written approval record and do not want to infer authorization from a working key.
>
> Could you please clarify:
>
> 1. **Existing key and approval:** Can you confirm whether the relevant developer/account has an approved third-party API application for Crucible? If not, what exact application/submission and approval steps are required, and how can we identify the application or key owner?
> 2. **Data retention:** The current implementation can retain CurseForge project/file IDs, version information and provider-returned checksums in local management/history records. What may be retained under the API's no-save/no-cache restriction? In particular, please address locally calculated SHA-1 fingerprints and their associations, IDs supplied by a user-imported Prism manifest, user-selected installed JARs, provider-returned filenames, and ordinary staging/history/rollback copies used to replace a mod safely.
> 3. **Multi-source application:** Crucible supports CurseForge alongside Modrinth and local JAR imports. Is this kind of multi-source Minecraft server-management application permitted under the API terms' restriction concerning competing products or services?
> 4. **Required changes:** If any of these practices are not permitted, please identify the required changes or an approved architecture before the integration is distributed.
>
> We want to comply with the applicable terms. We have not included an API key in this request and can provide non-secret application/account details if needed. A written response suitable for maintaining an accurate compliance record would be appreciated.
>
> Thank you,
> Crucible maintainer

This is a prepared request, **not a submitted ticket**. Record the ticket number, submission timestamp, and exact written response here only after the owner submits it and receives the confirmation. Until then, keep the CurseForge online fallback as a draft release option and do not treat the Overwolf redirect as authorization.

Published terms currently state that API data may not be saved or cached and that the API/materials must not be used to build a competing service. The terms page reports a last modification of 2024-08-14; the public text does not define the specific RAM/disk/media/artifact boundaries above. This document does not infer permission from silence.


## Prior provider design (inactive in this fallback branch)

Crucible has its own Python provider adapter; it does not incorporate Prism Launcher source or its application key. Local JAR inspection needs no API. Optional online support covers mod searches filtered by Minecraft/loader, project summaries and website links, compatible release selection, required-dependency resolution, and staged verified installs/downgrades. Online modpack browsing and a full HTML-description viewer are outside this release.

No shared API key is bundled. Authentication and failure behavior are regression-tested with synthetic credentials. **Local live API-key validation was completed on 2026-10-29**: authenticated `GET /v1/games` returned Minecraft, a Sodium/Fabric/1.21.1 search returned results, compatible file metadata and the official per-file download-URL endpoint succeeded, and the approved CDN returned HTTP 200 to an authenticated GET (the response was closed after reading only its first byte; no mod was downloaded). This confirms the configured key works for these read-only local probes, but does not constitute formal approval for Crucible as a distributed third-party app or settle the open distribution/retention terms below. Modrinth and local imports remain usable without a key.

## Historical implementation and remaining approval gates

The official application process requires review. A working key does not by itself resolve the third-party terms governing an implementation. Before general distribution/use of this provider, obtain written clarification for:

- This open-source desktop Minecraft server-manager use case and the competition restriction.
- Application-key distribution versus user-owned keys; do not ship a developer key in source or assume a binary can keep it secret.
- Retention of installed project/file IDs, version labels, checksums and recovery records. Crucible's existing installer writes these to local added-mods.json/history; API-derived fields may fall under the published no-save/no-cache restriction.
- Any response/icon caching, attribution, download and quota requirements. Do not add a persistent API catalog cache without permission.
- A backend if one becomes necessary. A backend can technically protect a key but is NOT an automatic terms workaround: proxy/identity restrictions also need clarification. No hosted proxy or mirror is deployed by Crucible.

The project owner must supply accurate application details and accept any applicable agreement. Do not reuse another launcher's key, scrape around API denials, or treat project-author distribution controls as download errors to bypass.

## Dormant credential handling (not used while fallback is disabled)

For a key whose use is authorized, supported local inputs are:

1. Session-only password input in the CurseForge provider UI.
2. CURSEFORGE_API_KEY in the app's environment.
3. A single-line UTF-8 file at ~/.config/crucible/curseforge-api-key, owned by the current user with mode 600. CURSEFORGE_API_KEY_FILE can select a different private file. Symlinks and non-regular/public files are refused.

Do not put a key in Git, exports, URLs, screenshots or logs. Local private-file permissions reduce exposure; they are not an authorization grant. The file is optional and can remain empty while approval is pending.

## Historical network and installation behavior (inactive while fallback is disabled)

- Both API requests and official CDN downloads use x-api-key over verified HTTPS.
- Credentials attach only to an explicit API/CDN host allowlist, never arbitrary mod download hosts. Authenticated redirects outside that list and HTTPS downgrades fail closed.
- No API-key query parameters, invented CDN paths or website-download redirect fallbacks.
- Missing credentials, HTTP 401/403/429 and server failures produce actionable messages. Explicit denials are not retried through alternate URLs.
- Missing/restricted download URLs remain unavailable. Users may use a permitted website download and Add .jar, or import a fully installed Prism instance without accessing Prism credentials.
- Browser installs/version changes stage and verify files before publication, retain originals and roll back ordinary I/O errors. Pack-index downloads use temporary files, official checksums and structured per-file failures.
- None of this guarantees that arbitrary mods are mutually compatible or that an exported client has been tested in-game.

## Official references

- API: https://docs.curseforge.com/rest-api/
- Key application guidance: https://support.curseforge.com/support/solutions/articles/9000208346-about-the-curseforge-api-and-how-to-apply-for-a-key
- Third-party terms: https://support.curseforge.com/support/solutions/articles/9000207405-curseforge-3rd-party-api-terms-and-conditions
- CDN authentication: https://blog.curseforge.com/introducing-api-key-authentication-for-curseforge-file-downloads/
- Prism source/key warning: https://github.com/PrismLauncher/PrismLauncher/blob/develop/CMakeLists.txt
- Prism licensing: https://github.com/PrismLauncher/PrismLauncher#license
