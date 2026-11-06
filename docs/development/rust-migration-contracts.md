# Crucible Data Contracts and Rust-Core Boundary Inventory

**Status:** Discovery and contract extraction only  
**Date:** 2026-11-06  
**Source baseline:** `main` at `0eb89f2517fc3c4ba9417c2bc97e6c63b5f80b8c`  
**Companion documents:** [Architecture assessment](rust-migration-assessment.md), [Performance audit](rust-migration-performance-audit.md), [Source/runtime audit](rust-migration-source-audit.md)

## 1. Purpose

This document records the data shapes and state distinctions that a Rust implementation must preserve. It is not an invitation to redesign them all at once.

The most important rule is that a record, an observation, a decision, and an effect are different things:

- **Record:** persisted identity/configuration, such as `ServerInstance`.
- **Observation:** what Crucible can currently verify about tmux, Java, a world, or a file.
- **Decision:** whether an action is allowed, requires user confirmation, is blocked, or is unknown.
- **Effect:** an external action such as starting Java, moving a world directory, writing a registry, or uploading a ZIP.

The first Rust core should receive bounded snapshots and return explicit decisions/results. It should not own a server directory, keep a process handle, or decide that missing evidence means an action is safe.

## 2. Contract inventory

### 2.1 Persisted server registry record

The registry remains at `~/.config/crucible/instances.json`, schema version **1**, with a current load-size limit of **16 MiB**.

The record fields in `ServerInstance` include:

| Field | Meaning / compatibility behavior |
|---|---|
| `id` | Stable instance ID; generated UUID string when a new object is constructed without one |
| `path` | Server directory path; registry load expands/resolves this path |
| `name` | Editable display name; not a persistent identity |
| `version` | Pack/server version text |
| `pack_source` | Source label such as Prism, Modrinth, CurseForge or manual |
| `minecraft_version` | Effective Minecraft version metadata |
| `loader`, `loader_version` | Declared loader identity/version |
| `prism_source` | Original Prism folder/archive reference |
| `notes` | Free-form user text |
| `java_args` | Launch flags; defaults include `-Xms2G -Xmx4G` |
| `color` | Legacy color metadata retained for registry compatibility |
| `tmux_session` | Stable process-session name; derived from name only when absent |
| `auto_restart` | Per-instance unexpected-crash watchdog preference |
| `created_at`, `last_started` | Creation and last-start metadata |
| `fml_query_result` | `ask`, `confirm`, or `cancel`; unknown values normalize to `ask` |
| `extra` | All unknown registry keys, preserved on round-trip |

Important compatibility rules:

- `to_dict()` writes unknown `extra` keys first and recognized fields second, so a stale `extra` key cannot override a real model field.
- `from_dict()` preserves unrecognized keys. A Rust representation must not discard future metadata merely because its current struct has no field for it.
- Missing `tmux_session` triggers legacy session-name derivation. That is a compatibility rule, not permission to regenerate existing session names on every read.
- Registry load rejects duplicate IDs, normalized server paths, and tmux session names.
- A malformed or externally changed registry disables writes; Crucible does not silently replace it with an empty registry.
- The one narrow external reconciliation accepts lifecycle-only `last_started` updates while retaining the current live objects. Other edits require a restart/reload path.

**Rust boundary:** a serializable `InstanceRecord` plus pure validation/defaulting helpers may belong in the core. Path resolution, registry signatures, atomic write/fsync/rename and external-edit detection remain filesystem-adapter responsibilities until separately specified.

### 2.2 Process observation versus GUI lifecycle phase

The host-observation values returned by `TmuxManager.status_map()` are:

- `running`
- `unmanaged`
- `stopped`
- `missing`
- `unknown`
- `tmux_missing`

The UI also has transient presentation/action states such as `checking`, `starting`, and `stopping`. Those are not additional tmux observations; they describe a pending operation or a not-yet-complete initial read.

The low-level process probe `probe_running(instance)` is tri-state:

- `True`: exact managed tmux session positively exists.
- `False`: exact session is positively absent (or tmux itself is unavailable, which higher-level status code handles separately).
- `None`: query failed or timed out; state is not known.

Higher-level status adds further evidence:
1. tmux availability is checked;
2. all tmux sessions are listed once;
3. a matching managed session yields `running`;
4. otherwise Crucible checks for an unmanaged Java process tied to the instance;
5. then checks whether the registered server directory exists;
6. only then reports `stopped`.

