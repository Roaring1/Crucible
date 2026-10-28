# Mod distribution catalog

Crucible must not infer redistribution permission from a JAR filename, mod ID,
metadata, repository popularity, or a license guess. The catalog is a reviewed
record of evidence for the **exact JAR bytes**.

## Current state

The shipped file is `crucible/data/mod_distribution_catalog.json`. It currently
contains zero records. Consequently, every real mod currently remains
**unverified** until a maintainer curates an exact-artifact entry. The catalog
implementation does not itself certify any real mod or legal permission.

## Matching and decisions

Each record is keyed by a lowercase SHA-256 digest of the whole JAR:

- `permitted`: the cited evidence was reviewed and supports bundling these exact
  bytes. The UI still displays the basis, source and evidence, and the owner must
  confirm distribution responsibility.
- `conditional`: a decision is recorded only with one or more explicit
  conditions. Those conditions are displayed in the review and must be followed.
- `prohibited`: this exact artifact must not be included in the ZIP. If the
  record contains a reviewed `alternativeDownloadUrl`, Crucible omits the JAR
  and puts the project/evidence/alternative links in the manifest, review and
  `CRUCIBLE-MOD-DOWNLOADS.txt`. The recipient must manually obtain the compatible
  mod and install it into the imported instance's `.minecraft/mods/` folder.
  If no alternative URL is recorded, export fails rather than bundling the
  prohibited bytes or silently creating a pack with no next step.
- No matching SHA-256 record: `unverified`. The current workflow continues to
  include the JAR only after its ordinary owner review/acknowledgement; it must
  not describe the catalog as approving it.

A different version, repack, or byte-modified file has a different digest and
is unverified unless that exact artifact has its own record. Mod ID/name fields
are descriptive only; they never determine the decision.

## Record format and review requirements

The JSON root contains `schemaVersion: 1`, a human-maintained `catalogVersion`,
and a `records` array. Each curated record contains:

- `sha256`: exact lower-case 64-character SHA-256 of the reviewed JAR.
- `modId` and `modVersion`: descriptive identifiers for the artifact.
- `decision`: `permitted`, `prohibited`, or `conditional`.
- `projectUrl`: HTTPS project/home page.
- `evidenceUrl`: HTTPS page supporting the distribution decision.
- `verifiedOn`: the date the evidence was checked, in `YYYY-MM-DD` format.
- `basis`: a concise explanation of why the evidence supports the decision.
- `license` and `licenseUrl`: optional human-reviewed license details.
- `conditions`: required for a `conditional` decision.
- `alternativeDownloadUrl`: optional HTTPS alternative for a prohibited artifact.
- `notes`: optional curator notes, with no secrets or private account data.

URLs with non-HTTPS schemes, embedded credentials or non-standard ports are
rejected. Duplicate SHA-256 records, invalid dates, malformed fields and an
invalid catalog file fail closed. Only exact JAR SHA-256 hashes are looked up.
The catalog's raw-file SHA-256 is included in Prism input fingerprints, so
catalog changes invalidate cache reuse. Crucible also checks that the catalog
did not change during export and rechecks the current revision when reviewing
the resulting ZIP.

## How maintainers should add a record

1. Obtain the exact JAR artifact from a known source and compute its SHA-256.
2. Read the original project/license/permission terms. Record a link to the
   specific evidence page and the date it was checked.
3. Choose a decision only when the evidence supports it. Do not interpret a
   general source/download page as a redistribution grant.
4. For `conditional`, write each condition in plain, bounded text. For
   `prohibited`, add an alternative URL only when that destination has itself
   been reviewed as an appropriate source for players to obtain the mod.
5. Add a focused test fixture and have the record reviewed before merging.
   Synthetic test records must be clearly labeled and must never be merged into
   the shipped catalog as real permission records.
6. Re-check records periodically and when mod authors change hosting or terms.
   Replacing the catalog changes its fingerprint and forces cached packs to
   rebuild.

This catalog is operational evidence for the owner's review process, not legal
advice and not a guarantee that every distribution restriction has been found.
Resource packs are not covered by this first catalog and remain separately
unverified in the sharing review. Links and terms may change; owners must still
review conditions and make the final distribution decision.
