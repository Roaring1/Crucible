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



#### 2.1.1 Registry load/write and identity edge semantics

The current registry load contract has a mismatch between its declared schema and its enforcement:

- The constant says version 1, and the writer always emits `{"version": 1, "instances": [...]}`. The reader checks that the root is a JSON object and instances is a list, but it does not validate the version or require the version field. It accepts missing/unknown versions and ignores unknown root keys; a later save rewrites to version 1 and drops unknown root-level metadata. Per-record unknown keys are different: `ServerInstance.extra` preserves them on round trip, with known fields overriding any collision.
- Missing record `id` generates a new UUID during each load. Missing `tmux_session` derives a safe slug from the display name; provided session names from a persisted registry are not passed through `validate_session_name()`. Other known-field types are mostly trusted rather than schema-validated. Paths are expanded/resolved and duplicate normalized paths, IDs and sessions are rejected.
- The external-change signature is `(device, inode, size, mtime_ns)`, not a content digest. The narrow reconciliation accepts only `last_started` and `last_started_world` changes and keeps the live object identities. All other changes remain write-blocking until restart.
- The writer uses the fixed sibling `instances.tmp`, opens it with normal write/truncate semantics, fsyncs the file and atomically replaces `instances.json`. It does not use exclusive/no-follow temp creation, fsync the containing directory, or hold a cross-process registry lock around check-and-publish. Two independent Crucible processes can race after both pass the signature check. The temp path can also be a pre-existing symlink. These are current source properties; the tests do not cover them.
- `HeartbeatStore._path()` uses the raw `instance.id` as a path component. Registry load does not enforce a safe ID segment. A crafted ID containing separators/traversal/absolute-path text can direct heartbeat read/replace outside the state directory. Heartbeat write does use unique `mkstemp`, but the final replace target still derives from that ID. The store also reads heartbeat JSON without a size cap and maps malformed/non-object data to the same `None` as a missing file.

The future Rust contract should distinguish `RegistryLoad::Missing`, `Loaded(version, records, unknown_root_fields)`, `UnsupportedVersion`, and `Invalid(reason)`; it must not downgrade an unknown schema by assuming version 1. Validate instance IDs as one safe path segment at both the registry and heartbeat adapter boundary while preserving historic safe identifiers (not only UUIDs). Audit all ID-derived storage consumers together: HeartbeatStore and legacy BackupManager currently interpolate raw IDs; fleet generations and custom server icons hash them; FleetBackupManager's legacy migration helper rejects unsafe components. Any ID repair must update all dependent state via an explicit recoverable mapping, never silently rotate an ID on each load. Keep file IO, symlink handling, directory durability and inter-process locking in the Python adapter until hardened and tested. The record decoder currently requires only `path` and `name`; it accepts unvalidated types for most fields, accepts any provided ID/session string, and generates a fresh in-memory UUID on each load when `id` is missing. It therefore needs a typed validation/normalization result before it is a Rust `InstanceRecord`.

**Registry writer concurrency contract (not yet implemented):** GUI and CLI have separate in-memory managers. `last_started`/`last_started_world` reconciliation is a narrow exception, not a multi-writer transaction. A content change can evade the current signature if inode/size/mtime are preserved because the signature omits ctime and bytes. Two processes can pass the signature guard before either writes; the fixed `instances.tmp` then races, and a stale full snapshot can overwrite the other process's change. A future Python writer should acquire a dedicated owner-private registry `flock` before rereading/reconciling, verify a content/signature snapshot while holding it, use an exclusive no-follow unique temp in the config directory, file-fsync, replace and directory-fsync, then update the manager's known snapshot. External editors remain non-cooperating and must be detected by a content hash or robust signature check. No Rust code should write registry files during this migration.

**Heartbeat transition contract (not yet implemented):** `HeartbeatStore` uses secure `mkstemp` for its temp file but its state transition APIs are unlocked read-modify-write sequences. `mark_stop_requested`, `mark_stopped_clean`, and `mark_crashed_handled` must not be allowed to overwrite a newer `mark_running` or stop transition based on stale input. Serialize per instance ID with a separate lock or use a revision/CAS contract, and re-read/validate immediately before publish. Heartbeat reads need a size cap, no-follow handling and typed `Missing`/`Valid`/`Invalid/Unavailable` results; corrupt data must not be treated as evidence of clean Stop or absence of a crash. The heartbeat parent directory needs the same explicit directory-durability decision as registry publication. A safe ID component must be validated in both the registry and store, with historic safe non-UUID IDs retained.

#### 2.1.2 Pack provenance and local sidecar records

Small sidecars have different authority and durability levels. Do not treat every valid JSON object as verified application state.

| Sidecar | Current format/behavior | Rust contract requirement |
|---|---|---|
| `.crucible/pack-provenance.json` | Unversioned JSON. Reader has no size cap, no symlink rejection and no schema/field validation. Any truthy provider+version outrank all other identity evidence; absent/invalid variant defaults to Java 17. `pack_mods` is an unchecked list of strings used as exact pack-owned baseline. Writer uses a fixed `.json.tmp`, no fsync, and swallows OSError. | Versioned schema; bounded no-follow read; validate provider/version/variant; each pack-mod name is one safe JAR leaf and optionally bind to SHA-256. Legacy receipt is `LegacyUnverified`, not equivalent to a validated new receipt. Publication is atomic, exclusive-temp, fsynced, error-visible. |
| `.crucible/import-summary.json` | Unversioned, direct-written JSON; reader has no size cap or symlink check. Its source/detected.name are advisory GTNH filename candidates; temporary extraction roots may be recorded and explicitly marked temporary. | Bounded advisory data only; never authoritative over provenance; distinguish installed root from deleted temporary detection root; malformed/unreadable should not claim verified identity. |
| `.crucible/added-mods.json` | Unversioned object keyed by filename. Reader silently returns empty on corrupt/unreadable data; no size/symlink/filename/value validation. Modrinth writer uses fixed `added-mods.tmp`, no fsync and swallows OSError. Version replacement writer uses random temp + file fsync/replace but no directory fsync. | Bounded schema and safe leaf keys; exact identity fields typed; store digest semantics explicitly; report record-write failure separately from JAR install success; maintain legacy reader without granting trust to malformed data. |
| `CRUCIBLE_IMPORT.md` | Human-readable best-effort report, direct text write; not consumed as automated authority. | Keep as presentation output, not a decision source. |
| Server icon override | Instance ID is SHA-256 hashed before forming its filename; input <=20 MiB/4096 edge, private directory/file, randomized temp and atomic replace. | Preserve privacy/size/dimension limits; keep ID-derived storage key deterministic; treat icon bytes as presentation-only. |
| Share history | Versioned schema 1, event allowlist, <=50 records, <=1 MiB, validated Gofile URL/hash/types, private 0700/0600, random temp, file fsync and atomic replace. | Maintain strict typed schema and privacy allowlist; parent-directory fsync and directory-descriptor race hardening remain adapter tests. |

`compute_carry_mods()` consumes pack provenance to decide which JAR names belong to the new pack and which owner additions survive an update. That is a consequential decision, not just metadata display. A receipt with an unchecked `pack_mods` array can alter the next update's carry set. The pure Rust classifier may receive a validated immutable baseline, but it must never parse an arbitrary sidecar and silently promote it to authoritative evidence.

The existing tests cover valid provenance receipt precedence, a corrupt receipt fallback, import-summary source inference, and the pack-baseline carry calculation. Missing tests include oversize and symlinked sidecars, wrong schema/type/provider/variant, unsafe/duplicate pack-mod names, pack JAR replaced under the same filename, fixed-temp symlink/collision, failed receipt publication after swap, added-mod record write failure after successful JAR download, and legacy-to-versioned migration.


#### 2.1.3 Registered path identity and symlink provenance

- Both `add_instance()` and registry `load()` resolve server paths and store the canonical target string. The original user-supplied path spelling and whether it was a symlink are not retained.
- The GUI delete flow later calls `validate_delete_target(instance.path)`, which refuses a leaf symlink. For a path that was a symlink when registered, that check receives the already-resolved target and cannot detect the original alias. It will therefore authorize staging and recursive deletion of the target directory after the owner sees the canonical path in the confirmation. This is a behavior/policy ambiguity, not proof of an exploit: choose explicitly whether to reject symlink roots at registration, retain both original and canonical path, or treat canonical-target deletion as intended and disclose it. Add a registration-through-symlink/delete fixture.
- `find_server_dirs()` (CLI `crucible scan`) also follows directory symlinks: it tests `child.is_dir()` without rejecting `child.is_symlink()`, recursively walks aliases outside the chosen root and can visit the same physical directory multiple times. It has a depth cap but no visited inode set/root-containment check. The scan is read-only and prints candidate paths; `crucible add` resolves them before registration. There are currently no focused tests for symlink traversal, out-of-root discovery or duplicate physical directories.
- This behavior must be an explicit contract for a future Rust walker: choose follow/no-follow semantics, cycle handling, root containment, duplicate physical identity and permission-error reporting. Do not assume Rust's default filesystem iterator follows or skips symlinks in a way that matches Python.






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
- **Start/restart preflight:** a matching PID blocks; an incomplete scan blocks; only a complete scan with no match can establish process absence. Enforce this at the shared start effect boundary, not only in GUI/CLI/shortcut callers.
- **Unregister/delete preflight:** a matching PID blocks; an incomplete scan blocks; only a complete scan with no match can establish absence.
- **tmux unavailable:** still inspect process state for removal/stop observations; missing tmux is not proof that no manually-started JVM exists. A new Start still cannot proceed without tmux.
- **Force-kill verification:** after SIGTERM/SIGKILL, an incomplete scan must not be reported as success. Report that process exit could not be verified.
- **Status/metrics:** may display an unavailable/unknown condition; must not convert incomplete discovery into `stopped`.
- **Backup/readiness:** retain fail-closed behavior if a matching process cannot be confirmed.

There are concrete existing risks at both removal and launch boundaries. `TmuxManager.safe_to_remove()` currently returns safe immediately when tmux is unavailable, before checking for an unmanaged JVM. When tmux is installed but `/proc` scanning throws, the helper can still return an empty list and authorize removal. The GUI calls this preflight before both removing a registry entry and the destructive server-folder deletion path, which stages the folder by renaming it and then recursively deletes it. Separately, `TmuxManager.start()` does not require an unmanaged-process scan: it checks the exact tmux session and an IPv4 listener for the configured port, but it does not prove that the same instance is not already running outside tmux. CLI `cmd_start()` and the watchdog call this shared method directly; the GUI relies on cached status following asynchronous world inspection; the shortcut does a fresh `get_status()` but inherits the lossy scan. The configured-port check is useful but is not a complete process-identity guard. A shared tri-state scan must therefore gate Start/restart as well as removal/force-kill. Add a separate safety patch and dedicated tests before refactoring or porting this helper. This concern is also documented in the source and performance audits.

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

The current watchdog tick is every 10 seconds, but it does not yet use this batch snapshot: it calls `probe_running()` (`tmux has-session`, 2-second timeout) once per watched instance, then calls `is_java_foreground()` (`tmux list-panes`, 2-second timeout) for each present session. The loop is serial, so the source-level count is up to 2N tmux subprocesses per poll for N healthy watched servers. The main-window health worker is a separate, batched `list-sessions` path. This mismatch with the watchdog module docstring is source-confirmed; the wall-time impact has not been measured. The batch adapter should report per-instance session/pane observations and batch-level failures rather than collapsing one failed query into false for all servers. Measure subprocess count, p50/p95 tick duration, and per-instance delay at 1, 5, 20 and 50 watched servers, including a slow/timeout case. The Rust core should receive the finished observation snapshot; it should not own the Qt timer or start a parallel poller.

