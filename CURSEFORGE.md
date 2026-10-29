# CurseForge integration: conditional provider support

Updated 2026-10-29. This is implementation guidance, not legal advice or a claim of CurseForge approval.


## Current compliance disposition (2026-10-29)

A field-by-field source-to-sink audit and proposed no-persistence architecture are documented in [the CurseForge API data-lineage audit](docs/development/curseforge-data-lineage-audit.md). The audit found API-derived CurseForge identity/install metadata persisted in `.crucible/added-mods.json`, temporary staging metadata, historical `added-mods.json` snapshots and installed-file names in operation receipts. The audit now includes an explicit owner decision matrix and three fallback choices: seek written approval for current behavior, pursue a minimal-metadata redesign only after artifact/storage questions are clarified, or disable CurseForge integration while retaining independent Modrinth and local/manual workflows. A further reviewed path is the copied imported Prism manifest at `.crucible/source-pack/manifest.json`, which can preserve user-supplied CurseForge project/file IDs across restarts and is later used for fresh API lookups. The audit also treats media/icon URLs and in-memory thumbnails, plus provider-derived error/output text, as open retention questions; the reviewed icon path has no identified disk image cache, but that is not an exhaustive audit of all output paths. The manifest values originate from imported input rather than an API response at copy time, so their treatment under the published no-cache clause remains an explicit question—not a proven API-response cache or a proven exception. A broad no-save/no-cache clause means passing tests or having a working key is not enough. **Keep PR #34 open; do not merge, release, deploy or distribute the current persistent CurseForge integration pending written clarification/approval or an accepted no-persistence redesign.** The support/application request in the audit is drafted but has not been submitted.

## Current scope

Crucible has its own Python provider adapter; it does not incorporate Prism Launcher source or its application key. Local JAR inspection needs no API. Optional online support covers mod searches filtered by Minecraft/loader, project summaries and website links, compatible release selection, required-dependency resolution, and staged verified installs/downgrades. Online modpack browsing and a full HTML-description viewer are outside this release.

No shared API key is bundled. Authentication and failure behavior are regression-tested with synthetic credentials. **Local live API-key validation was completed on 2026-10-29**: authenticated `GET /v1/games` returned Minecraft, a Sodium/Fabric/1.21.1 search returned results, compatible file metadata and the official per-file download-URL endpoint succeeded, and the approved CDN returned HTTP 200 to an authenticated GET (the response was closed after reading only its first byte; no mod was downloaded). This confirms the configured key works for these read-only local probes, but does not constitute formal approval for Crucible as a distributed third-party app or settle the open distribution/retention terms below. Modrinth and local imports remain usable without a key.

## Approval remains open

The official application process requires review. A working key does not by itself resolve the third-party terms governing an implementation. Before general distribution/use of this provider, obtain written clarification for:

- This open-source desktop Minecraft server-manager use case and the competition restriction.
- Application-key distribution versus user-owned keys; do not ship a developer key in source or assume a binary can keep it secret.
- **Blocking compliance issue for the current implementation:** the published [CurseForge 3rd Party API Terms](https://support.curseforge.com/support/solutions/articles/9000207405-curseforge-3rd-party-api-terms-and-conditions) prohibit saving or caching data obtained through the API/SDK. This provider flow currently persists CurseForge project/file IDs, version labels and API-returned SHA-1 values in `.crucible/added-mods.json` and operation history. Treat that behavior as incompatible with the published terms unless Overwolf/CurseForge gives written permission or a compliant design removes persistence of API-derived data. **Do not merge or distribute the active persistence path based only on a working key.**
- Any response/icon caching, attribution, download and quota requirements. Do not add a persistent API catalog cache without permission.
- A backend if one becomes necessary. A backend can technically protect a key but is NOT an automatic terms workaround: proxy/identity restrictions also need clarification. No hosted proxy or mirror is deployed by Crucible.

The project owner must supply accurate application details and accept any applicable agreement. Do not reuse another launcher's key, scrape around API denials, or treat project-author distribution controls as download errors to bypass.

## Credential handling

For a key whose use is authorized, supported local inputs are:

1. Session-only password input in the CurseForge provider UI.
2. CURSEFORGE_API_KEY in the app's environment.
3. A single-line UTF-8 file at ~/.config/crucible/curseforge-api-key, owned by the current user with mode 600. CURSEFORGE_API_KEY_FILE can select a different private file. Symlinks and non-regular/public files are refused.

Do not put a key in Git, exports, URLs, screenshots or logs. Local private-file permissions reduce exposure; they are not an authorization grant. The file is optional and can remain empty while approval is pending.

## Exact installed-JAR identification

When a private key is configured, **Identify online** computes CurseForge fingerprints locally using its Murmur2-derived algorithm (seed 1, excluding ASCII TAB/LF/CR/space) and submits only the numeric fingerprints to the official `POST /v1/fingerprints/432` endpoint. JAR bytes are not uploaded. CurseForge fingerprints are not SHA-1 values; the code keeps these algorithms and identity labels separate. The algorithm details are not specified in the API schema, so the implementation is cross-checked against the published Murmur2/seed-1 compatibility implementation at https://gist.github.com/arvitus/5126ca93898c008abed49c0db0e5ef5e and the Aternos client documentation at https://github.com/aternosorg/php-curseforge-api. Only API `exactMatches` whose returned fingerprint agrees with the submitted fingerprint are considered, and a fingerprint is linked only if it resolves to one unambiguous project/file pair. Ambiguous exact matches remain unlinked rather than being selected by response order. Partial/fuzzy matches are never treated as an identity. Without a configured CurseForge key, Modrinth exact SHA-1 identification continues to work.

The two identity providers fail independently: a Modrinth lookup outage does not skip a configured CurseForge fingerprint lookup, and a CurseForge key/API failure does not discard valid Modrinth exact SHA-1 hits. Malformed response envelopes are treated as provider failures, not as successful no-match results. Partial results include a warning; when neither provider can complete and there is no exact match, the error identifies the failed provider(s). Provider-specific IDs and hashes stay namespaced: `cf:` projects are routed only through CurseForge adapters, and returned release/version IDs must agree with their selected project before use. Pinned CurseForge dependencies are resolved from compatible CurseForge releases, not from Modrinth's version endpoint.

Existing identity records are revalidated against the current local JAR's SHA-1 or saved CurseForge fingerprint before they can drive version changes, dependency checks, or project-page navigation. Project/version association is checked again when dependency metadata is fetched. A manually replaced JAR therefore cannot inherit the old project's identity merely because its filename stayed the same. Add Mod only opens a CurseForge source page when it has a provider-supplied slug; otherwise it explains that the page is unavailable rather than inventing a numeric-ID URL.

## Network and installation behavior

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