A failed list-sessions query returns `unknown`, not `stopped`.

**Do not collapse these states.** In particular:
- `unknown` is not false;
- `unmanaged` is not `stopped`;
- `tmux_missing` is an environment state, not proof that Java is absent;
- GUI `starting`/ `stopping` must not be overwritten by a stale status refresh.

Action-time safety probes remain separate from cached display state. Start/Stop and destructive operations are intended to revalidate the target immediately before acting, but the current unmanaged-process observation has two important fail-open edges that must not be baked into the Rust contract:

- `TmuxManager.safe_to_remove()` currently returns safe immediately when the tmux executable is missing, without checking for an unmanaged JVM. The GUI calls this before both unregister and Delete server files, so a manually launched server can evade the preflight in this environment.
- `_unmanaged_pids()` catches discovery exceptions and returns an empty list; `_iter_pids()` also returns an empty list when `/proc/self/stat` is not visible. An incomplete process scan is therefore indistinguishable from a complete scan with no match. `_force_kill()` treats an empty list after `tmux kill-session` as proof that Java exited and may report success without verifying a surviving JVM.

There is a marker nuance too: the exact `-Dcrucible.session=<session>` argv token is accepted before checking `/proc/<pid>/comm`; only the fallback path/cwd match requires `comm == java`. Since `TmuxManager.start()` embeds the marker inside its `bash -c` command string, add a shell-wrapper fixture before tightening or consolidating this logic.

**Proposed observation contract:** `ProcessScanResult = Complete(matches: Vec<ProcessIdentity>) | Incomplete(reason)`. A complete empty match is valid negative evidence; incomplete/unavailable is not. Safety decisions must block on Incomplete. Metrics may still display best-effort partial values, but they must not feed destructive decisions. A separate, narrowly scoped Python safety fix and tests should precede any Rust port of process matching. Python remains the only layer issuing tmux commands during the coexistence phase.



#### 2.2.1 Process discovery must represent incomplete observations

The current `ResourceSampler.find_instance_pids()` API returns only a list. `TmuxManager._unmanaged_pids()` catches every exception and returns an empty list, and `_iter_pids()` can also return an empty list when `/proc` is unavailable. Those values currently conflate:
- complete scan, no matching process;
- complete scan, one or more matching processes;
- scan unavailable/incomplete (permission failure, procfs unavailable, enumeration error, or other exception).

For metrics, best-effort absence may be tolerable. It is not a safe answer for destructive operations. A future shared process matcher should return a typed tri-state result for every instance, for example:
- `Complete { pids: [] }`
- `Complete { pids: [..] }`
- `Incomplete { reason }`

Safety policy must then be explicit:
- **Unregister/delete preflight:** a matching PID blocks; an incomplete scan blocks; only a complete scan with no match can establish absence.
- **tmux unavailable:** still inspect process state; missing tmux is not proof that no manually-started JVM exists.
- **Force-kill verification:** after SIGTERM/SIGKILL, an incomplete scan must not be reported as success. Report that process exit could not be verified.
- **Status/metrics:** may display an unavailable/unknown condition; must not convert incomplete discovery into `stopped`.
- **Backup/readiness:** retain fail-closed behavior if a matching process cannot be confirmed.

There is a concrete existing risk: `TmuxManager.safe_to_remove()` currently returns safe immediately when tmux is unavailable, before checking for an unmanaged JVM. When tmux is installed but `/proc` scanning throws, the helper can still return an empty list and authorize removal. The GUI calls this preflight before both removing a registry entry and the destructive server-folder deletion path, which stages the folder by renaming it and then recursively deletes it. The process scan semantics need a focused safety fix and dedicated tests before this helper is refactored or ported. This concern is also documented in the source and performance audits.

There is another compatibility trap: both current PID matchers accept an exact `-Dcrucible.session=<session>` command-line token without checking `/proc/<pid>/comm`; only the legacy path/cwd fallback requires `comm == java`. Because Crucible injects the marker into a `bash -c` command, a shell wrapper may also carry the token. Add a wrapper-plus-JVM fixture and decide deliberately whether the marker is intended to attribute the wrapper, Java child, or both. Do not silently tighten or broaden this matcher during migration.

### 2.3 Heartbeat/recovery record

The default heartbeat directory is `~/.config/crucible/state/heartbeats/`, with one JSON file per instance ID. The state values are:

- `running`
- `stop_requested`
- `stopped_clean`
- `crashed_handled`

A running heartbeat stores:
- `state`
- `boot_id` from `/proc/sys/kernel/random/boot_id`
- `tmux_session`
- `updated_at`

Stop/reconcile updates preserve the other existing fields while changing state/timestamp. The current writer flushes/fsyncs the temporary file and replaces the destination atomically, but does not fsync the parent directory after rename. Treat that as the existing durability level; do not claim full directory-entry crash durability unless the adapter is deliberately strengthened and tested.

The safety ordering is important:

1. After a server is positively running, write `running`.
2. Before sending graceful Stop, atomically write `stop_requested` to disarm the live watchdog.
3. Only after the session is confirmed gone, write `stopped_clean`.
4. If Stop fails and the server is positively confirmed running again, restore `running`.
5. If observation is unknown, leave the heartbeat disarmed/pending; do not guess.
6. At app startup, only a different boot ID plus positively absent tmux session can produce a whole-host crash report. An uncertain probe is not evidence.
7. A handled report is marked `crashed_handled`; the report is informational and never auto-starts Minecraft.

**Rust boundary:** pure state-transition eligibility/classification is a candidate. Heartbeat persistence, atomic publication, Linux boot-ID reads, tmux queries, and startup orchestration are adapters. Do not move all of `HeartbeatStore` into a Rust library before the failure and durability semantics have fixture coverage.

### 2.4 Watchdog poll snapshot contract

The current watchdog calls `has-session` and `list-panes` separately for each watched instance. A proposed batch adapter may return one row per registered instance:

- exact session observation: `present`, `absent`, or `unknown`;
- pane observation when present: the current-command string, or `unknown` if the batch could not observe it;
- batch-level failure detail, including tmux unavailable, timeout, malformed output, or partial observation.

This is an adapter snapshot, not a new source of process truth. Preserve the existing two-consecutive-miss crash rule:
- unknown session query does not increment the miss count;
- absent session increments the miss count;
- present session requires a reliable Java foreground observation;
- pane unknown does not increment the Java miss count;
- a wrapper/shell in the current pane is distinct from Java foreground;
- pending `stop_requested` heartbeat remains disarmed, and an owner-requested Stop is never reinterpreted as a crash or automatic restart.

A batched `list-panes -a -F` design must match the exact per-session/current-window/first-pane behavior of the existing `list-panes -t =session:` command. Do not assume "active pane" is equivalent to the first row the existing code reads. Golden fixtures should cover two or more sessions, multiple windows per session, multiple panes in the current window, absent sessions, a wrapper shell, tmux timeout, pane-query error and malformed output.

### 2.5 World-start summary contract

`WorldStartSummary` is an immutable point-in-time result with fields:

- `world_name`
- `world_path`
- `state`
- `size_bytes`
- `newest_file_mtime`
- `region_files`
- `player_files`
- `other_developed_worlds`
- `previous_world_name`
- `error`

The observed state values are:

- `existing`: a non-symlink selected folder has `level.dat` and at least one developed region or player-data file.
- `new`: the safe selected folder does not yet exist.
- `incomplete`: the folder exists but is not a developed save by the above test.
- `unsafe`: configured `level-name` is not one safe direct child folder name, or violates path constraints.
- `unavailable`: filesystem inspection failed or could not safely inspect the selection.

The scan:
- preserves the raw configured `level-name` until safety validation;
- rejects absolute paths, separators, dot/dot-dot and selected symlink roots;
- does not follow symlinks;
- recognizes developed alternate saves by actual region `.mca` or player `.dat` content, including known dimension subtrees;
- records size, newest file modification time (excluding `session.lock`), and region/player counts;
- returns an unavailable/error result on incomplete filesystem inspection instead of silently treating the scan as empty.

Confirmation is required when the selected world is new or incomplete, when the selection changed from `last_started_world`, or when older registry data has no previous-world marker while other developed saves exist. The UI writes `instance.extra["last_started_world"]` through the normal registry update only after `tmux.start()` reports success; older records may therefore lack the key and must retain the legacy-confirmation behavior.

This is a useful domain model, but the current `inspect_world_start_state()` is still a filesystem scanner. A clean Rust split would be:
- a path/filesystem adapter that safely gathers the inventory;
- a pure classifier from a normalized inventory record to a `WorldStartSummary` state;
- a Python/Qt adapter that displays the summary and asks the owner;
- the existing Python lifecycle path that revalidates status and then starts the server.