### 2.4.1 Diagnostics-embedded resource observation

The `SystemTab` widget is embedded inside `InfoTab` (the Diagnostics page), not a separate top-level workspace tab. It is a demand-scoped presentation sampler, not a lifecycle authority. It starts a 2-second timer while Diagnostics is shown, stops it on hide, and refreshes immediately from both `InfoTab.load()` and `SystemTab.showEvent()`; the exact initial event count needs a Qt test. The current _refresh() runs on the Qt GUI thread, calls ResourceSampler.find_instance_pids() (a full /proc enumeration for the selected server), samples each matched PID's stat/statm/cmdline, and reads /proc/meminfo. _iter_pids() calls os.listdir('/proc') without catching an enumeration OSError, so unavailable procfs can escape the timer callback even though the module says sampling is best-effort. The process association contract is the same exact marker plus Java/path fallback used elsewhere; preserve it during any metrics optimization.

A future metrics adapter may move this work to a bounded worker or reuse a fresh per-instance association snapshot. Diagnostics load also synchronously calls `instance.validate()` (which recursively calls `get_bundled_jars()`), then calls `get_bundled_jars()` a second time for display, plus `get_world_names()`/`get_mod_count()`. This duplicate filesystem traversal is a Python-first optimization candidate: compute one immutable diagnostics snapshot, reuse the nested-JAR inventory for both validation and display, and offload only if first-load profiling shows a measurable delay. It must return explicit Complete, Incomplete(reason), and per-PID-disappeared observations. An incomplete metrics result renders unavailable; it must never be treated as server offline or passed into Start/Stop/removal as negative process evidence. Keep refresh demand-scoped to the visible tab, and never create a resident poller or reuse a metrics snapshot across lifecycle actions. Add tests for show/hide timer ownership, 2-second refresh, event-loop responsiveness at scaled procfs sizes, os.listdir('/proc') failure, malformed/vanished PID entries, and no stale snapshot reuse.
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
- returns an unavailable/error result on incomplete selected-world filesystem inspection instead of silently treating the scan as empty.

Two current implementation caveats must be explicit in the reference contract:

1. `ServerInstance.world_level_name()` returns `"world"` both when `server.properties` is absent and when a present file raises `OSError` during read. It also follows a symlinked `server.properties`. This loses a safety-critical distinction before the summary helper sees the name. A safer boundary is `WorldSelectionRead = MissingUseDefault | Value(raw) | Unavailable(reason)`. Preserve the vanilla/default behavior only for a genuinely absent config file (or a valid regular file without `level-name`); treat a present unreadable, non-regular or symlinked config as unavailable/unsafe. Add fixtures for absent, valid default, unreadable, symlink, broken symlink, malformed and custom-name cases.
2. The selected-world summary currently recursively stats every file to compute size, newest mtime and counts; it is called on server selection and again for each Start. Alternate-world detection can also recursively inspect dimension subtrees, while an unreadable alternate directory is currently collapsed to `False` by `_is_developed_world()` rather than `unavailable`. A Rust implementation must not preserve that false-negative ambiguity. Consider a quick safety inventory plus separate optional detail stats, but any incomplete scan of paths needed to decide whether an alternate developed world exists must block or require owner review, not be reported as "no other save". Benchmark traversal cost independently from GUI responsiveness.

Confirmation is required when the selected world is new or incomplete, when the selection changed from `last_started_world`, or when older registry data has no previous-world marker while other developed saves exist. The UI writes `instance.extra["last_started_world"]` through the normal registry update only after `tmux.start()` reports success; older records may therefore lack the key and must retain the legacy-confirmation behavior.

This is a useful domain model, but the current `inspect_world_start_state()` is still a filesystem scanner. A clean Rust split would be:
- a path/filesystem adapter that safely gathers the inventory;
- a pure classifier from a normalized inventory record to a `WorldStartSummary` state;
- a Python/Qt adapter that displays the summary and asks the owner;
- the existing Python lifecycle path that revalidates status and then starts the server.

### 2.6 Server properties and configuration validation

`properties.py` contains a version-aware schema (`PropSpec`), parsed document model (`PropertiesDoc`), issues (`PropIssue`) and repair results (`RepairResult`). These are not currently a complete implementation of Java's `java.util.Properties` parser.

The current validator's exact compatibility behavior is:
- `PropertiesDoc.loads()` splits physical lines, preserves blank/comment records and order, recognizes only a literal `=` separator, trims key/value edges, and treats a non-comment line with no `=` as a key with an empty value. It does not implement Java's full property grammar for whitespace/colon separators, continuation lines, or general escape decoding.
- `PropertiesDoc.get()` and `set()` use the last occurrence of a duplicate key; `set()` updates that last record and leaves earlier duplicates in place. Validation emits a warning that the last value wins, and autofix deliberately does not remove duplicates.
- `to_text()` retains comment text and line order, but serializes every key/value as `key=value` with a trailing newline. This is not byte-for-byte preservation of whitespace, separator style, escape spellings, or line endings. `save()` writes a sibling `.tmp` and replaces the file; it does not use the registry/fleet backup durability protocol.
- Numeric blank/invalid values are the only start-blocking validation errors. Out-of-range numerics are warnings, as are boolean/enum/unknown-key issues. This conservative severity split is intentional.
- Legacy GTNH 1.7.10 keys and numeric difficulty/gamemode values are accepted with an explicit supported-version context. Versioned removed properties are reported without unsafe rename suggestions. When version is unknown, version-specific keys stay advisory/unknown instead of being assumed valid or removed.
- Fuzzy suggestions are suppressed for ambiguous keys such as `server-id` and `server-name`; modded namespaced world-generator IDs are accepted under the specific `level-type` rules. The complete schema and version map remain in Python source and must be captured in fixtures before a Rust rewrite.
- `PropertiesDoc.load()` converts an `OSError` into an empty document. `validate_file()` also returns an empty issue list when the path is missing or any exception occurs. Therefore `has_blocking_errors()` can return false for an unreadable/missing file; it is not a successful-read proof. `autorepair_file()` uses the same loader and may report an unchanged successful result when the source could not be read. GUI `_preflight_properties()` also suppresses all exceptions. These are current behaviors to harden separately; a Rust parser must not encode “no issue” as “the file was read successfully.”
- `world_level_name()` has a separate narrow reader: it recognizes a stripped line beginning exactly with `level-name=`, and returns `"world"` both for missing, empty, or unreadable files. It does not support `level-name : value`, `level-name value`, escaped keys, or Java-property continuations.
- `server_port()` has a different simple reader for `server-port=`, uses the last matching key, preserves a blank final value, but falls back to `25565` for both missing and unreadable files.
- Pack-update's `_configured_level_name()` uses a separate logical-line/key-value parser with Java-style separators and escape handling, but currently returns `"world"` after an `OSError`. Consequently world selection used by the GUI/CLI start path, world convenience paths, server-port conflict check and pack-update preserve-plan path is not guaranteed to use one identical parse result.

**Rust boundary:** the narrow pure seam is still a supplied text snapshot plus an explicit Minecraft version returning the existing ordered document/issues/fixes behavior. The first parity test must reproduce Crucible's current parser, not silently broaden it to full Java semantics. Keep file I/O, read-completeness, backup creation, repair publication, and confirmation in Python. Separately define a typed read contract for safety-sensitive effective values, such as `PropertyRead::Missing`, `Value(raw)`, and `Unavailable(reason)`; do not map unreadable to a default. Before consolidating the multiple readers, add cross-fixtures that compare GUI/CLI start, `ServerInstance` world/port helpers, and pack-update parsing against the effective values actually used by supported Minecraft/Forge launchers. Any decision to adopt full Java-properties parsing is a separate behavior change and needs GTNH 1.7.10 and modern-version compatibility tests.

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

`plan_update()` is documented as non-mutating: it does not apply an update. Its docstring inaccurately says it does not touch the filesystem; the current implementation reads the server root, properties, local archive, world inventory, preserve paths and disk usage/recursive size. It accepts a tri-state running observation:
- unknown (`None`) blocks;
- running blocks;
- only confirmed stopped can proceed to a plan without that blocker.

It also checks pack identity/version ordering, downgrades, stable/beta/RC channels, Java compatibility warnings, world-folder validity, and preserved world/config paths. There is not yet a complete/incomplete inventory result. `_world_names()` can return a partial selected-world list after an `iterdir()` error or skip inaccessible alternate saves; `_dir_size()` can return a partial byte count after traversal/stat failures. These results feed both the preserve list and the free-space estimate, so an incomplete scan can under-preserve owner worlds or under-estimate staging needs. A mandatory fleet checkpoint remains available before mutation, but it does not mean all world saves are carried into the updated live tree.

**Proposed input contract:** `WorldPreserveInventory { entries, complete, errors }` and `TreeSizeEstimate { bytes, complete, errors, capped }`. Planning must block on an incomplete required inventory; apply and rollback must compare fresh inventories against the reviewed plan before staging/rename. `WorldPreserveInventory` and `TreeSizeEstimate` should be gathered in Python and passed as immutable snapshots to any Rust classifier. This is a candidate for a later Rust decision function only after Python behavior is hardened and golden fixtures exist. Download, extraction, stage, apply, receipt and rollback remain outside the first Rust core.



### 2.7.1 Pack-update and world-mutation effect/lock contract

The shared operation lock is an OS-level inter-process lock, not a Python-only mutex: `fleet_operation_lock()` opens `DEFAULT_FLEET_BACKUP_DIR / fleet-operation.lock` with no-follow semantics (current default: `~/.local/share/crucible-backups/fleet/fleet-operation.lock`) and takes non-blocking exclusive `flock`. A second process receives `BackupAlreadyRunning` rather than capturing or mutating a partially-swapped tree. The lock serializes scheduled/manual fleet backup with pack updates and destructive world operations.

Current production call-site coverage is incomplete:

- Normal pack-update apply is protected in both GUI and CLI. Each outer lock spans the mandatory complete fleet checkpoint and `apply_update()`. The backup callback calls `run_backup(operation_lock_held=True)` to avoid nested acquisition.
- GUI and CLI rollback call `rollback_update()` without taking the operation lock; the helper itself does not acquire it. Rollback renames the live server root and restores a prior tree, so a concurrent backup can observe an intermediate/missing root.
- GUI and CLI previous-install pruning call `prune_previous_installs()` without the lock; the helper itself does not acquire it. A concurrent rollback may be restoring the same previous tree.
- World swap/reset/wipe workers and full-server restore use the lock. The visible Worlds-tab pre-swap cleanup button does not: it directly calls `BackupManager.prune_pre_swap_dirs()`. A separate worker cleanup branch would checkpoint and check stopped state, but the visible button does not dispatch through that branch.

This contract must be closed before any Rust implementation owns or accelerates these effects. Every effect must have one clear lock owner and explicit ownership convention. Do not acquire the same non-reentrant OS lock again from inside an outer transaction. Add cross-process contention tests for rollback, prune, and pre-swap cleanup and verify that a lock conflict produces no rename, delete, or partial registry/filesystem change.