### 2.6 Server properties and configuration validation

`properties.py` contains a version-aware schema (`PropSpec`), parsed document model (`PropertiesDoc`), issues (`PropIssue`) and repair results (`RepairResult`).

This domain has important compatibility behavior:
- legacy GTNH 1.7.10 properties are recognized without treating old valid keys as typos;
- removed vanilla keys are version-gated;
- unknown properties are not automatically rewritten from fuzzy guesses when meanings differ;
- integer/bool/enum/string rules and value ranges come from schema records;
- values and duplicate-key behavior must be preserved according to Java's effective server.properties semantics;
- `PropertiesDoc.load/save` and `autorepair_file` are filesystem operations; parsing/validation of a supplied text snapshot is the cleaner pure seam.

**Potential core slice:** parse a supplied property text buffer into an ordered representation, validate against an explicit Minecraft version, and return structured issues/fixes. Keep file backup, diff, atomic save and owner confirmation in Python until compatibility tests are complete.

### 2.7 Pack-update plan contract

`UpdatePlan` contains:
- current `PackIdentity`
- selected `PackRelease`
- server variant
- local archive or download URL
- paths to preserve
- mods that can be carried
- warnings
- blockers

`plan_update()` is documented as non-mutating: it does not apply an update. It accepts a tri-state running observation:
- unknown (`None`) blocks;
- running blocks;
- only confirmed stopped can proceed to a plan without that blocker.

It also checks pack identity/version ordering, downgrades, stable/beta/RC channels, Java compatibility warnings, world-folder validity, and preserved world/config paths. Although it does not mutate, it currently reads filesystem state and therefore is not entirely pure.

**Rust boundary:** the normalized plan decision could be a later candidate after the disk inventory and version evidence are supplied as an immutable input. Download, extraction, stage, apply, receipt and rollback remain outside the first Rust core.

### 2.8 Fleet-backup generation contract

The current fleet backup generation schema is **1**, with fixed two-generation retention. A generation manifest includes at least:
- status and generation ID
- UTC creation timestamp and retention count
- server/file counts and logical bytes
- bytes copied and bytes reused
- per-instance records and file/directory inventory
- legacy source records/exclusions
- manifest SHA-256

Each copied file record carries a normalized relative path, size, SHA-256, source-stat signature, mode and whether it was reused from the previous immutable generation. Generation publication is staged, verified, atomically renamed, fsynced and sealed read-only; malformed prior generations are not automatically rotated away.

This is a critical on-disk compatibility contract. Before any Rust work touches backups, capture full sanitized manifests, old/new generation pairs with hard-link reuse, and failure-state fixtures. The first Rust slice must not write or restore generations.

### 2.9 Export/share/provider contracts

The client-export workflow intentionally separates:
1. input inventory/fingerprint;
2. JAR identity and distribution review;
3. privacy scan and scan-gap reporting;
4. archive build;
5. sealed member SHA-256 manifest;
6. final archive SHA-256;
7. source/catalog revalidation;
8. explicit owner approval;
9. separate provider upload;
10. verified share-link/history publication.

The Gofile upload result includes the exact download URL/file identity/name/size, local SHA-256, optional remote MD5 and whether size/MD5 verification was present. The provider response is not trusted as an arbitrary URL: Crucible validates the host/path/HTTPS format and checks the returned IDs and metadata.

A Rust port must not collapse build/review/upload into one function or auto-upload as a side effect of export.


### 2.9 Network/provider boundary contracts

Crucible does not have one generic HTTP policy. The source currently has several distinct adapters whose behavior must be preserved and made explicit before consolidation:

| Adapter | Current responsibility | Important constraints and behavior |
|---|---|---|
| `download_http.py` | Shared downloader request/redirect helper | HTTPS-only initial URL, rejects URL userinfo, nonstandard ports, and API-key query names; CurseForge host allowlist is explicit; current CurseForge policy rejects before key lookup/network; authenticated requests cannot redirect outside the CurseForge host set; generic redirects are rebuilt key-free |
| `importers/downloader.py` | Indexed modpack JAR retrieval | Bounded JSON/index and JAR sizes, finite timeout, 64 KiB stream chunks, cancellation checks, temp file in destination directory, hash verification (SHA-512 preferred, SHA-1 fallback), retries for network/hash failures, atomic rename; CurseForge item requests are blocked by the current fallback |
| `mods/modrinth.py` | Modrinth API search/version/dependency and mod file operations | API JSON bounded at 16 MiB and normal API timeout 12 seconds; compatible version resolution refuses to guess if server loader/Minecraft metadata is absent; actual JAR downloads use the shared safe redirect/request helper and have a 2 GiB limit |
| `mods/icon_fetch.py` | Current Add Mod dialog icon retrieval | Strict `cdn.modrinth.com` allowlist; redirects must remain within the provider allowlist; no credentials; maximum 8 MiB; socket timeout/deadline capped at 8 seconds; errors do not echo untrusted URL/header content |
| `mods/curseforge.py` | Legacy provider/API adapter | Online browsing, API/CDN, provider-backed install/version and media path are code-disabled pending policy review; the key file must be user-owned/private if this is ever re-enabled |
| `importers/serverloader.py` | Mojang/Fabric/Forge/NeoForge/Quilt metadata and installer downloads | Fixed public vendor endpoints, default TLS verification, 30-second timeout, 3 retries, 16 MiB metadata and 2 GiB download caps; 64 KiB streamed writes to same-directory temporary files, cancellation checks and atomic replace |
| `importers/pack_update.py` | GTNH version index and server-pack download | HTTPS-only initial URL, 30-second timeout, retries, 16 MiB index and 8 GiB pack caps, 256 KiB chunks, cancellable staging; pack URL comes from the GTNH versions index or explicit owner-supplied HTTPS link, never a guessed URL |
| `sharing/gofile.py` | Player-pack upload | Direct TLS connection to fixed `upload.gofile.io` endpoint; guest mode by default; optional owner token only in Authorization header; same no-follow-open file is hashed, rewound and streamed in 256 KiB chunks; transmitted bytes and file identity are rechecked; cancellation is honored before final multipart boundary; response capped at 1 MiB and download page host/path/ID validated; optional remote size/MD5 are only claimed verified when present and matching |

**Important redirect caveat in the current Python baseline:** `serverloader.py` and `pack_update.py` validate the starting URL as HTTPS but use the default `urllib.request.urlopen()` opener. `modrinth._get()` and the older `modrinth.fetch_bytes()` helper also use default `urlopen()`. Python 3.11 and 3.13 document that the default `HTTPRedirectHandler` accepts redirect targets using HTTP, HTTPS, or FTP; it does not itself enforce HTTPS-only redirects. Therefore, an HTTPS-only check on the original URL is not an end-to-end HTTPS guarantee for these paths. This matters especially for server-loader and modpack archives, which contain executable code.

This has not been exercised as a live network exploit or changed in this documentation PR. Before a Rust network client replaces any of these paths, add deterministic mock-redirect tests and a narrowly scoped Python hardening change: each download operation should validate every redirect target, require HTTPS after every hop, reject userinfo/unsafe ports, and preserve the host policy appropriate to that operation. Do not apply a rigid host allowlist to owner-supplied modpack URLs if legitimate hosts and CDN redirects are intentionally supported; use an HTTPS-only redirect policy there while retaining strict provider allowlists for icon and authenticated provider requests. Preserve finite redirect counts and ensure credentials are never forwarded across hosts.

Official references for the supported runtime matrix: [Python 3.11 urllib.request](https://docs.python.org/3.11/library/urllib.request.html) and [Python 3.13 urllib.request](https://docs.python.org/3.13/library/urllib.request.html).

**Do not conflate provider adapters.** Gofile upload uses `http.client.HTTPSConnection`, not urllib redirects; icon fetching has its own deliberately strict host policy; indexed JAR download uses `download_http`; serverloader and pack update have separate retry/redirect/size behavior. A Rust implementation may eventually share low-level TLS, bounded-body and redirect primitives, but its policy must be supplied per operation as explicit data. Keep provider approval, URL provenance, retry safety, cancellation semantics, and exact artifact checksum validation in the higher-level workflow.

## 3. Recommended first Rust core types and functions

Do not begin by mirroring all Python classes one-for-one. The first Rust crate should model a small number of explicit, versioned values:

| Proposed core value | Input boundary | Pure result |
|---|---|---|
| `InstanceRecord` | Sanitized registry row / golden JSON | Validated record plus preserved unknown fields |
| `ObservedServerState` | Caller-supplied tmux/process observation | Explicit status including Unknown reason |
| `LifecycleDecision` | Instance ID + observed state + action intent | Allowed / requires confirmation / blocked with stable reason code |
| `WorldInventory` | Filesystem adapter's bounded scan result | World start state / confirmation requirement |
| `PropertySnapshot` | UTF-8 text + target Minecraft version | Parsed ordered properties, validation issues and proposed fixes |
| `PackUpdateSnapshot` | Pack identity, target release, running state and inventory summary | Reviewable update plan/blockers |

No proposed core function should:
- spawn tmux or Java;
- create/delete/rename server files;
- read or write Crucible's live registry or heartbeats;
- access the network or credentials;
- update backup retention or perform a restore;
- invoke Qt or display a confirmation dialog.

Expected failures should be explicit typed errors or decision outcomes, not panics or implicit false values.

## 4. Golden fixture plan

Create sanitized, small fixtures in the repository before writing a production Rust call site. Each fixture should be shared by the Python reference and Rust tests.

| Fixture family | Cases that must be represented |
|---|---|
| Registry | Version 1; missing optional keys; unknown `extra` fields; missing session name; invalid FML choice; duplicate ID/path/session; malformed/oversized registry |
| Status/process scan | Running; stopped; unmanaged Java; missing folder; tmux missing; tmux timeout/unknown; marker-only process; shell wrapper plus Java child; complete empty scan; inaccessible `/proc`; absent `/proc`; transient GUI starting/stopping kept separately; tmux missing plus unmanaged Java |
| Heartbeat/watchdog | Each of four states; missing boot ID; changed boot ID; interrupted stop same/different boot; absent, present and unknown tmux results; wrapper alive/Java gone; two misses; restart delay; crash-loop cap; stable uptime reset |
| World summary | Existing developed save; new folder; incomplete folder with `level.dat`; alternate developed save; changed selection; absent prior marker; path traversal; absolute path; symlink root/child; unreadable file |
| Properties | GTNH 1.7.10 legacy keys; modern 1.20+ keys; removed 1.21.2 keys; duplicate keys; escaped key/value; malformed number/bool/enum; unknown key; comments and ordering |
| Pack update | Unknown/running/stopped; stable update; downgrade blocker; beta warning; unsupported Java; multiple worlds; unsafe `level-name`; insufficient free space |
| Fleet backup | Complete generation; prior generation; reused file; changed file; legacy archive; checksum mismatch; missing/symlinked path; retention and publish failure |
| Client export | Exact JAR hash; unknown/manual JAR; conflicting distribution decision; source mutates after review; scan incomplete; duplicate/unsafe ZIP member; manifest mismatch |
| Provider/download | Same-host HTTPS redirect; HTTPS cross-host CDN redirect where permitted; HTTP/FTP downgrade redirect blocked; userinfo/unsafe port; oversized body; timeout; 401/403/429/5xx; missing/invalid digest; wrong digest; credential redaction; cancelled transfer; provider-disabled path before key/network |
| Share | Valid Gofile result; unsafe host/path; malformed optional MD5/size; mismatched size/MD5; source file replacement attempt; cancellation before/during transfer |

Fixtures should contain no user secrets, private URLs or real server/world data. Use generated files with deterministic contents and fake IDs/paths.

## 5. Compatibility tests before integration

For each pure function:
1. Keep the Python implementation as the behavioral reference.
2. Feed identical serialized input to both implementations.
3. Compare all fields and stable error/reason codes.
4. Include unknown future keys/enum values and malformed input.
5. Verify order preservation where it matters (properties, manifest entries, unknown metadata).
6. Test boundary values and size limits.
7. Confirm the Rust function has no filesystem, process or network side effects.
8. Run the whole Python test suite in its existing isolated environment after adding Rust CI; never attach a test to the user's live tmux socket.

A parity mismatch is not automatically a Python bug. Record it, determine whether the old behavior is intentional compatibility, and require an explicit decision before changing persisted/output semantics.

## 6. Remaining contract discovery

The source record inventory is now broad, but before Phase 1 implementation:
- expand real-shaped fixtures with exact sanitized examples;
- finish per-symbol callers for each chosen pure function;
- record the full set of accepted `ServerInstance` fields and version normalization behavior;
- document every backup manifest field and source-stat signature;
- trace provider response variants and retry policy per network path;
- record exact CLI arguments, output, exit codes and working-directory/environment assumptions for any command considered for migration.

No runtime benchmark or parity test is claimed by this document. It is a contract map and test plan, not proof of equivalence.