The live pack-update blocker also needs a stronger process observation: `_update_safety_blocker()` rechecks the tmux session and the configured IPv4 listening port before the backup and again before the live rename, but it does not query the instance's unmanaged Java process association. If tmux is absent, `probe_running()` is false; the port guard catches an ordinary bound server, but it is not proof that no JVM is still using the folder during early startup or shutdown. The same applies to rollback. Require `ProcessScanResult::Complete(no match)` in addition to the current tmux/port checks before a live directory rename. Matching PIDs or an incomplete process scan must block.


### 2.7.2 GTNH release-version parsing and ordering contract

The current Python reference is `parse_pack_version()` and `channel_of()` in `crucible/importers/pack_update.py`.

- A valid version requires major and minor decimal components separated by a dot. Patch is optional and defaults to zero. A leading `v` or `V` is optional.
- An optional prerelease channel is `alpha`, `beta`, or `rc`, case-insensitive. Zero or more `-`, `_`, `.`, or space separators may appear before the channel and between the channel and its optional decimal sequence. Missing prerelease number defaults to zero. The separators may be absent (for example, `2.8.4beta3`).
- Sort key is exactly `(major, minor, patch, channel_rank, prerelease_number)`; ranks are alpha=0, beta=1, rc=2, stable=3. Thus prereleases sort below the corresponding stable version, and RC sorts above beta. Unparseable inputs return no version; `channel_of()` returns `unknown`.
- Python uses `re.IGNORECASE`, Unicode-aware `\d`, and `str.strip()` before parsing. The Rust pilot accepts ASCII digits only and uses Rust `str::trim()`, which does not strip Python's additional U+001C..U+001F whitespace controls. These are recorded incompatibilities, not full semantic parity. Python `re.IGNORECASE` also folds U+017F long-s and U+0130/U+0131 dotted/dotless I in `Horizons`; the Rust name parser uses ASCII case-insensitive token matching. Rust-only tests pin the current divergence, while Python-only tests preserve the accepted Python inputs. The Rust parser is not callable from production code; resolve the differences or add a tested Python fallback before any integration.
- Rust stores decimal components as canonical strings and compares by digit length then lexicographically. It has no fixed-width bound and currently accepts the 4,301-digit case in the Rust test. Python's `int()` conversion has a runtime-configurable decimal digit limit on CPython 3.11+ (default 4,300); `parse_pack_version()` does not catch the `ValueError` if a captured component exceeds the active limit. This is an additional intentional pilot divergence, now covered by a Rust acceptance test and a Python reference test. The user/runtime may change or disable the CPython limit, so do not hard-code 4,300 as a universal Python contract. [Python 3.11 documentation](https://docs.python.org/3.11/library/stdtypes.html#integer-string-conversion-length-limitation).
- The shared fixture is `rust/crucible-core/fixtures/pack-version-cases.tsv`. Rust unit tests and `tests/test_rust_pack_version_fixtures.py` must both consume that exact file. Keep the fixture authoritative for accepted syntax, canonical tuple, and channel output. Add edge cases there before widening the parser.
- The current tested head `81bf49f6d70abede8e4374b966893e289ee6c23b` passed formatting, 12 Rust unit tests and Clippy with warnings denied on stable Rust 1.99.1; the separate Rust 1.85.0 MSRV job also passed all 12 tests. Python 3.11 and 3.13 jobs each reported 848 passed and 95 subtests, and the Qt interaction smoke passed in both jobs. See [Rust CI](https://github.com/Roaring1/Crucible/actions/runs/42333476845) and [Python CI](https://github.com/Roaring1/Crucible/actions/runs/42333476850). The parser still has Unicode-digit, dotted/dotless-I and long-s case-folding, and Python-specific trim differences. The shared fixture validates only the explicitly common ASCII cases; Rust-only tests pin current divergence, and Python-only tests preserve Python behavior. This slice has no filesystem, network, tmux, Qt, or process effects and makes no performance claim. It must not alter release selection in Python.

### 2.7.3 GTNH server-pack name parsing and Java variant classification

The Python reference is `identify_gtnh_name()` in `crucible/importers/pack_update.py`. It is deliberately narrower than full `identify_pack()`, which reads receipts, import summaries, registry metadata and the filesystem to establish provenance/confidence.

The pure name parser contract is:

- Search the entire supplied string (not only the basename) for the case-insensitive pattern `GT[ _]?New[ _]?Horizons[ _]+(.+?)[ _]+Server[ _]+Java[ _]+(8|17-\d+)`.
- The captured version is the shortest text ending at a delimiter that permits a valid Server/Java suffix. Validate it with the version parser and return its trimmed original spelling (retain prerelease case and leading zeroes). An unparseable first syntactic match returns no identity; the function does not continue to a later GTNH substring.
- Java token `8` maps to `java8`; token `17-<digits>` maps to `java17_2X`. The suffix is not end-anchored, so a normal archive extension or trailing text is accepted. The Java build number is not retained.
- Search/prefix separators are intentionally specific: optional single space/underscore between GT/New/Horizons, one or more spaces/underscores before the version, then one or more before Server, Java, and the variant. The parser does not infer identity from loose names, changelog files, receipts or the presence of GTNH files on disk.
- The Rust pilot is `rust/crucible-core/src/pack_identity.rs`, exposing `GtnhServerName`, `PackVariant`, and `identify_gtnh_name()`. It is pure string processing and calls the Rust version parser to validate the capture. It preserves the first-syntactic-match behavior and trailing-text acceptance.
- The shared fixture is `rust/crucible-core/fixtures/pack-name-cases.tsv`; Rust unit tests and `tests/test_rust_pack_identity_fixtures.py` consume it alongside the unchanged Python reference. Cases include official Java 8/17 names, path prefixes, case and separator variants, prerelease version spelling, invalid Java tokens, April Fools' non-version, first-match precedence and trailing text.
- Python accepts Unicode decimal digits in both the version and Java 17 digit token because its regex uses Unicode-aware `\d`. Rust version validation accepts ASCII decimal digits only; its Java 17 suffix check currently requires the first digit after `17-` to be ASCII (the remaining numeric suffix is not consumed because only the variant classification is returned). This is a known compatibility gap. The Python-only regression test preserves Unicode behavior; the Rust-only test pins the current divergence.
- This is a parity/toolchain exercise, not a hot-path optimization. It does not read receipts, resolve a folder, inspect a Prism source, infer confidence, or change the version/variant Python callers currently use.

The second pilot and expanded Unicode regression cases passed on tested head `81bf49f6d70abede8e4374b966893e289ee6c23b`: 12 Rust unit tests on stable 1.99.1 and MSRV 1.85.0; 848 Python tests plus 95 subtests on each supported interpreter, and both Qt smoke jobs passed. No runtime bridge or production caller has been added.

### 2.8 Fleet-backup generation contract

The current fleet backup generation schema is **1**, with fixed two-generation retention. A generation manifest includes:
- `schema`, `status=complete`, generation ID matching the containing directory, UTC creation timestamp and retention count;
- server/file counts, logical bytes, bytes copied and bytes reused;
- an `instances` array with server identity/context, registry snapshot, capture status, active world, world inventory, directory records, file records, explicit exclusion records, and legacy world-archive records/exclusions;
- `legacy_sources` and `legacy_sources_excluded` for opaque migrated retired/manual backup sources, including source type, original relative paths, captured file inventory, counts/bytes and skipped-entry reasons;
- `manifest_sha256`, computed from canonical sorted/compact JSON of the rest of the manifest.

Each copied file record carries a normalized relative path, size, SHA-256, source-stat signature (`device`, `inode`, `size`, `mtime_ns`, `ctime_ns`), original mode and whether it was reused from the previous immutable generation. Directory records carry path and original mode. Excluded records preserve why a symlink, non-regular object, cache, log or temporary entry was not captured.

The manifest SHA-256 is an unkeyed integrity checksum, not a signature. It detects a changed manifest when the digest is unchanged; it does not authenticate a manifest against an actor able to rewrite both fields. Snapshot trust also relies on private/immutable generation directories and deep payload verification.

`verify_generation(deep=False)` checks the manifest checksum/schema/status/directory ID, inventory counts, safe relative paths, file/directory existence, sizes and immutable permissions, but does not hash each payload. `deep=True` additionally hashes every payload file. Default `restore_server()` and the Backup-tab restore worker require deep verification; shallow verification is for fast listing/inventory only. The Rust/Python contract must preserve this difference.

Publication is staged, manifest-written, file/directory-sealed, fsynced and shallow-verified before atomic rename to the final generation ID; final directory entries are fsynced strictly before old generations are pruned. A durability failure after rename rolls back the unpublished generation. Invalid retained generations block rotation. `FleetBackupManager.run_backup()` acquires the outer `fleet_operation_lock(root)` automatically unless the caller explicitly passes `operation_lock_held=True`; it then takes the separate `fleet-backup.lock` for the generation publisher. Callers already holding the outer non-reentrant flock must use the explicit already-held flag. This is why normal Backup-tab/CLI capture is serialized even without a front-end lock, while a custom caller-owned world/pack transaction must document its outer lock scope.

Restore targets are separate new directories, never the registered live path or an existing directory. Restore validates every target parent against symlinks, deep-verifies the generation by default, copies and re-hashes payload into a sibling staging directory, restores original file/directory modes, and atomically renames staging to the new target. Legacy world ZIPs are restored in a separate hidden recovery directory with an index, not injected into the active world save.

The current manifest is loaded and JSON-decoded as a single unbounded file; no manifest-byte/entry-count cap is present. Benchmark file count, manifest bytes, decode time and peak RSS before setting any new limit. If real installs produce large manifests, consider a versioned sharded index while retaining one complete generation identity and integrity contract. Do not weaken default deep verification for restore just to reduce I/O.

This is a critical on-disk compatibility contract. Before any Rust work touches backups, capture full sanitized manifests, old/new generation pairs with hard-link reuse, and failure-state fixtures. The first Rust slice must not write or restore generations.


### 2.8.1 Legacy world ZIP and BackupManager compatibility contract

The old per-instance world ZIP manager remains a read/restore compatibility surface; it is not the current fleet-generation publisher. Its behavior must be described separately from immutable fleet generations.

- Current root: ~/.local/share/crucible-backups/{instance.id}/. The manager appends the raw instance ID without a single-safe-path-component check. Registry load currently permits hand-edited IDs; a path-containing ID can redirect the legacy backup directory outside its intended root. Fleet generations avoid this by hashing IDs through _server_key(). Validate legacy IDs before filesystem use while retaining historic safe identifiers; do not silently hash/rehome existing raw-ID folders.
- The legacy manager does not enforce owner-private directory modes. New directory permissions depend on ambient umask and existing paths are not chmod-hardened. These ZIPs contain world data. Contrast with fleet backup roots/generations, which are explicitly restricted to 0700/0555/0444-style immutable modes.
- Archive listing follows *.zip symlinks via stat(). Slot sidecars are unversioned {archive}.zip.json files, read without a size cap or symlink rejection. Writes use fixed .tmp siblings with ordinary write+replace, no exclusive/no-follow creation or file/directory fsync. A lost slot label is treated as non-fatal; keep old no-sidecar ZIPs backward compatible.
- Legacy create_backup() is a compatibility API, not the normal production fleet-capture path. It writes a predictable .zip.partial with ZipFile write mode, tests CRC and the presence of level.dat, then renames. It has no explicit archive member-count/total expansion/per-member/ratio policy at creation, and the temp path is not exclusive/no-follow. Failure cleanup removes the predictable partial path.
- Restore materializes the ZIP directory via infolist() and rejects empty archives or more than 2,000,000 entries, unsafe paths, unapproved world roots, symlink/special files and duplicate normalized member names. But _backup_level_name() first calls namelist() with no entry cap, so the central directory is already allocated before the later count cap. Expanded bytes are summed and compared with free space while preserving max(512 MiB, 5% of free space); there is no compression-ratio or per-member cap.
- Extraction streams 1 MiB blocks to the new world root and checks exact uncompressed member length/ZIP CRC, then only verifies non-empty level.dat and expected dimension-folder presence. It does not have a per-file SHA manifest for ordinary legacy archives. A fleet-exported archive's BackupEntry.sha256 is intended to be checked by _verify_fleet_archive_hash(), but a source-wide call-site search finds no caller - the helper is currently unused. _validate_backup_archive() and swap_world() validate ZIP shape and free space but do not compare the expected fleet archive SHA before extraction. The existing test_fleet_backed_world_archive_must_match_manifest_sha256 invokes the helper directly with a deliberately wrong digest; it does not exercise swap_world() or prove that production restore invokes the check. Add a mutation-after-export end-to-end test that verifies the actual swap refuses before the live world rename.
- World swap itself has a valuable caller-owned recovery contract: the WorldTab worker takes a fleet generation and holds fleet_operation_lock, BackupManager renames the current world aside, extracts/verifies the saved world, updates level-name, and on ordinary failure removes the new target and renames the old folder back. The lower-level BackupManager methods do not acquire the lock or independently check stopped state. However, _extract_validated_backup() writes members directly into the target world root with ordinary path-based opens after a separate validation pass; it does not use a no-follow directory descriptor or a private fully-staged world root. A parent path replaced with a symlink between validation and open can redirect a write. The exception path uses shutil.rmtree(target_world_root, ignore_errors=True) then attempts to rename the old world back without confirming target cleanup succeeded; if cleanup failed, the rollback rename can fail and leave the previous world in its .pre-swap directory. Add a destination-parent symlink race test and injected rmtree-failure recovery test. Do not make a Rust archive helper assume it owns this transaction.
- _write_level_name() uses a fixed .crucible-tmp path, checks it for existence/symlink, then writes and replaces; there is no exclusive tempfile or file/directory fsync. The check/open sequence can race.
- prune_pre_swap_dirs() deletes candidates with shutil.rmtree(ignore_errors=True) but increments its deleted count even when removal failed. The helper does not acquire fleet_operation_lock, and the visible World-tab cleanup action invokes it directly rather than the checkpointed worker branch.
- reset_world() and wipe_world() accept caller-supplied safety_backup_verified and non-empty fleet_generation_id flags; they do not verify the ID against a generation manifest or check that the generation contains this instance. Production WorldTab supplies them only after the fleet checkpoint, under the lock, and rechecks stopped status. The booleans are not an authority proof for a direct caller.

Required legacy restore fixtures: raw-ID traversal, broad-permission root, symlink ZIP listing, partial-path symlink, oversized central directory, extreme compression ratio, fleet ZIP SHA mismatch, CRC/truncated stream, post-swap corruption, properties temp collision, rmtree failure count, and direct reset/wipe with fake generation ID, plus a nonexistent/wrong-instance generation ID. Do not change or migrate the legacy on-disk path without an explicit compatibility plan.

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
10. provider-response validation and local, allowlisted history publication (the public link is not independently fetched or re-downloaded).

The Gofile upload result includes the exact download URL/file identity/name/size, local SHA-256, optional remote MD5 and whether size/MD5 verification was present. The provider response is not trusted as an arbitrary URL: Crucible validates the host/path/HTTPS format and checks the returned IDs and metadata. A returned link is accepted from Gofile's successful response and shape-checked, but Crucible does not independently fetch the page or download the remote payload; missing remote size/MD5 are recorded as not verified rather than assumed to match.

The export contract is format-aware. The share/upload path currently builds Prism ZIPs only and uses whole-input fingerprints before cache/build, at finalization, and again immediately before network upload. General local export also supports MRPACK and CurseForge, but those formats do not currently compute that fingerprint; they still bind bundled JAR decisions to the exact bytes sealed in the ZIP and recheck the catalog revision. Do not assume the Prism share workflow's full input-stability guard automatically applies to every format.

The distribution catalog is exact-artifact SHA-256 evidence, not a general mod-name permission list. `unverified` artifacts are included with a warning and remain the owner's responsibility; `conditional` items carry conditions; `prohibited` items are excluded and need a reviewed alternative-download record or export fails. A catalog `permitted` status is limited to the reviewed hash and recorded terms/evidence. Never infer permission from a familiar filename, mod ID, project page, or matching version string.

Privacy coverage is intentionally bounded. Supported config/KubeJS text is scanned at <=8 MiB per file and <=256 MiB total; nested ZIP scan input is <=64 MiB; findings are capped at 50. Known binary assets and unsupported file types are not scanned, and oversized/nested unreadable items are counted as incomplete. The manifest records scanner scope, coverageComplete and warnings. `coverageComplete=false` does not currently make the archive build fail; it must remain a prominent review condition and be acknowledged before public upload. Even `coverageComplete=true` is only complete within the recorded scanner scope and is not a guarantee that the pack is secret-free.

Local share ZIPs use owner-only directory/file permissions, collision-free `O_EXCL` naming, a verified content-fingerprint/archive-hash cache, and a bounded atomic sidecar. Three explicit acknowledgements gate the public upload. The upload adapter opens the local file no-follow where supported, validates the file identity, hashes the same descriptor, streams it in bounded chunks while re-hashing, and checks the expected archive SHA and source fingerprint before the provider sees it. Cancellation before the final multipart boundary stops the request, but a partially received remote object may exist. After the final boundary, local cancellation cannot retract the upload. Only the optional owner API token is sent as a bearer header; it is not persisted.

Share history is local, not provider-side. It holds at most 50 allowlisted events in a <=1 MiB owner-private file. It stores server name, local path, archive checksum/fingerprint, size, provider, validated last-returned Gofile URL, upload status, optional verification flags and generic error category; it excludes credentials, headers, raw exceptions and response bodies. Clearing history does not delete local ZIPs or revoke remote links.

A Rust port must not collapse build/review/upload into one function or auto-upload as a side effect of export. Keep GUI review/consent, catalog interpretation, privacy scope, source fingerprinting, provider policy, network effects and history as separate contracts. A Rust helper can later receive a validated immutable archive/input inventory or implement an isolated ZIP/hash primitive; it must not become the implicit legal/distribution approver or publisher.


### 2.9.1 Network/provider boundary contracts

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

This has not been exercised as a live network exploit or changed by this PR. Before a Rust network client replaces any of these paths, add deterministic mock-redirect tests and a narrowly scoped Python hardening change: each download operation should validate every redirect target, require HTTPS after every hop, reject userinfo/unsafe ports, and preserve the host policy appropriate to that operation. Do not apply a rigid host allowlist to owner-supplied modpack URLs if legitimate hosts and CDN redirects are intentionally supported; use an HTTPS-only redirect policy there while retaining strict provider allowlists for icon and authenticated provider requests. Preserve finite redirect counts and ensure credentials are never forwarded across hosts.

Official references for the supported runtime matrix: [Python 3.11 urllib.request](https://docs.python.org/3.11/library/urllib.request.html) and [Python 3.13 urllib.request](https://docs.python.org/3.13/library/urllib.request.html).

**Do not conflate provider adapters.** Gofile upload uses `http.client.HTTPSConnection`, not urllib redirects; icon fetching has its own deliberately strict host policy; indexed JAR download uses `download_http`; serverloader and pack update have separate retry/redirect/size behavior. A Rust implementation may eventually share low-level TLS, bounded-body and redirect primitives, but its policy must be supplied per operation as explicit data. Keep provider approval, URL provenance, retry safety, cancellation semantics, and exact artifact checksum validation in the higher-level workflow.

### 2.10 CLI command and behavior contract

The current `argparse` interface registers 32 subcommands:

`gui`, `list`, `check-updates`, `update`, `rollback`, `prune-previous`, `probe-pack`, `add`, `remove`, `start`, `fix-properties`, `fix-loading`, `stop`, hidden `shortcut-action`, `restart`, `status`, `attach`, `send`, `scan`, `scan-prism`, `import-prism`, `create-server`, `install-modpack`, `list-mc-versions`, `install-server`, `download-mods`, `accept-eula`, `doctor`, `validate`, `backup-fleet`, `info`, and `edit`.

Behavioral contracts that must survive any future CLI migration:
- `argparse --help` is handled before registry loading. With no subcommand, Crucible prints banner/help and exits successfully.
- Normal commands print the banner except `gui` and the hidden `shortcut-action`.
- After parsing a valid command, `main()` loads `InstanceManager` and creates `TmuxManager` before dispatch, even for commands like `probe-pack` or `list-mc-versions`. Qt is only imported by the GUI handler.
- Start uses the same world-start inspection as the GUI. New/incomplete/changed/uncertain selection requires a TTY and exact typed `START`; non-interactive invocation cannot skip this gate.
- Stop is graceful by default. `--force` is explicit and means no world save; `--timeout` is configurable. Restart has its own graceful timeout.
- Update supports dry-run, target release/source, prerelease/downgrade policy, Java variant, rollback retention and explicit confirmation skip. Rollback can preserve the current world.
- Fleet backup supports mutually exclusive list, deep verify, install timer and remove timer modes; default action creates a full fleet generation.
- Hidden `shortcut-action` accepts only `start`/`stop` plus a stable instance ID and has separate graceful-only stop semantics.
- Handlers currently call `sys.exit(1)` and `sys.exit(2)` in distinct failure paths, print user-facing text directly, and often return `None` on success. These conventions are not centralized.

This interface must be tested as a CLI, not just by calling internal handler functions. Build a golden argv corpus that captures parsed options/defaults, stdout, stderr, exit code, TTY vs non-TTY behavior, environment/working-directory assumptions, and whether the invocation reads or mutates the registry, server files, tmux, network, or backup state. Do not replace the CLI until its command-by-command parity and installed launcher behavior are proven.

### 2.11 Installed launcher and release-artifact contract

The current release path publishes a source ZIP and its exact SHA-256 text asset. The one-line installer selects the latest release tag from GitHub's API, validates the tag and exact asset URLs, bounds both downloads, verifies the source archive checksum, rejects unsafe ZIP names/links/oversized expansion, verifies CRCs, and invokes install.sh.

The per-user installer:
- selects a supported Python 3.11+ interpreter, preferring one that can already import PyQt6; honours a supported CRUCIBLE_PYTHON override;
- creates a venv using --system-site-packages;
- stages the source tree separately from the venv and excludes build/VCS/cache files;
- compiles and smoke-tests the staged package, then atomically renames the old app aside and publishes the staged app;
- smoke-tests the final installed path before discarding the rollback copy;
- writes the command and desktop integration, while keeping server data/backups outside the application tree.

The launcher resolves its own symlink and prepends the copied source tree to PYTHONPATH before executing the venv's Python module. This is a native-extension integration constraint: a PyO3 wheel installed only into venv/site-packages may be shadowed by the source package. A future artifact design must explicitly place the native module where the imported package can load it, or change the install model to install the complete wheel without source shadowing.

 
**Installer data-preservation and privacy constraints**
- The installer currently creates `~/.local/share/crucible-backups` with ordinary `mkdir -p`; it does not set or verify mode 0700 on that parent. Legacy per-instance world ZIPs live directly beneath that parent, so their privacy depends on the user's umask and existing directory permissions. The fleet generation subdirectory applies separate permissions but does not secure sibling legacy ZIPs. Before writing new archives, enforce a private backup root and per-instance legacy directories, while refusing symlinked paths. Do not chmod unrelated parent directories.
- Upgrade migration from the old in-app `APP_HOME/backups` directory is currently `cp -a -n ... || true`. The copy result is ignored, and the old installation directory is later removed after publication. `-n` also skips colliding destination files without verifying byte equality. A failed/partial copy can therefore destroy the only copy of a legacy backup when OLD_APP is removed. This is a release blocker for any install-layout migration that moves user data. Replace it with per-file copy/compare/hash verification; only discard OLD_APP after every source backup is either proven identical at the destination or successfully copied and verified. Any failure must abort or retain OLD_APP for manual recovery. Never swallow a backup migration error.
- `tests/test_release_scripts.py` contains nine source/stub-focused checks. It does not execute a full fresh install/upgrade nor inject failures into legacy-backup migration or verify the backup-root mode. Add a temporary-HOME integration test with representative old in-tree ZIPs, a pre-existing identical/different destination collision, copy permission/disk-full failures, and an interrupted migration. Assert that the previous app/backup remains intact and the installer does not report a completed migration.
- The current installer's broad tar copy includes the `rust/` crate and fixtures in `$APP_HOME/source`, but does not compile or load them; this is source payload only, not an installed native artifact. The Rust workflow tests the crate but does not build a wheel/binary or publish any release asset. The recursive source tree currently has no release-publishing workflow; source ZIP/checksum publication is therefore an out-of-band release step relative to the checked-in Actions workflows. A production bridge needs a dedicated build matrix, verified artifact metadata, release publication and an installer-stage smoke test.
- The source ZIP SHA-256 downloaded beside the archive gives transport integrity against an accidental/corrupt transfer, but a checksum file published alongside the ZIP is not a signed release attestation or proof of a trusted build origin. Keep provenance/signing as a separate release-security decision.


No update may require the user to install Rust or build a native library. Release artifacts must be prebuilt for the supported platform/architecture/Python ABI, have explicit size and checksum expectations, be staged atomically, import successfully from the final path, and roll back to the previous installation if the native artifact is missing/incompatible. Test fresh install and upgrade/rollback separately. The existing installer smoke test is only --help plus import/version; a Rust extension needs a separate import-and-call self-test that has no tmux, filesystem mutation, network or Qt side effects. The upgrade smoke must also prove every old backup was preserved byte-for-byte before the previous installation is deleted.



The one-line downloader uses curl `--proto '=https'` for the API, source ZIP and checksum requests. This transfer allowlist also constrains redirect targets: the default redirect protocol set cannot override protocols excluded by `--proto`. Thus HTTP/FTP downgrade redirects are already blocked on these calls. Adding explicit `--proto-redir '=https'` would improve readability/auditability, but this is defense-in-depth rather than a confirmed current vulnerability. See the [curl command-line documentation](https://curl.se/docs/manpage.html).

### 2.12 Mod metadata and live mod-file effects

The JAR metadata parser is a candidate pure-data boundary; filesystem mutation is not.

**Metadata contract**
- A `ModEntry` contains the filename, enabled/disabled state, size, friendly name, mod ID, version, description, URL, explicit execution side and whether it is a nested/bundled entry.
- The only acceptable side values are `client`, `server`, `both`, and `unknown`. Missing, invalid, unsupported or ambiguous metadata remains `unknown`; do not infer `both` from absence.
- Preserve format-specific semantics for Fabric `fabric.mod.json`, Quilt `quilt.mod.json`, Forge/NeoForge TOML, legacy Forge `mcmod.info`, and `META-INF/MANIFEST.MF`. Current code reads only the first `mods` entry for several formats and uses only explicit side markers. Forge `displayTest`/dependency metadata is not an execution-side declaration.
- Current member reads are bounded to 4 MiB, but the Python path first calls `ZipFile.namelist()`; the full ZIP central directory remains unbounded by an entry-count or directory-byte cap. A first Rust parser should take already-selected bounded member bytes and return typed metadata, not own ZIP traversal or directory I/O.
- Nested JARs are informative bundled entries, not directly manageable mods. Keep that distinction separate from enabled/disabled state.

**Current effect routes and safety gaps**
- Enable/disable are path renames, delete is unlink, and manual Add JAR uses direct copy. Their stopped-state check lives in ModsTab GUI code; the file-operation methods do not independently revalidate process state or acquire a transaction lock.
- On Unix, `Path.rename()` replaces an existing file destination when permitted. Enable/disable must therefore be no-replace operations, not plain renames. An enabled and disabled copy of the same basename can coexist today; toggling one over the other can destroy the destination copy.
- `versions.replace_files()` is the stronger group-replacement path: it takes `.crucible/mod-change.lock` with `O_EXCL`, stages verified downloads, rechecks the original JAR hash, runs a caller-provided pre-commit guard, stores affected JARs and the old record in `.crucible/mod-history/<id>/`, writes a receipt, publishes replacements, and rolls back ordinary exceptions. An interrupted operation leaves its lock and history for manual recovery.
- This per-instance lock does not coordinate with `fleet_operation_lock()`; FleetBackupManager does not inspect `.crucible/mod-change.lock`. A fleet snapshot can overlap the multi-file replacement and capture an intermediate mod tree. The backup walks the server tree and includes `.crucible/`, so `mod-change.lock`, `mod-history/`, receipts and `added-mods.json` are snapshot content; a stale lock after interruption is not currently a blocker. Fleet backup does use file-stat/hash checks and a second inventory walk, which catch many concurrent changes, but it has no knowledge of this transaction boundary. `test_global_operation_lock_prevents_backup_during_world_or_pack_mutation` only verifies that backup refuses while the test explicitly acquires the outer lock; it does not verify any production mod caller acquires it. The other mutators and the indexed-pack download path do not acquire either lock.
- AddModDialog and Change Version pass a stopped-state callback before replacement commit. `_verify_deps()` instead calls `modrinth.install_files()` directly after dependency resolution; the long worker does not repeat the stopped check, stage the full group, or acquire the mod-change/fleet lock. `download_pack_mods()` is also called after only a one-time GUI stopped check; its worker can continue downloading and atomically publishing individual JARs after the server starts. Manual file Add and Delete similarly do not recheck at the final mutation boundary.
- `modrinth.install_files()` and `download_pack_mods()` are per-file operations, not multi-file transactions. They may leave a partial set after failure. The indexed downloader prefers SHA-512, then SHA-1; when neither is present it accepts a non-empty file. The declared size is an upper limit, not an exact-length assertion in that no-hash branch. Modrinth direct install checks SHA-1 only when supplied. Replacement via `replace_files()` is stricter and refuses missing SHA-1.
- The current Modrinth identity/version resolution must remain exact: compatible loader/Minecraft metadata is required, dependency graphs are checked for cycles and version conflicts, required dependencies are selected, and same-name different artifacts must not silently overwrite one another.

**Rust boundary**
- First candidate: a pure parser that accepts a bounded metadata member plus explicit format/version context and returns `ModMetadata` and `ExecutionSide`; preserve `Unknown` as a first-class value.
- Later candidate: a pure dependency-graph validator over an immutable list of exact project/version/file candidates. It should return a deterministic selected set or structured blocker; API lookup and download remain in Python.
- Do not move file rename/copy/delete, stopped-state probing, lock acquisition, backup/history publication, or network downloads into the first Rust core. Those belong behind a Python effect adapter until every caller has one transaction owner.
- Before production extraction, close the no-replace rename and global backup-lock gaps in Python. Every multi-file mutation that can coincide with a fleet snapshot must acquire the same OS-level fleet lock (or use a documented already-held-lock contract); FleetBackupManager must also treat an in-progress/stale `mod-change.lock` as a blocker unless a defined recoverable-snapshot contract is introduced. Every operation must revalidate a complete stopped/process observation immediately before publication. No in-process Rust mutex can substitute for that cross-process lock.

### 2.13 Crash diagnosis and mod quarantine/restore

Crash parsing and candidate identification are read-only; quarantine and restore are file mutations.

- Preserve the bounded text/JAR metadata limits, parser precedence and evidence types. A crash diagnosis can identify exact Mod file entries, mod IDs mapped from declared JAR metadata, or a fallback set of explicitly client-only Fabric JARs. Unknown/ambiguous JARs must not be quarantined solely by name similarity.
- The current CLI exposes fix-loading --apply and fix-loading --restore. Both mutate mods/ directly without a fresh stopped-state check or either mod/fleet operation lock. A Rust core may return a proposed set of exact paths/hashes but may not execute the rename.
- Quarantine renames files one by one to .jar.disabled, has no no-replace guard, and writes .crucible/quarantine.json only after the rename loop. The JSON update is best-effort and non-atomic; a failure or interruption can leave partial quarantines or lost provenance.
- Current restore behavior is broader than its CLI description: restore_quarantined() ignores the quarantine JSON and re-enables every top-level *.jar.disabled whose enabled destination does not exist. This can undo manual owner disables. A future safe restore contract must be receipt-driven: validate the quarantine log/transaction receipt, ensure the file still matches the recorded hash, and only restore entries owned by that quarantine operation.
- Quarantine/restore must coordinate with fleet_operation_lock() and per-instance mod mutation, require a complete stopped/process observation immediately before each transaction, refuse destination collisions instead of replacing, and preserve recovery receipts/preimage hashes. A partial failure must report which files changed and which remain pending; it must not claim an all-or-nothing fix.

The Rust extraction candidate is limited to a pure CrashDiagnosis parser and JarIdentity/offender matching over immutable log text and supplied metadata. Keep log discovery, ZIP access, locks, filesystem mutation, backup coordination and user confirmation in Python.

### 2.14 Registered-server file-effect boundary across CLI and Setup

The command line and GUI are both supported front ends; neither may be allowed to bypass the shared effect policy.

- The current CLI cmd_download_mods() writes indexed mod JARs without checking whether the server is stopped. ModsTab checks stopped once before opening the dialog; SetupTab's Download Mods action does not check at all. The actual worker only performs per-file temp/rename/hash handling and does not recheck process state or acquire a mod/fleet lock during the batch.
- cmd_install_server() and SetupTab._install_server() call install_server_loader() on an existing registered server root without an action-time stopped check or fleet-operation lock. Vendor installers can write launcher files/libraries while a registered Java server may be running; a GUI-local worker state only disables Start in that same UI and does not coordinate a second process. install_server_loader() also follows its target path: it does not reject a symlinked root or hold a no-follow directory handle, then downloads beneath target and runs Forge/NeoForge/Quilt installer subprocesses with cwd=target. The create_fresh_server() caller checks symlink once before invoking it, but that check does not protect the reusable existing-server repair API or a target replaced after the check. Installer subprocesses do not receive the cancellation event and can run until the 900-second timeout; failed installers can leave installer JARs and partial library/launcher files.
- cmd_fix_properties --apply and SetupTab's PropertiesFixDialog rewrite server.properties without a common stopped check or fleet lock. PropertiesFixDialog only tries a one-time .bak copy and ignores backup-copy OSError, then publishes through PropertiesDoc.save(). The ordinary Start preflight is a separate path and is not a substitute for explicit repair actions.
- cmd_accept_eula() and SetupTab._accept_eula() write eula.txt without a stopped check. Define whether this is allowed while running and give it one consistent effect path.
- WorldTab seed edit performs a stopped check and a configuration guard before prompting, then atomically replaces server.properties, but does not acquire fleet_operation_lock(). This is safer than the unguarded Setup repair, but not serialized with a fleet snapshot.
- CLI fix-loading quarantine/restore is governed by the separate 2.13 contract. Pack-index download and Modrinth dependency install share the mod-file effects in 2.12.

The desired interface is a shared Python-side effect adapter invoked by both CLI and UI. It should acquire the appropriate OS-level operation lock, take a fresh complete process observation at the point of commit, validate current source/destination state, stage/verify outputs and then publish with explicit no-replace/rollback semantics. The owner confirmation and UI/CLI presentation remain in Python. Rust only supplies pure decisions/data transformations behind this adapter. A GUI's modal dialog, prior status check, or QThread does not constitute a cross-process lease.

### 2.15 GTNH update, rollback, and previous-install retention

The three operations have different safety contracts and must remain separate in a typed migration.

- Update requires a confirmed plan, a complete verified fleet generation before download/stage, server/port checks before backup and again immediately before live-root rename, archive probing, same-filesystem extraction/staging, explicit preserve/carry decisions, a sibling previous-install tree and a receipt for later identification. Cancellation before swap must leave live unchanged; a failed second rename must put the previous tree back. The GUI and CLI update callers hold fleet_operation_lock across checkpoint and apply and pass an already-held flag to the nested backup.
- The plan is read-only but not pure: it reads properties, world directory inventory, recursive tree sizes and free disk. The reviewed preserve list, carry-mod set, source state and disk estimate can stale while confirmation is open; the apply path must revalidate any source content decision that matters before commit.
- Pack download writes a predictable .part sibling using a normal path open rather than an exclusive/no-follow temp descriptor. It needs a symlink/collision test and a safe tempfile/no-follow publication primitive. The download is HTTPS/size/Content-Length checked but not cryptographically pinned; ZIP structural validation is not publisher authenticity.
- Pack receipt creation is temp+replace but best-effort: OSError is swallowed after the server root has been swapped, so update can return ok without the provenance needed for exact future update planning. Define whether receipt publication is a commit requirement; if so, preserve previous tree and return an explicit recoverable partial state when receipt publication fails.
- Rollback rechecks tmux/port state but GUI and CLI callers do not hold fleet_operation_lock; rollback_update itself does not acquire it. It validates the previous tree's sibling path/name and mods directory but does not reject a symlink or verify a cryptographic/structural receipt proving it is a Crucible-created pre-update snapshot. A previous-install symlink can point outside the server's parent tree and pass is_dir()/mods checks.
- keep_current_world=True intentionally deletes a same-named world in the prior install before moving the current world into that path. Preserve the previous full fleet generation as the recovery point and report that the old world slot was replaced. A failure in the sequence needs a transaction journal, because current rollback recovery only restores the moved-aside root when the registered live path is absent.
- Previous-install pruning is separately destructive, currently has no fleet_operation_lock in either GUI or CLI call path and no lock in the helper. Its candidate list is re-enumerated after confirmation, so a new matching sibling can be deleted without appearing in the preview. Reject symlinks and pin exact candidates/inodes/hashes between review and execution.
- The server/port status recheck is a check-then-rename operation. Unless Start participates in the same process-operation lease, a separate launch can race between the final check and root rename. The port guard also does not prove no unassociated Java process is using the folder.

Do not port pack rollback/prune as simple Path rename/delete equivalents. The first possible Rust work is pure pack identity/version parsing and deterministic planning from immutable snapshots. The filesystem effect transaction remains Python-owned until update, rollback and prune all share a cross-process lock/preflight/journal contract and tests for the race windows above.

### 2.16 Server installation and modpack import stage semantics

Keep the fresh Modrinth GUI route, direct local `.mrpack` route, Prism import and registered-server repair as separate entry contracts.

- ModpackDialog's Modrinth route chooses a new leaf, checks it again in the worker, then calls `install_modpack_from_modrinth(target_must_not_exist=True)`. The installer performs an exclusive leaf mkdir after Modrinth resolution, downloads the `.mrpack` inside that target and delegates to the local `.mrpack` installer. The fresh leaf claim prevents a target created during resolution from being overwritten; it does not make the overall installation transactional or clean the claimed target on cancel/failure.
- CLI `install-modpack --mrpack` calls `install_modpack_from_mrpack()` directly. It calls `mkdir(exist_ok=True)`, with no strict no-collision/non-symlink claim, and may merge into an existing directory. Prism local archive import has its own target semantics and partial-folder contract. Do not represent these as one identical InstallTarget policy.
- The local `.mrpack` installer runs `install_server_loader()` first, then stages the index, downloads mods individually, applies overrides and writes support files. Loader install failure is intentionally non-fatal to later stages; at the end `ModpackInstallResult.ok` becomes true even when the nested loader result failed. Individual download failures and override omissions also need inspection. Define a typed stage outcome (loader, index, downloads, override members, support files, final readiness) and never collapse all of this into one bool.
- `apply_overrides()` processes `overrides/` before `server-overrides/`, caps member count/per-file/aggregate expansion but does not enforce the Prism compression ratio limit; it writes directly to destination and on any caught member error unlinks that destination. It can delete an existing file or an earlier successfully-applied general override when the server-specific replacement fails. No cancellation token is checked during the override loop. A Rust ZIP helper must not inherit this as a hidden success path; first fix/fixture the Python behavior and then preserve explicit per-member result records.
- `_safe_join()` resolves against the target root before opening a file; it catches an already-present symlink escape but does not make the resolve/open sequence race-free. A root/parent symlink inserted after validation can still redirect a path operation without a no-follow directory-handle strategy.
- `serverloader._download_to()` uses `mkstemp` plus `os.replace()` for the downloaded installer/library file, which is preferable to the pack-update fixed `.part` path. It still does not hash against a pinned expected digest, does not pin the target directory identity, and the vendor installer subprocess does not receive cancellation; the call may remain active until the 900-second timeout. Failed installs can leave downloaded installer JARs and partial launcher/libraries files. `create_fresh_server()` rejects its initial leaf symlink before using the helper, but `install_server_loader()` itself is shared with already-registered roots and has no such validation.
- Keep source trust, target ownership, and stage success explicit. A new Rust core can parse immutable `.mrpack` indexes and normalize member metadata, but Python must own the target claim, cancellation, installer subprocess, network policy, file publication and partial recovery.

### 2.17 Archive import and partial-result contracts

Crucible has at least three distinct archive/import paths. They must not be collapsed into one generic `extract_and_install()` operation or one boolean `ok` value.

| Path | Extraction/staging behavior | Current bounds and validation | Partial-result/ownership behavior |
|---|---|---|---|
| Prism/MultiMC archive (`prism.py`) | `_detect_archive()` calls `_extract_archive(path)` without `dest_parent`, so its temporary tree is under the system temp directory (commonly `/tmp`); later `import_prism_source()` copies files directly into the chosen target | 100,000 members, 20 GiB total expanded, 2 GiB/member, 1,000:1 ratio for files >100 MiB; skips archive symlinks and traversal paths; 1 MiB streaming | target is fresh and not registered until user confirms after successful import; a copy failure can leave partial target and extraction temp because cleanup isn't in `finally`; GUI explicitly tells user partial files may remain; Cancel after success also leaves files |
| GTNH pack update (`pack_update.py`) | probes central directory first, then calls `_extract_archive(arc, staging_parent)` so extraction shares the server filesystem; stages new pack, copies preserve list, validates, rechecks safety, renames live -> previous then staging -> live | server pack <=8 GiB download; shared Prism extraction limits including ratio check; server-pack probe requires mods and recognized launcher/script or notes unknown launcher; rejects client-only/index archives | mandatory verified fleet checkpoint before download/stage; failed staging before rename keeps live tree; rename failure rolls previous tree back; retains previous tree by default; whole workflow returns structured UpdateResult |
| Modrinth `.mrpack` (`modpack_auto.py`) | reads only index first; target is freshly claimed in strict GUI mode; loader/mod files/support files are written directly into target; no whole-target stage/swap | index <=16 MiB; override member <=2 GiB, total <=20 GiB, <=100,000 entries; symlinks/path traversal skipped; 1 MiB stream; `server-overrides/` is applied after `overrides/`; no 1,000:1 compression-ratio check; write-error cleanup unlinks destination instead of restoring old bytes | per-file override errors are logged and skipped; no structured omitted-file list; failed writes can delete a pre-existing file or the successful general override in the second pass; may still return `ok=True` when some overrides failed or loader installation failed; cancellation/failure can leave partially populated target; strict mode atomically claims target after remote resolution; CLI/direct non-strict path may reuse existing target |

The shared Prism extractor has a `dest_parent` parameter specifically to avoid extracting multi-GiB archives into tmpfs `/tmp` and to enable same-filesystem rename. The pack-update caller uses it; Prism archive detection currently does not. This discrepancy is source-confirmed and its user-visible runtime impact depends on the system temp mount and archive size. A separate Python performance fix could pass the actual import destination parent into detection/extraction, but it needs a clear ownership decision (the target is chosen after some archive metadata detection today) and cleanup tests.

`import_prism_source()` uses a `PrismImportPlan.cleanup_dir` that it removes on normal completion, not in a `finally`; an unexpected I/O error can strand the temporary extraction directory. Its final target is not staged atomically. The GUI doesn't register a failed import, but it leaves the copied folder on disk by design. By contrast `.mrpack` override application swallows per-file failures and the top-level result can still be `ok=True`; `create_fresh_server()` writes support files even when loader installation fails, while its top-level `ok` follows `install.ok`. These result semantics must remain separate in a typed Rust model, e.g. stage outcomes for target claim, loader install, each downloaded mod, each override, support files, cleanup and readiness. Do not report a pack as fully ready just because a workflow returned `ok`.

Before implementing a Rust archive primitive, generate identical hostile/large test fixtures for each route: default temp root vs destination-parent temp root; corrupted ZIP/CRC; duplicate normalized destinations; absolute/traversal paths; symlink/special-file members; excessive member count/expanded bytes/per-file size/compression ratio; truncated stream; write failure mid-extract; cancellation before and during target claim; stale source mutation; failed loader with support files left; individual override failure; server-overrides precedence; incomplete cleanup. Keep local installed-Prism folder copy distinct from ZIP extraction: `_copy_tree()` has no aggregate archive cap and is intended to copy large local server directories.


### 2.18 Player permissions, player history, and avatar boundaries

Player data includes both Minecraft-owned files and Crucible-owned presentation state. Keep those distinct.

- `whitelist.json`, `ops.json`, and `banned-players.json` are server-owned lists. The UI reads them with a 16 MiB cap, but the current reader follows symlinks and does not validate root/list/entry/UUID/name types. Add uses live console commands after a single managed-session probe; Remove edits the JSON file directly with a fixed `.tmp` path. Removal does not recheck server state or acquire the fleet lock and does not update the running PlayerList in-memory state. Future policy: use `whitelist remove`, `deop`, or `pardon` through a confirmed managed console while running; only use a file transaction when the server is confirmed stopped and the operation is serialized with fleet backup. A Rust parser may validate a supplied list snapshot, but it must not own the live edit. The current `_remove_player()` mutates `_data` before `_save()` and the save method does not catch publication errors; add a no-loss failure test so a displayed removal cannot look committed when the file remains unchanged.
- Player stats lookup trusts a `uuid` string from local JSON and interpolates it into a world stats path. The read boundary must validate the UUID format and keep the resolved target inside the selected world's `stats/` directory; malformed metadata must not be treated as a valid path. It currently checks `exists()` and then uses the candidate path with a later bounded JSON read; a symlinked stats file is followed. Validate accepted UUID forms, reject symlink components, and open no-follow after verifying the resolved stats directory remains beneath the selected world. An absolute UUID string can reset the path join and `../` components can escape `world/stats`; the UUID and player-name fields originate in local server JSON and are not trusted merely because the file is local. Validate their types and accepted Minecraft UUID forms before comparison/path construction. At final open, use a no-follow descriptor and verify the opened file is beneath the selected world's `stats/` directory; string checks plus `exists()` are not race-safe.
- `PlayersTab.load()` reads/parses `players_seen.json` plus `whitelist.json`, `ops.json`, and `banned-players.json` synchronously on the Qt thread. Each file has its own 16 MiB cap, so the combined input budget is up to 64 MiB before Python objects and table widgets. `_PlayerListWidget._refresh_table()` creates a name item, UUID item, and Remove button for every permission entry; there is no aggregate row limit or virtualized view. `_refresh_online_list()` sorts the full offline history set and then shows only 12 names. For large valid lists, this can delay the first Players-page paint. Keep the full owner-managed data intact, but benchmark worker parsing and a virtualized/paged view; any cap must be explicit, and stale worker results must be rejected by instance generation. `json.loads()` already runs in native code, so a Rust JSON parser is not the likely speed win.
- `.crucible/players_seen.json` is Crucible-owned UI history, not authoritative Minecraft permission state. It stores timestamps/session count/tracked playtime by player name. It has a 16 MiB read limit but no versioned schema or name/type bounds, follows symlinks, and writes through a fixed non-fsynced `.tmp` path while swallowing failures. Define a bounded versioned sidecar and distinguish corrupt/unavailable from empty. This distinction is safety-critical for retention: an oversized file (>16 MiB) currently raises during load, is caught as an empty `_seen` map, and the next join/leave event replaces the old file with only the new in-memory records. Treat `Loaded`, `Missing`, and `Unavailable` as separate outcomes and forbid writes from `Unavailable` until an explicit owner recovery/reset decision. `_on_server_stopped()` currently saves once per online player because each loop iteration calls `_record_seen()`; the future state update must batch all leaves and publish once. This sidecar is safe to reset only with an explicit owner action. `_record_seen()` is called by join/leave slots and synchronously serializes/writes the entire sidecar on the Qt event thread for every event; history is not pruned or count-bounded, so write cost grows with record count and event bursts can delay UI handling. Benchmark history sizes and join/leave bursts before choosing a Python debounce/queue. A coalesced writer must retain a dirty generation until successful publication and flush on server stop/close; a Rust serializer would not remove the UI-thread disk I/O by itself.
- The current avatar path is presentation-only: one QThread per unique join, Minotar HTTPS URL, 8-second timeout, 2 MiB response limit, seven-day cache and stale-cache fallback. It uses default urllib redirects, has no decoded image dimension/pixel cap, writes through a fixed `.partial` file and has no concurrency queue. A burst of unique joins can create a burst of worker threads/requests. A future change should validate every redirect as HTTPS, set a strict image dimension/pixel budget before full decode, use exclusive no-follow temp publication, and bound concurrent work. Do not make avatars a Rust core concern. The 2 MiB limit is on encoded response bytes only: `QImage.fromData()` is called without a pre-decode pixel bound, cached files can be read via unbounded `read_bytes()`, and `_avatar_for()` loads cached paths with `QPixmap` without reapplying the response cap. A second path boundary exists on cache reads: persisted `players_seen.json` keys are not constrained to the LogWatcher `\w+` player-name grammar, but `_refresh_online_list()` feeds offline keys into `_avatar_for(name)`, which interpolates the raw name into `_AVATAR_CACHE_DIR / f"{name}.png"`. Absolute/traversal names can therefore point the image loader outside the avatar cache. Validate persisted names before display/cache lookup and use a safe encoded or hashed filename under the fixed cache root, with no-follow reads and containment checks. This is independent of the URL/network path. There is also a console-input path: offline keys from the same history are selectable in `_show_player_menu()`, whose Make operator/Remove operator/Pardon callbacks build commands with the raw name and call `TmuxManager.send_command_result()`. That method uses `tmux send-keys -l` and then a separate Enter, but does not reject embedded control characters. `-l` disables key-name interpretation; it does not remove line-feed/carriage-return characters from the string. tmux's `cmd-send-keys.c` injects each decoded UTF-8 code point, and `input-keys.c` writes literal key bytes directly to the pane PTY, so LF (`0x0a`) is sent as a console line terminator. Source: https://github.com/tmux/tmux/blob/master/cmd-send-keys.c and https://github.com/tmux/tmux/blob/master/input-keys.c. Therefore a name like `guest\nstop` can split the generated `op guest\nstop` input into two Minecraft console lines and execute `stop` on the trailing Enter. This is a local metadata-to-console command injection path; it does not imply remote network code execution. Required shared fix: reject C0 and DEL controls (at least LF, CR, NUL, ESC) in `send_command_result()` before invoking tmux, and validate history-name keys before exposing player actions. Keep literal `-l` for printable content so shell metacharacters remain uninterpreted. Regression tests must prove no tmux subprocess is called for CR/LF/control-bearing commands and that malicious offline history keys cannot issue a command. Treat decoded dimensions/pixel count and cache size as separate budgets; Qt already provides native image decoding, so moving this path to Rust does not automatically improve performance or memory.
- Existing avatar Qt tests cover success/failure/stale cache, thread retention during close, stale-generation suppression and placeholders. They do not cover hostile redirect targets, over-dimensioned PNGs, cache partial symlink/collision, duplicate worker concurrency, or player-list/history file contracts.

The current UI test tree has `test_player_avatar_states_qt.py` but no dedicated player-list/history persistence suite and no dedicated ConfigTab/NotesTab save-contract suite. Add tests for wrong-shaped whitelist/ops/banned JSON, removal while the server is running, file-write failure after the UI mutates its in-memory list, temp symlink/collision, recently-played history at the 16 MiB boundary; malformed/permission-denied load must not become empty; no overwrite after `Unavailable`; one save for 1/10/50/100 server-stop leaves; join/leave coalescing; dirty-state retention after save failure, and UUID path containment, absolute/traversal UUID and history-key-to-avatar-cache path containment; history-key CR/LF/C0 console-line injection; shared send_command_result rejects C0/DEL before tmux. Add a combined four-file 64 MiB boundary test, large valid list/history first-paint benchmark, and assertion that virtualized/paged rendering does not truncate the underlying server-owned list during writes. Do not infer coverage from the avatar visual-state suite; it does not exercise these file contracts.

### 2.19 Config and notes tab persistence

- ConfigTab is an independent editor parser: literal `=` only, comments/blank lines skipped, last duplicate value stored, and every duplicate key rewritten to that final value at save. It normalizes matching lines to `key=value` and LF/final newline; it is not the same implementation as PropertiesDoc or pack-update's parser. Golden fixtures should preserve this exact current behavior or an explicit migration should be approved.
- ConfigTab save writes a fixed `.tmp` file, then replaces `server.properties`; no backup, fsync, exclusive/no-follow creation, fleet operation lock, or stopped-process check at publication. It can race a running server and a fleet snapshot. Dangerous-key confirmation is a human warning, not a process lease.
- Notes are stored in the registry record, not a per-instance text file. The UI debounces 500 ms and calls `InstanceManager.update_instance()`. On a registry write failure it displays an error but unconditionally clears the dirty flag; the latest edit is not retried on switch/close. The save API should return a committed/not-committed result and only clear dirty after commit. Tests should inject registry lock/external-edit/disk failures and assert the text remains dirty and recoverable. Each save serializes the entire registry snapshot (all instances and unknown per-record metadata), writes the fixed instances.tmp sibling, fsyncs the temp file and replaces instances.json. The 500 ms debounce limits typing bursts within one NotesTab but does not make this a field-level update; two manager processes still race on the same registry file/temp with stale snapshots. Benchmark total registry bytes and fsync cost before changing the format. Add a two-InstanceManager test with barriers that forces both writers past the signature check before publication and proves one update cannot silently overwrite the other; also cover a pre-existing instances.tmp symlink and parent-directory fsync failure.
- These tab actions are presentation/persistence concerns, not early Rust performance candidates. Keep Qt debounce, status and retry behavior in Python; any future Rust registry record serializer must preserve the same unknown-field/version contract already documented above.

### 2.20 Rust filesystem and ZIP dependency constraints

The initial Rust crate is dependency-free with MSRV 1.85. Do not change that simply to gain a convenience archive API or a high-level rename helper.

- Current `zip` crate 9.4.1 declares Rust 1.88, above the pilot MSRV 1.85. Its default features enable AES, BZip2, Deflate/Zopfli, Deflate64, LZMA, PPMd, time, XZ and Zstandard. If later considered, pin a deliberate version, decide whether MSRV rises, use `default-features = false` and enable only algorithms verified against real supported archives. Recheck the dependency's RustSec advisories at the time of selection.
- RustSec RUSTSEC-2025-0168 (CVE-2025-29787) affects the zip crate extraction helpers in releases >=1.3.0 and <2.3.0 because archive symlinks could redirect subsequent extracted files. The advisory says 2.3.0+ is patched. Never use an affected version, and keep Crucible's path/member/size/ratio/special-file validation even when the library offers an extraction helper.
- Do not use a central-directory entry name directly as a destination path. The Rust core must receive or produce validated relative components; the Python effect adapter still owns root containment, no-follow destination opens, staging, cancellation, exact-byte/CRC validation and atomic publication.
- Stable `std::fs::rename()` replaces an existing destination file on Unix. It is therefore not a safe implementation of Crucible's no-replace JAR toggle, import member, sidecar or target-claim operations. `std::fs::rename_noreplace()` is nightly-only as of 2026-11-06, documented in the Rust 1.101.0-nightly standard library. A check-then-rename fallback is raceable. Keep publication in Python for the first Rust phases; a future platform adapter must use an audited atomic no-replace primitive or fail closed on unsupported filesystems.
- A safe ZIP contract must preserve per-route policies rather than one global limit. Prism ZIP extraction currently caps 100,000 entries / 20 GiB aggregate / 2 GiB member / 1,000:1 ratio for large members; `.mrpack` override extraction caps 100,000 / 20 GiB / 2 GiB but has no 1,000:1 ratio check; legacy world ZIP restore caps 2,000,000 entries and available disk reserve but has no fixed ratio/per-member cap; client export and fleet restore have their own path/hash/privacy checks. Capture exact existing behavior first, then make safety improvements as explicit Python changes with fixture updates.
- ZIP parsing/decompression and large directory inventories must run off the Qt main thread, with bounded memory and cancellation. The current Python extractors use 1 MiB member streaming; Rust must not decode entire multi-GiB members or central directories into memory. Compare p50/p95 time, CPU, peak RSS, decompressed/written bytes, entry count and cancellation latency before accepting a new crate.
### 2.21 Qt table model, worker, and presentation boundary

The Rust core must not own Qt models or presentation. The large UI lists should be treated as Python model/view concerns:
- Mods and Players currently use `QTableWidget`/`QListWidget`, including one child Remove button per permission row and one state button per manageable mod row. The view duplicates the underlying `ModEntry`/player records as cell items and child widgets. Qt's own docs describe these convenience classes as less flexible than the model/view classes and recommend model/view unless item-based widgets are specifically required.
- A likely future UI seam is `QTableView` + `QAbstractTableModel`, with a delegate/accessible action for per-row control and a proxy model for sorting/filtering. Keep the underlying complete records authoritative; visual paging/`fetchMore()` must not omit records from saves or make hidden rows unmanageable.
- Background workers may do filesystem scans, bounded JSON parsing, JAR metadata inspection, and build immutable row snapshots. The model and any attached view remain GUI-thread objects: worker threads must not call model APIs directly. Deliver one generation-tagged immutable batch by queued signal, then mutate/reset/insert rows in the GUI-thread slot with correct model notifications.
- Do not use a Rust JSON parser to try to speed up `json.loads()`; the current JSON decoder is already native, while Qt item/widget creation, synchronous filesystem walks, full-history sorting, and per-result UI updates are more likely costs. Benchmark first.

Parity fixtures must cover row data/roles, sorting/filtering, selected identity through re-sort, keyboard/accessibility actions, stable IDs for duplicate display names, stale instance generation, add/remove without losing unseen server-owned records, worker cancellation/close, and one batch update versus N per-row signals.

Qt references: [Model/View Programming](https://doc.qt.io/qt-6/model-view-programming.html), [Model/View Tutorial](https://doc.qt.io/qt-6/modelview.html), [QAbstractItemModel and thread-safety](https://doc.qt.io/qt-6/qabstractitemmodel.html), [Fetch More example](https://doc.qt.io/qt-6/qtwidgets-itemviews-fetchmore-example.html).

## 3. Recommended first Rust core types and functions

Do not begin by mirroring all Python classes one-for-one. The first Rust crate should model a small number of explicit, versioned values:

| Proposed core value | Input boundary | Pure result |
|---|---|---|
| `InstanceRecord` | Sanitized registry row / golden JSON | Validated record plus preserved unknown fields |
| `ObservedServerState` | Caller-supplied tmux/process observation | Explicit status including Unknown reason |
| `LifecycleDecision` | Instance ID + observed state + action intent | Allowed / requires confirmation / blocked with stable reason code |
| `WorldInventory` | Filesystem adapter's bounded scan result | World start state / confirmation requirement |
| `PropertySnapshot` | UTF-8 text + target Minecraft version | Parsed ordered properties, validation issues and proposed fixes |
| `PackUpdateSnapshot` | Pack identity, target release, `ProcessScanResult`, complete `WorldPreserveInventory`, complete/capped `TreeSizeEstimate`, loader evidence and free-space observation | Reviewable update plan/blockers that cannot claim preservation from incomplete inventory |

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
| Instance ID path consumers | Safe non-UUID historic IDs; heartbeat raw path traversal; legacy backup raw path traversal; hashed fleet/icon stores; invalid ID type; missing ID; explicit old->new mapping across all stores |
| Registry | Version 1; missing optional keys; unknown per-record/root fields; missing/unknown/unsupported schema version; missing ID re-generated per load; invalid field types; unsafe ID/session; duplicate ID/path/session; malformed/oversized registry; symlinked file/config dir; fixed-temp symlink; two managers racing add/remove/update; same inode/size/mtime but changed bytes; directory-fsync failure; no lost update |
| Status/process scan | Running; stopped; unmanaged Java; missing folder; tmux missing; tmux timeout/unknown; marker-only process; shell wrapper plus Java child; complete empty scan; inaccessible `/proc`; absent `/proc`; transient GUI starting/stopping kept separately; tmux missing plus unmanaged Java; launch attempted with unmanaged PID; launch attempted with incomplete scan; watchdog process-call counts and slow per-instance tmux/pane probes |
| Heartbeat/watchdog | Each of four states; missing boot ID; changed boot ID; interrupted stop same/different boot; absent, present and unknown tmux results; wrapper alive/Java gone; two misses; restart delay; crash-loop cap; stable uptime reset; 1/5/20/50 server tmux subprocess count and timeout latency; raw-ID traversal; symlink/oversized/invalid heartbeat; directory-fsync failure; interleaved two-process running/stop/handled transitions |
| World summary | Existing developed save; new folder; incomplete folder with `level.dat`; alternate developed save; changed selection; absent prior marker; path traversal; absolute path; symlink root/child; unreadable selected file; unreadable alternate save; missing/unreadable/symlinked/broken `server.properties`; exact `level-name=`; whitespace/colon/escaped key spellings; valid default and custom `level-name`; large file-count traversal |
| Properties | GTNH 1.7.10 legacy keys; modern 1.20+ keys; removed 1.21.2 keys; duplicate keys; escaped colon in level-type; malformed number/bool/enum; unknown key; comments/order/whitespace normalization; colon/whitespace separators and continuations as explicit unsupported/current-parser divergence cases; missing vs unreadable file; conflicting GUI/CLI/world/port/update readers |
| GTNH version parser | Leading v/V; two- or three-component versions; alpha/beta/rc spellings and separators; absent prerelease number; leading zeroes; huge decimal components; stable/prerelease ordering; malformed suffixes; empty and whitespace input; Python Unicode digits versus Rust ASCII-only boundary |
| Pack update | Unknown/running/stopped; unmanaged JVM; incomplete process scan; stable update; downgrade blocker; beta warning; unsupported Java; multiple worlds; unreadable root/alternate world; incomplete size estimate; inventory changed after plan; unsafe `level-name`; insufficient free space |
| GTNH name parser | Full path and basename strings; casing; allowed single GT/New/Horizons separators; required separators; Java 8/17 variants; bad/missing Java token; trailing extension/suffix; prerelease version spelling; first syntactic match precedence; Unicode decimal digit divergence; non-GTNH/A.F. name |
| Mod metadata | Fabric/Quilt/Forge/NeoForge/legacy metadata; malformed JSON/TOML; multiple declared mods; oversized metadata; unsupported format; explicit client/server/both; absent side remains unknown; huge central directory |
| Crash diagnosis/quarantine | Authoritative invalid-dist signature; named JAR/mod ID; exact metadata mapping; fallback explicit client-only scan; unknown culprit; large central directory; no-apply preview; conflicting enabled/disabled name; manually disabled file; receipt-owned quarantine; log corruption/symlink/write failure; interrupted partial quarantine; server running or unknown; backup lock contention; restore only verified receipt entries |
| Fleet backup | Complete generation; prior generation; reused file; changed file; legacy archive; checksum mismatch; missing/symlinked path; retention and publish failure; huge manifest; partial publish/fsync failure; old backup-root/ID compatibility |
| Legacy world ZIP | Raw-ID path component; legacy folder permission; ZIP symlink; slot sidecar corruption/symlink; fixed .partial collision; destination-parent symlink race during extraction; partial target cleanup failure/rollback; count/ratio/free-space boundaries; fleet archive SHA mismatch; CRC/truncation; rollback and reset/wipe caller proof |
| Client export | Exact JAR hash; unknown/manual JAR; conflicting distribution decision; source mutates after review; scan incomplete; duplicate/unsafe ZIP member; manifest mismatch |
| Provider/download | Same-host HTTPS redirect; HTTPS cross-host CDN redirect where permitted; HTTP/FTP downgrade redirect blocked; userinfo/unsafe port; oversized body; timeout; 401/403/429/5xx; missing/invalid digest; wrong digest; credential redaction; cancelled transfer; provider-disabled path before key/network |
| Mod-file effects | Enabled/disabled same-name collision; no-replace rename; direct add/delete; exact duplicate vs different hash; nested/bundled guard; interrupted `mod-change.lock`; concurrent replace; fleet-backup overlap; server starts during dependency/download worker; process scan incomplete at commit; no-hash/short-size download; partial group failure and rollback after each publication |
| Registered-server effect adapters | CLI and Setup/Mods tabs share the same stopped/process lease; server starts during transfer; fleet backup contention; install-server while live; target root replaced with symlink after registration; installer cancellation after subprocess starts; failed installer leaves partial files; properties repair while live; EULA write policy; seed edit while backup; publication no-replace and rollback |
| Player/config/notes state | Permission-list add by live command vs remove by disk; server running during remove; list JSON wrong root/schema; UUID path traversal; players-seen corrupt/oversized/symlink/temp collision; >16 MiB load followed by event must not erase history; batched server-stop persistence; hostile player-name history keys targeting avatar cache and injecting console line breaks; avatar redirect downgrade and oversized decoded PNG; avatar burst concurrency; ConfigTab duplicate key normalization and temp symlink; notes registry write failure must remain dirty |
| Modpack/import stages | strict Modrinth target claim and target-creation race; direct local mrpack into existing folder; target/parent symlink race; loader failure but top-level ok; per-file download partial result; override existing-file preservation; server-overrides second-pass failure; compression bomb ratio; cancellation during override/vendor process; target cleanup/recovery after failure; final readiness distinct from archive placement |
| Archive import | Prism temp-dir location; archive failure cleanup; duplicate normalized member paths; partial target contract; `.mrpack` missing/failed overrides; failed-write preservation of previous bytes; ratio policy differences; server-overrides precedence; target claim race; loader partial outcome; local folder copy with symlinks |
| Share | Valid Gofile result; unsafe host/path; malformed optional MD5/size; mismatched size/MD5; source file replacement attempt; same-descriptor hash/stream mutation; cancellation before/during/final multipart boundary; source fingerprint stale; catalog changed after build; cache sidecar/archive mode and payload tamper; coverageComplete false; three approvals reset after source change; retry after ambiguous partial remote upload; local history schema/permissions/redaction |
| CLI | Each of 32 subcommands; short/long options, defaults, hidden shortcut action, TTY and non-TTY Start confirmation, exit codes, stdout/stderr, registry/filesystem/tmux/network effects | Golden argv/output/result corpus for Python/Rust parity and installed-launcher smoke tests |
| Archive/file publication | Stable `std::fs::rename` overwrite semantics; nightly-only `rename_noreplace`; target exists after preview; source/destination symlink swaps; parent directory replacement; ZIP central-directory size/entry caps; per-route archive limits; zip-crate MSRV/features/advisory |
| Installer/release | Exact source/checksum asset pairing; ZIP path/link/size/CRC validation; Python selection; PyQt discovery; source shadowing; staging, rollback and desktop integration | Generated release fixture with a tiny fake native module, corrupted/missing artifact, ABI mismatch, final-path import and rollback verification |

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
- record the full set of accepted `ServerInstance` fields and version normalization behavior; add two-process registry writer tests (lost updates, fixed-temp collision/symlink, unknown root/version behavior) and NotesTab dirty-state retention tests;
- document every backup manifest field and source-stat signature;
- trace provider response variants and retry policy per network path;
- record exact CLI arguments, output, exit codes and working-directory/environment assumptions for any command considered for migration.

No runtime benchmark or parity test is claimed by this document. It is a contract map and test plan, not proof of equivalence.
