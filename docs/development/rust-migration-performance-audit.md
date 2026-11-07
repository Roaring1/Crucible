# Crucible Performance Audit for a Rust Migration

**Status:** Discovery only; no implementation or performance claims from benchmarks  
**Date:** 2026-11-06  
**Baseline:** `main` at `0eb89f2517fc3c4ba9417c2bc97e6c63b5f80b8c` as recorded in the migration assessment  
**Purpose:** Identify where performance work is likely to matter, distinguish measured facts from hypotheses, and define a benchmark-first order for any Rust work.

## 1. Performance objective

The goal is the fastest and most responsive Crucible in real use, without increasing idle resource use or compromising server/world safety. "Rust is faster" is not a performance target.

Evaluate at least these dimensions independently:

1. **Cold startup:** time from launching `crucible` to a usable window, including Python import time, Qt startup, registry load, and first view.
2. **Idle footprint:** steady-state RSS, CPU wakeups, subprocess count, open file descriptors, and background I/O with the window open but no active operation.
3. **UI responsiveness:** event-loop stalls and p50/p95/p99 action-to-feedback latency while switching servers/tabs, opening Console, and using the tray.
4. **Lifecycle observation:** status refresh duration and process/file reads as registered server count and unrelated host process count grow.
5. **Backup:** full and incremental elapsed time, bytes read/written, CPU time, peak RSS, fsync count, file count, and retained disk usage.
6. **Import/export:** archive build/extract throughput, hashing time, memory peak, and responsiveness for large modpacks.
7. **Resource reporting:** cost and freshness of procfs and optional GPU sampling.

Do not combine these into one synthetic benchmark score. The best result may be a faster hot path with no change to startup or idle behavior.

## 2. What the source currently does

### 2.1 Fleet backups already have incremental storage

The main implementation is `crucible/data/fleet_backup.py` (4,238 lines at the audited baseline), not a small wrapper. Its current design includes:

- A global operation lock and a separate backup lock to prevent overlap with world/pack mutations and concurrent publishers.
- A staging generation, canonical JSON manifest, manifest SHA-256, verification, atomic rename/publication, directory fsync, and immutable generation permissions.
- A fixed two-generation retention policy that refuses to rotate invalid existing restore points.
- A deterministic server-tree walk that excludes transient paths and records why content was excluded.
- No-follow handling for symlinks and special filesystem objects.
- A 1 MiB streaming copy/hash loop using SHA-256 and a temporary file, with source file identity/size checks before and after copying.
- Incremental reuse from the previous immutable generation when source signatures match, using hard links where possible and a verified copy fallback when hard links are unavailable.
- A second inventory walk and final source-stat verification to detect structural or content changes during capture.
- Running-server consistency through readiness checks, `save-off`, `save-all`, command acknowledgement, and `save-on` restoration. Already-disabled autosave is preserved.
- Legacy archive/source migration with delayed retirement until the required retained generations contain verified copies.

This is already a sophisticated design. Replacing it wholesale would be a high-risk rewrite and could lose incremental performance, snapshot consistency, or rollback guarantees.

### 2.2 Backup path has a plausible redundant read cost

For a newly copied server file, `_copy_and_hash_live()` reads the source once, hashes the bytes while writing a temporary destination, flushes and fsyncs the destination, checks source identity/size before and after, and publishes by rename. Then `_copy_server_tree()` calls `_hash_file(destination)` to read the new copy again and compare its digest. The legacy archive copy path performs a similar destination readback.

The legacy-archive path `_snapshot_archive_file()` also performs a post-copy destination hash. This can add a complete additional read of newly copied data. For a large world or pack, the extra read can be material.

This is **not yet classified as waste**. The readback may be an intentional assurance that the staged destination contains the expected bytes after write/flush. Before changing it, measure the time and determine whether the assurance can be preserved with a lower-cost method (for example, validating destination metadata and retaining a deep-verification mode, or using platform-specific copy/verification facilities with explicit durability semantics). Never simply remove integrity checks to make a benchmark look better.

There are other possible I/O costs:
- A flush/fsync for each file can be expensive for trees containing many small files.
- Full inventory traversal occurs before and after the copy.
- Final per-file stat verification is another metadata pass.
- `_seal_generation()` walks the staged tree bottom-up, sets files/directories read-only, and fsyncs every directory so published names are durable.
- Manifest JSON grows with the number of files and exclusions.
- Legacy archive validation may read old immutable archive bytes again to verify them before reuse.

Benchmark these separately. A many-small-files server and a few-large-files world stress different bottlenecks.

### 2.3 Incremental reuse is an important optimization to preserve

For normal server files, prior snapshot entries are reused when source stat signatures match and the prior immutable file is present with the expected size and immutable permissions. Crucible then hard-links the old snapshot into the new generation. If the source signature changed, Crucible copies and hashes the live file; if the bytes match the old digest, it can replace the staged copy with a link to the old immutable inode.

Consequences:
- An incremental backup can avoid copying unchanged multi-gigabyte world data.
- Hard-link reuse saves physical storage, but logical bytes in manifests still describe the full restore point.
- Cross-filesystem or unsupported hard-link behavior falls back to copying and hashing.
- The Rust port must preserve the same immutable-generation assumptions and fallback semantics.
- Benchmark the common unchanged-generation path separately from first/full capture and changed-world capture.

### 2.4 Idle health checks are already partly consolidated

`crucible/ui/main_window.py` uses one persistent Qt health worker and a 5-second timer. The worker calls `TmuxManager.status_map()` once for the whole instance list. That status map obtains the tmux session list in one subprocess call rather than launching one tmux process per server.

However, for each instance with no matching managed session, `status_map()` calls `_unmanaged_pids(instance)`. That delegates to `ResourceSampler.find_instance_pids()`, which iterates all numeric entries in `/proc` and reads process command lines, executable names, and sometimes cwd for that instance.

This creates a potential scaling cost: with many stopped instances, the same host process table may be scanned repeatedly during a single health refresh. Complexity is approximately proportional to stopped-instance count multiplied by host process count, plus procfs reads. This is a concrete optimization candidate, but its real impact depends on the owner's number of instances and host process population.

There is an important existing building block: `crucible/process/machine_resources.py::_instance_processes(instances)` already scans `/proc` once for all registered instances, but returns only an aggregate PID list for resource reporting. The health path in `tmux_manager.py` still calls the per-instance `ResourceSampler.find_instance_pids()` helper. A promising refactor is to share one matcher that returns per-instance PID associations, then use it in the health status map and resource sampler. This would remove repeated enumeration without introducing Rust or changing the safety model. Preserve the exact current semantics in the first parity pass: an exact marker token is accepted without checking `/proc/<pid>/comm`; only the fallback path/cwd match requires `comm == java`. The marker behavior needs a wrapper-process fixture because `TmuxManager.start()` embeds the marker inside a `bash -c` command string. Also preserve incomplete-scan semantics: the current best-effort helper collapses some `/proc` failures into an empty list, which is not valid proof of absence for destructive preflight. Do not cache stale results across lifecycle actions; safety-critical actions must still revalidate immediately.



### 2.5 Watchdog polling is also per-instance today

The watchdog runs on a demand-driven 10-second QTimer and stops its timer when no instance is watched and no external Stop is pending. That idle behavior is good and should be preserved.

There is a second, separate query-scaling issue while servers are being watched:

- `Watchdog._poll()` loops over the watched instances.
- For each normal `running` heartbeat it calls `TmuxManager.probe_running(instance)`, which launches `tmux has-session -t =session`.
- If the session is present, it calls `is_java_foreground(instance)`, which launches `tmux list-panes -t =session:`.
- For an external `stop_requested` heartbeat it likewise probes the exact session and then queries the pane when the wrapper still exists.
- Each instance also reads its heartbeat JSON on each poll.

Thus a normal tick can launch roughly two tmux subprocesses per watched instance, plus heartbeat reads. The module docstring says one `list-sessions` call per poll covers all watched instances, but the current `_poll()` implementation does not use that batch path; the comment and implementation have diverged. This is a source-confirmed discrepancy, not a measured latency claim. The relevant tmux command capabilities are documented in the [tmux manual](https://man.archlinux.org/man/tmux.1.en) and [official format guide](https://github.com/tmux/tmux/wiki/Formats).

A possible Python-side refactor is a `watchdog_snapshot(instances)` adapter that uses one `list-sessions` call to determine exact session presence and, when sessions exist, one `list-panes -a` call with session name plus pane-current-command to build a per-session snapshot. Its output must distinguish:
- positively absent session;
- present session with Java foreground;
- present wrapper/shell with Java not foreground;
- pane/query unavailable or malformed (unknown).

Do not infer Java is gone when the pane query failed. Preserve the two-poll crash confirmation, pending Stop semantics, heartbeat ordering, and world-review gate before auto-restart. This would reduce subprocess startup overhead as the watched fleet grows; whether it matters should be measured before implementation.

Existing watchdog tests strongly cover one-instance safety semantics, including unknown probes, wrapper survival, two-miss confirmation, restart limits, pending Stops and world confirmation. They do not currently assert the number of tmux subprocesses for multiple simultaneous watched instances or validate a batched snapshot. The performance benchmark and new batch tests should therefore be kept separate from the existing safety suite.

### 2.6 Resource sampling is on-demand, not a resident monitor

`sample_machine_usage()` builds the managed PID set, samples procfs counters, waits 350 ms for a CPU delta, samples again, and optionally invokes `nvidia-smi` with a 1.5-second timeout. Its docstring explicitly says no timer is installed and no work continues after the call returns.

This is consistent with Crucible's demand-driven resource philosophy. A Rust daemon or always-running helper would be a regression unless it replaced existing work and measurably reduced total cost.

### 2.7 GUI cold-start imports are a separate measurement target

The UI data-loading policy is lazy, but module and widget construction are not. `crucible/ui/tabs/__init__.py` imports all ten tab modules, and `instance_panel.py` imports those tab classes when the panel module loads. `InstancePanel._build_ui()` then immediately constructs eight tab widgets and inserts them into the QTabWidget. This means the first window pays for those module imports and widget constructors before the user opens Worlds, Backup, Players, Setup, or Mods. It may contribute to cold-start latency and initial RSS, but no import-time or widget-construction profile has been collected.

Measure `python -X importtime` for CLI commands and `crucible gui` separately, then measure process RSS after first paint and after opening each tab. Keep the existing first-use data-loading behavior. If this is material, the actual optimization is deferred tab-widget construction, not just moving imports behind functions. Prototype one secondary tab at a time and measure first paint, initial RSS and first-open latency. Preserve signal wiring, parent ownership, selected-instance loading, cleanup, and action-state updates. Only do this if it provides a meaningful user-visible gain.


### 2.8 Process-scan optimization must close launch and removal safety gaps first

There are two distinct action-boundary gaps, not just a remove-preflight problem.

**Unregister/delete:** TmuxManager.safe_to_remove() returns safe immediately when the tmux executable is missing, without checking for an unmanaged process. When tmux exists, _unmanaged_pids() catches process-scan errors and returns an empty list, while _iter_pids() can also represent unavailable /proc as an empty inventory. The GUI calls safe_to_remove() before offering both unregister and permanent directory deletion. An incomplete observation can therefore be mistaken for process absence and allow live server files to be renamed/deleted.

**Start/restart:** TmuxManager.start() does not require a complete unmanaged-process scan. It checks tmux availability, its configured session through the boolean is_running() helper, a start script, and a configured IPv4 port-conflict probe. That port check catches common cases, but it is not a process-identity or process-absence guarantee. The CLI cmd_start() calls tmux.start() directly after world/property preflight; the GUI relies on cached _current_status == "stopped" after asynchronous world inspection; the shortcut checks get_status() but inherits the best-effort scan semantics; and Watchdog._do_restart() checks the world then calls tmux.start() without checking for an unmanaged JVM. CLI cmd_restart() checks get_status() more carefully, but its result also inherits the lossy process scan.

A manual server outside the registered tmux session can therefore be missed by some Start entry points, especially when /proc discovery is incomplete or the configured-port check does not identify its listener. The shared start path could then launch another JVM against the same server folder. The existing port-conflict probe remains valuable, but it must be an additional guard rather than a substitute for checking the exact instance's process association.

**Required separate safety fix before performance batching or Rust work:** give process discovery a tri-state result (complete/no match, complete/matching PIDs, incomplete/unavailable). At the shared TmuxManager.start() boundary, block launch if any process is associated with the instance and block when process discovery is incomplete. For unregister/delete, block on a matching PID or incomplete scan even when tmux is not installed. For force-kill, report exit as unknown unless a complete scan verifies absence after escalation. Keep action-time checks at the shared effect boundary; do not rely on cached GUI display state or front-end-specific prechecks.

Existing tests need to cover tmux missing plus unmanaged Java, unavailable/incomplete procfs plus remove/delete/start/force-kill, CLI cmd_start bypassing get_status(), stale GUI status during world confirmation, shortcut start, watchdog auto-restart with an unmanaged process, and normal complete no-match. Current StartTests.test_start_wrapper_has_no_unbounded_tee patches is_running=False and _port_conflict_message=None and expects launch success without asserting a process scan. Current watchdog security tests prevent double launch only when the original wrapper still owns its tmux session; they do not cover a detached JVM after the session disappears.

This is not a performance optimization or Rust migration task. Keep it as a focused safety patch with its own review and tests; do not combine it with the procfs batching or watchdog query-scaling work.

### 2.9 UI threading already addresses important responsiveness hazards

The main window has a persistent health thread. Its status path batches one `tmux list-sessions` call for all instances. The watchdog is a separate persistent Qt worker with a 10-second timer, and its current `_poll()` loop is *not* batched: for each watched running instance it invokes `TmuxManager.probe_running()` (a `tmux has-session` subprocess) and then `is_java_foreground()` (a separate `tmux list-panes` subprocess). Each command has a 2-second timeout, and the loop processes instances serially. For N healthy watched servers, the source-level upper bound is approximately 2N tmux subprocess calls per poll, plus heartbeat file reads; with slow/time-out commands, a single poll can take multiple seconds per instance. The watchdog module docstring says one `list-sessions` call covers all watched instances, but the implementation no longer matches that statement. This is a stronger scaling candidate than the already-batched main-window status query. The instance panel also uses persistent log-watcher and watchdog workers and avoids some blocking queued transitions when switching servers. Large operations have dedicated worker patterns. This is an area where a rewrite can introduce regressions even if individual Rust functions are faster.

The performance audit should measure event-loop stalls and worker queue latency before changing thread topology. Do not add more threads/processes by default.

## 3. Likely optimization candidates, ranked by evidence

| Priority | Candidate | Evidence | Risk | Next step |
|---|---|---|---|---|
| 1 | Batch watchdog session/pane observation per 10-second poll | `Watchdog._poll()` launches a session probe and pane probe per watched instance; the module docstring's batch claim is stale | High: unknown panes, wrapper countdowns and selected-pane semantics must remain exact | Benchmark 1, 5, 20 and 50 watched instances; assert subprocess count, per-instance output and unknown-result behavior |
| 2 | Reduce Start verification subprocess count without weakening early-exit detection | `_verify_started()` has a 6-second grace period and 250 ms sleep; about 24 `has-session` subprocesses run on a healthy start | High: marker handling and immediate launcher failure evidence must remain reliable | Compare marker-driven and less-frequent probes; test early script exit, marker-write failure, tmux timeout and wrapper survival |
| 3 | Share one per-instance procfs matcher across health and resource reporting | Health uses per-instance scans; resource reporting already has a one-pass aggregate matcher | High: destructive preflight must distinguish complete absence from an incomplete scan | After the separate safety fix, benchmark health refresh at 1, 5, 20 and 50 stopped instances; compare exact per-instance associations |
| 4 | Separate fast world-start safety classification from full save statistics | Full recursive selected-world scan runs on selection and again on Start; WorldTab can also scan size separately | High: incomplete alternate-save inspection must never become `no other world` or `new` | Benchmark p50/p95 scan time by file count; prototype quick classification plus independent stats only if all required safety paths remain fail-closed |
| 5 | Measure backup copy + destination readback | Fresh copies are hashed while streaming and fsynced, then read back and hashed again; the legacy archive path has a similar verification pass | High: removing verification could weaken backup integrity | Benchmark full, unchanged, and changed generations; compare disk read/write counters and CPU; retain exact-byte verification |
| 6 | Measure per-file durability and inventory overhead | Every fresh file is fsynced; staging is walked twice; every directory is sealed and fsynced; source files receive final stat checks | High: durability and race guarantees | Use synthetic trees with many small files and large files; profile syscall counts and time before considering batching |
| 7 | Reduce startup module/widget work, if material | Ten tab modules are imported and eight tab widgets are constructed during `InstancePanel._build_ui()`; only data loading is lazy | Medium: deferring widgets changes Qt object and signal lifetimes | Measure import, first-paint, initial RSS, and first-open latency; then prototype a single deferred secondary tab |
| 8 | Reduce redundant client-export passes without weakening verification | Fingerprinting, JAR inspection, distribution review, archive sealing, and final re-fingerprinting can each read the same inputs | High: every reviewed artifact must match the bytes actually packaged | Measure per-stage time/bytes; explore a staged immutable input inventory only if it preserves source-change and exact-SHA checks |
| 9 | Rust rewrite of Qt or tmux lifecycle | No evidence that language execution is the bottleneck; Qt/process control is mostly waiting on external systems | Very high | Do not pursue in the initial migration |

### 3.1 Client export has multiple intentional full-content passes

`client_export.py` is a large, safety-sensitive pipeline. `_fingerprint_input_tree()` hashes every eligible file in `mods`, `config`, `kubejs`, Prism `patches` and `libraries`, and optionally resource packs. For Prism exports, the pipeline may compute the input fingerprint before building and recompute it after the archive is sealed to ensure that source inputs did not change. JARs are also inspected for metadata, hashed for exact distribution-catalog decisions, copied into the archive, then read from the archive by `_seal_archive()` to build a member digest and run bounded privacy checks. The final ZIP is hashed as a whole.

These are not automatically wasteful duplicate loops: they prove that the exact JAR bytes reviewed are the bytes bundled, that the archive itself is intact, and that the source set did not change during the operation. But these full passes can dominate a large pack export. Benchmark each phase and record bytes read, not just wall time. A future optimization might build a staged immutable input snapshot with one digest inventory consumed by review and archive creation, but it must still reject source mutation and tie each catalog decision to the exact payload hash. Never use metadata-only caching as proof of content identity.


### 3.2.1 Start/Stop verification is intentional but process-heavy

`TmuxManager._verify_started()` checks an exit marker and then runs `tmux has-session` every 250 ms for up to a six-second grace window. This is roughly 24 tmux subprocess launches on a healthy start. It is a synchronous call inside the worker/CLI command, so the event loop is not blocked in the GUI, but `start()` does not report success until the grace window ends. A benchmark can test whether fewer probes (for example, marker checks at high frequency and session checks at selected intervals) preserve early failure detection without an extra six seconds of avoidable process spawning. Do not simply shorten the grace window without testing the slowest supported loader startup scripts.

Graceful Stop polls every two seconds for up to 90 seconds. It calls `has-session` on every cycle and, while the tmux session persists, `list-panes` to tell Java from the GTNH wrapper countdown; it may send Ctrl-C repeatedly to prevent the wrapper from relaunching Java after the owner asked to stop. The maximum path is about 45 session probes plus up to 45 pane probes. That cost is part of a user-initiated shutdown safety protocol, not an idle service. The first optimization candidate is a single per-tick session/pane observation adapter, while retaining `Unknown`, wrapper-alive, and graceful-only semantics. Do not reduce the time budget or escalate to force kill to make the benchmark look faster.

### 3.2 World and archive operations already avoid UI-thread blocking

World-size scans and world-start inspection run through dedicated workers rather than the Qt event loop. Prism archive extraction uses 1 MiB streaming reads, caps member count/expanded size/single-member size/compression ratio, rejects unsafe paths and symbolic links, and creates its temporary extraction tree under the destination parent when supplied. This avoids expanding a multi-GiB modpack into a small tmpfs `/tmp` and helps the final staged tree stay on the destination filesystem. Preserve these choices.



### 3.3 World-start safety scan is off-thread, but remains on the Start critical path

`inspect_world_start_state()` recursively walks the selected world and stats every regular file to compute logical size, newest modification time, region count and playerdata count. The worker keeps the Qt event loop responsive, but the user cannot Start until this fresh scan returns. Instance selection triggers a non-start summary scan, and clicking Start performs another fresh scan; opening Worlds can also trigger a separate `world_size_bytes()` traversal. A large GTNH save therefore pays more than one filesystem walk in the same session, and at least one walk is directly on the Start path. The runtime has no measured scan timings yet.

There is a plausible performance split: collect a small safety inventory (configured world path, root/link state, developed-save evidence, alternate saves, and confirmation requirement) first; compute size/mtime/count details as a separate background report. A cached detail inventory could help only if its source signature/generation is explicit and the Start gate still revalidates the configured name, selected path, relevant alternate saves and scan completeness immediately before action. Do not treat an incomplete scan as `new` or absence of alternate saves. `world_level_name()` also collapses a missing `server.properties` and a present-but-unreadable file into the same `"world"` default, and follows a symlinked file; a typed read result and fail-closed preflight correction are required before an optimization is considered. `_is_developed_world()` currently collapses some inaccessible alternate-save scans to `False`, which can hide that inspection was incomplete. These are correctness preconditions, not performance trade-offs.

Benchmark generated worlds with 1k, 10k, 100k and 500k files; compare selection-scan and pre-start-scan wall time, stat/syscall count, bytes read, and p50/p95 across warm/cold caches. Include an unreadable alternate dimension subtree, symlink, unreadable `server.properties`, and concurrently changing files. Run the measurements off the live GTNH world.

### 3.4 Icon retrieval is already demand-driven and bounded

The Add Mod dialog uses one short-lived `_IconWorker` that drains a single queue sequentially. The queue has a hard cap of 48 pending jobs, visible/selected icons can be promoted ahead of off-screen work, obsolete queued jobs are discarded when the search changes, and an in-flight request is allowed to finish unless the dialog is cancelled. Each request uses the Modrinth-only icon adapter (the current UI offers only Modrinth), which is capped at 8 MiB and 8 seconds. This is a good example of resource-aware application design: it prioritizes what the user can see without introducing a large concurrent request burst or resident icon service. Keep it in Python unless an end-to-end profile proves icon decoding or network scheduling is a real bottleneck.

## 4. Why Rust may or may not improve the hot paths

- Python's `hashlib` delegates SHA-256 to native cryptographic code (typically OpenSSL-backed in CPython builds). The Python loop has overhead, but the hash computation itself is not interpreted Python bytecode.
- File copying and hashing are often limited by storage throughput, file metadata operations, fsync latency, or archive compression rather than language execution.
- Rust can help with tight parsing/validation loops, large in-memory inventories, and carefully parallelizable CPU work. It can also lose if it adds per-call process startup, extra copies, or a slower hashing backend.
- Parallel hashing can saturate CPU and storage, contend with a running Minecraft server, and worsen latency. It must be opt-in or carefully bounded and benchmarked, not assumed to be faster.
- A JSON subprocess worker is a poor fit for tiny high-frequency status decisions. If a Rust core is used for such decisions, a native extension or a larger batched request is more plausible, but only after the boundary is stable.
- A native extension adds packaging and ABI complexity. That cost may be justified for a proven hot loop, not for code that simply invokes tmux or waits for disk.

## 5. Benchmark plan before implementation

### 5.1 Establish repeatable fixtures

Use sanitized, generated data only. Never benchmark by running backup/restore experiments against the owner's active GTNH server or live world.

Create fixture classes:
- **Small server:** hundreds of files, small world, representative config/mod files.
- **Large world:** a generated tree with large region-like files and realistic directory depth.
- **Many-small-files:** tens of thousands of tiny files to stress traversal, metadata, and fsync.
- **Legacy archives:** ZIPs of several sizes, with valid and deliberately invalid metadata for negative tests.
- **Process table:** controlled fake procfs or isolated helper processes to scale process count without matching real server paths.

### 5.2 Capture baseline metrics

For each operation, capture:
- wall-clock duration and CPU user/system time;
- bytes read and written (where available);
- number of files/directories and file-size distribution;
- peak RSS;
- number of subprocesses and their durations;
- fsync/rename/link counts for backup;
- cold vs warm filesystem cache conditions;
- p50/p95 over multiple runs, not a single best result.

Record environment details: CPU, storage device/filesystem, kernel, Python/OpenSSL versions, Rust toolchain, and whether the test was on tmpfs, SSD, or HDD. Do not compare numbers from different storage/cache conditions as if they were language results.

### 5.3 Compare equivalent implementations

For any candidate Rust primitive:
1. Keep the current Python implementation as the reference.
2. Use identical input bytes, paths, chunk sizes, and output semantics.
3. Compare Python-only, Rust library, and (if relevant) JSON-worker paths.
4. Include warm and cold data, changed and unchanged inputs, and worst-case safety checks.
5. Measure total operation time, not just the inner hash or parser.
6. Verify outputs byte-for-byte and preserve failure behavior.
7. Test CPU/RAM contention while a separate dummy workload runs; a faster backup that starves a live Minecraft JVM is not an improvement.

### 5.4 Suggested go/no-go threshold

Do not port a module for a marginal microbenchmark. A candidate should show a repeatable end-to-end improvement that matters to users (initial planning threshold: at least 20% on the target operation or a clear reduction in UI latency/idle resource use), with no meaningful regression in memory, startup, packaging, or safety. This is a decision threshold for this project, not a universal performance rule.

## 6. First Rust slice recommendation

Do **not** start with fleet backup or tmux.

The first Rust work should still be a pure, side-effect-free core crate and parity harness. But the first production integration should be selected only after the baseline identifies a real hot path. If watchdog subprocess startup is confirmed dominant, batch session/pane snapshots in Python first and prove multi-instance parity. If procfs scanning is confirmed dominant, refactor the existing Python aggregate matcher into a shared per-instance association helper. Only consider Rust if those optimized Python implementations remain measured bottlenecks. If backup I/O dominates, optimize the Python algorithm and filesystem strategy first; a Rust rewrite may not improve the storage bottleneck.

The best performance outcome may be:
- Rust for one measured CPU-heavy primitive;
- Python retained for Qt, process orchestration, and I/O workflows;
- fewer redundant host scans or data passes;
- zero additional idle services or background polling;
- unchanged server/world safety and exact rollback behavior.


## 7. Integration overhead, panic containment, and release profiles

### 7.1 PyO3 can avoid process IPC, but it is not free

A PyO3 extension executes in Crucible's process and avoids starting a helper executable for each call. That is attractive for a measured CPU-heavy function with a stable, small input/output contract. It also means a native bug, process abort, memory error, or unsafe lifetime mistake can take down the entire Qt application.

For a PyO3 prototype:

- Pass owned, bounded Rust inputs and return immutable typed results; do not retain borrowed Python/Qt objects after the call.
- Keep Python and Qt interaction on the Python side. Do not call Qt widgets from Rust.
- For multi-millisecond Rust-only computation or file scanning, use PyO3's detach mechanism so the Python interpreter can run other threads while the Rust-only closure operates. The detached closure must not access Python objects; copy the needed values into Rust-owned data first.
- Return expected failures as typed `Result` values mapped to Python exceptions. Do not use panic for expected bad input, inaccessible paths, unavailable tmux, or ordinary I/O errors.
- Benchmark the full Python-to-Rust call including conversion/copying, not only the Rust inner loop. For tiny predicates the boundary can cost more than the work.

PyO3's current guide recommends detaching for Rust-only work expected to last multiple milliseconds so the Python interpreter can run other threads. That is most useful for a coarse-grained CPU/I/O-independent batch, not for a loop which repeatedly attaches to Python for each tiny item. The guide also documents conversion and reference-management costs at the boundary. Crucible should therefore pass one bounded input snapshot and return one structured result per operation; it should not call a Rust function once per file or per UI row. This recommendation is conditional on a future measured PyO3 integration; the present pilot has no Python bridge. [PyO3 performance guide](https://pyo3.rs/v0.30.1/performance.html).

### 7.1.1 Toolchain snapshot as of 2026-11-06

The Crosshair branch's observed Rust 1.92 pin is a useful historical example, not the current stable compiler. The official Rust release list shows Rust **1.99.1**, released 2026-10-15, as the latest stable release on this audit date. Rust 1.99.1 includes fixes for an x86_64 FFI `bool` return-value miscompilation, so the runtime version used for a first FFI prototype should be pinned and recorded rather than inherited from the older Crosshair branch. [Rust 1.99.1 release announcement](https://blog.rust-lang.org/2026/10/15/Rust-1.99.1/), [official release list](https://blog.rust-lang.org/releases/).

PyO3's current published release is **0.30.1**, dated 2026-10-14. PyO3 0.30 dropped Python 3.8 support, set MSRV to Rust 1.85, and added support for Python 3.15; 0.30.1 added PyPy 3.12 support. PyO3 0.30.1 supports CPython 3.9+ and PyPy 7.3 (Python 3.11+). Crucible's existing minimum Python remains 3.11, and hosted CI currently tests 3.11 and 3.13, so the PyO3 release's nominal support is compatible with the currently tested CPython versions. This does **not** mean PyO3 should be selected now; the bridge still needs a measured benefit and a test of the exact wheel in Crucible's final installed layout. [PyO3 0.30.1 changelog](https://pyo3.rs/v0.30.1/changelog), [PyO3 0.30.1 package metadata](https://docs.rs/crate/pyo3/0.30.1).

For a future PyO3 experiment, `abi3-py311` is a reasonable first wheel target to test because it targets the oldest CPython version Crucible currently supports and can serve later compatible CPython releases. It is not a blanket compatibility guarantee: the Stable ABI has a restricted API surface and does not automatically solve platform linking or wheel tags. Do not select `abi3t` merely because free-threaded Python exists: it targets Python 3.15+ and requires separate ABI/build validation; Crucible's current supported runtime matrix is 3.11/3.13 and the application has no stated free-threaded support requirement. [PyO3 ABI feature guide](https://pyo3.rs/v0.30.1/features), [PyO3 0.30.1 ABI feature definitions](https://github.com/PyO3/pyo3/blob/v0.30.1/Cargo.toml).

Linux wheel distribution remains a separate product decision. Maturin's guide notes that Linux extension wheels must follow manylinux or musllinux symbol/library constraints for broad binary portability; it recommends manylinux images or Zig, and documents the glibc floor for the Rust compiler's Linux toolchains. Crucible currently installs a source package into a user venv, so adopting PyO3 means the release process must build and ship a correctly tagged native artifact, verify it in the final install staging directory, retain rollback if the artifact is absent or incompatible, and avoid requiring owners to compile Rust during normal updates. Decide whether Crucible wants a narrowly targeted Nobara/Fedora native wheel, a manylinux wheel, or a small out-of-process helper only after weighing support burden and real platform goals. [Maturin Linux distribution guide](https://www.maturin.rs/distribution.html).

**Toolchain policy for Phase 1:** use the current stable toolchain for development validation (1.99.1 on this audit date), but choose and test a deliberate `rust-version` MSRV separately. A locked toolchain file and `Cargo.lock` make CI reproducible; they should not force owners to install the Rust toolchain if release artifacts are prebuilt. The actual MSRV should be no newer than the chosen dependency graph requires and should be raised only with an explicit product compatibility decision.

### 7.2 Do not copy Crosshair's `panic = "abort"` into an in-process Crucible extension

The Crosshair `rust-port` Cargo profile currently specifies `panic = "abort"`. That profile belongs to its standalone executable architecture. Rust documents that with aborting panics, a panic terminates the process and cannot be recovered by `catch_unwind`. In an in-process PyO3 extension, that means an unexpected panic can terminate the whole Crucible GUI and every other server-management view in that process.

Provisional rule:
- **In-process PyO3 library:** keep an unwinding panic strategy in the extension build, prevent panics from crossing the Python/C ABI, catch/translate only where recovery is sound, and make normal operations return typed errors. Do not choose `panic = "abort"`.
- **Out-of-process Rust worker:** process isolation contains a worker crash, but the Python parent must detect EOF/nonzero exit, discard the incomplete response, report the component unavailable, and fall back only for a pure/idempotent operation. Never replay a potentially side-effecting request merely because the worker response was lost.
- **C ABI:** treat ABI unwinding rules as a hard safety boundary. Never allow an ordinary Rust panic to unwind through a non-unwind foreign ABI. A child-process protocol may be simpler and safer than inventing a custom FFI contract.

This is especially important because Crucible's user value is long-lived manager stability: a Rust speedup that can abort the GUI is not an acceptable improvement.

### 7.3 Choose release optimization settings for throughput, not borrowed defaults

Crosshair uses `opt-level = "s"` to favor binary size. Cargo's standard release profile defaults to `opt-level = 3`, `codegen-units = 16`, LTO off, no symbol stripping, and `panic = "unwind"`. Rust documents that a single codegen unit may improve runtime code at the cost of slower compilation; LTO can increase optimization at the cost of link time; and strip settings remove symbols useful for debugging.

For Crucible:
- Start performance comparisons with the default optimized release profile.
- Benchmark `opt-level = 3` against size-oriented options only if binary size is a real constraint.
- Benchmark ThinLTO and `codegen-units = 1` independently; do not assume their combination is worth longer build times.
- Keep a symbolized benchmark/profile build even if release artifacts are later stripped.
- Treat panic strategy as a safety/containment decision, not an optimization toggle.
- Measure clean build time, incremental build time, link time, binary/wheel size, first import/load time, and operation latency as separate product costs.

### 7.4 Maturin and stable ABI affect how the extension reaches users

Maturin can build PyO3 native extensions into platform-tagged wheels. The stable CPython ABI can reduce the number of Python-version-specific wheel builds, but it restricts the Python C-API surface, and Linux wheel portability requires a deliberate manylinux/musllinux or native Linux policy.

Crucible's current install path does not use Maturin or install a native wheel. It builds a user venv, copies source, compiles Python files, smoke-tests `crucible --help`, then atomically publishes the install. If PyO3 is chosen, the migration must deliberately add a prebuilt compatible artifact to the release/install staging flow, hash/verify it, test import from the final path, and restore the old install if the native artifact is missing or incompatible. Do not make owners install Rust or compile native code during routine application updates.

The minimum supported Python matrix should remain 3.11/3.13 until a separate product decision changes it. A build that works only under the developer's host interpreter is insufficient.

## 8. Source files inspected for this audit

- `crucible/data/fleet_backup.py`
- `crucible/data/backup_manager.py`
- `crucible/data/backup_scheduler.py`
- `crucible/process/resource_monitor.py`
- `crucible/process/machine_resources.py` (including its existing one-pass `_instance_processes` matcher)
- `crucible/process/tmux_manager.py`
- `crucible/process/watchdog.py` (including the current per-instance tmux polling loop)
- `crucible/ui/add_mod_dialog.py` (bounded prioritized icon queue)
- `crucible/mods/icon_fetch.py` (provider-specific icon HTTP)
- `crucible/ui/main_window.py`
- `crucible/ui/instance_panel.py`
- `crucible/diagnostics/loadcheck.py`
- `crucible/download_http.py`
- `tests/test_fleet_backup.py`

Relevant existing regression coverage includes incremental hard-link reuse, retention and failed-publish preservation, fsync failure rollback, running-server readiness and save acknowledgement, unknown-state refusal, lock coordination, symlink rejection, deep verification, and restore safety. These are behavioral contracts, not optional test details.


## 9. External measurement and runtime references

- Python `-X importtime` reports cumulative and self import timings and is available in Python 3.11/3.13: https://docs.python.org/3/using/cmdline.html
- Python's `cProfile` documentation explains call counts, direct/cumulative time, and why profiling data is not a substitute for timing: https://docs.python.org/3.14/library/profile.html
- Python `timeit` provides repeated timing for isolated expressions/functions: https://docs.python.org/3/library/timeit.html
- Rust's library benchmarking guidance says to optimize real-world hot paths rather than syscall-dominated operations: https://std-dev-guide.rust-lang.org/development/perf-benchmarking.html
- Cargo release profile defaults and build/runtime trade-offs: https://doc.rust-lang.org/cargo/reference/profiles.html
- Rust FFI unwinding and abort behavior: https://doc.rust-lang.org/nomicon/ffi.html
- PyO3 performance guidance for detaching Rust-only work: https://pyo3.rs/main/performance.html
- PyO3's supported interpreter families and minimum versions: https://pyo3.rs/main/doc/pyo3/
- Maturin's PyO3 bindings and stable-ABI wheel behavior: https://www.maturin.rs/bindings
- Maturin's Linux wheel portability guidance: https://www.maturin.rs/distribution
- Microsoft's Rust performance guidance recommends profiling hot paths, benchmarks and CPU/allocation observations: https://microsoft.github.io/rust-guidelines/guidelines/performance/
- Rust codegen options document the trade-offs between optimization levels, codegen units, LTO, compile time and binary size: https://doc.rust-lang.org/rustc/codegen-options/
- PyO3's guide documents how Rust-only work can detach from the Python interpreter while it runs: https://pyo3.rs/main/
- Maturin's guide documents Python wheel/native extension packaging: https://www.maturin.rs/

Crucible's CI currently supports Python 3.11 and 3.13, not 3.15. Use `-X importtime`, `cProfile`, and `timeit` for the current baseline; do not rely on new Python 3.15-only profiling interfaces until the supported runtime matrix changes.



## 10. Current Rust pilot and performance interpretation

### 10.1 PyO3 boundary and batching rule

The official PyO3 performance guide explains that attaching/detaching and converting Python objects is part of total cost; it recommends `Python::detach` for Rust-only work expected to last multiple milliseconds. Its implementation guidance also favors lower-cost type checks/call conventions where relevant. For Crucible the architecture implication is more important than micro-optimizing wrapper calls: send a bounded batch of plain owned values to Rust, do the work without Python objects, and return one bounded structured result. Avoid a Python<->Rust transition per JAR, per archive member, per process, or per UI row. Never detach while still holding Python references and then touch those references in the detached closure. Do not use unsafe reference-pool flags as an early performance tweak; they make object-lifetime errors fatal or leak-prone. See [PyO3 performance guidance](https://pyo3.rs/v0.30.1/performance.html) and [PyO3 parallelism guidance](https://pyo3.rs/v0.30.1/parallelism.html).


The first Rust code in this PR consists of two dependency-free GTNH string primitives: version parsing/comparison at `rust/crucible-core/src/pack_version.rs` and server-pack name/Java-variant parsing at `rust/crucible-core/src/pack_identity.rs`. They are low-risk contract/toolchain exercises because their behavior is deterministic and independent of application I/O. Neither is identified as a current performance bottleneck. Version parsing and filename parsing are tiny parts of pack-index/identity work; no benchmark has shown them to dominate update planning or release selection.

The Rust CI workflow currently requests the floating `stable` channel rather than pinning a dated compiler. The tested code head `5de50a906330323f000aa44ab69b8bbcdbd95d69` resolved stable Rust 1.99.1 (`58cf8f9c3`, 2026-10-13) and passed formatting, 9 Rust unit tests and Clippy with warnings denied; the separate Rust 1.85.0 MSRV job also passed all 9 tests. Python 3.11 and 3.13 each passed 845 tests and 95 subtests plus the PyQt interaction smoke. See [Rust CI](https://github.com/Roaring1/Crucible/actions/runs/42321790877) and [Python CI](https://github.com/Roaring1/Crucible/actions/runs/42321790881). The Cargo manifest declares MSRV 1.85, and that declaration is now verified for this dependency-free crate. The stable compiler version remains floating; pin it or record the exact compiler version for reproducible benchmark comparisons.

The Rust pilot makes **no speed claim** and does not justify a runtime bridge, parallelism, or replacement of the Python implementation. The shared TSV fixtures now cover ASCII version syntax, server-pack name variants, invalid names and search precedence. The version parser deliberately rejects Unicode decimal digits accepted by Python's `\d`; the server-name parser likewise requires an ASCII first digit after `17-`; and `str::trim()` differs from Python `str.strip()` for U+001C..U+001F. Rust-only tests pin those differences, and Python-only regression tests preserve the Python behavior. These gaps still prevent production use. The workflow's floating `stable` channel is convenient for quick CI validation but does not provide a reproducible historical compiler; either add a pinned toolchain file with an explicit update policy or make the toolchain version visible in every benchmark result.

No operation benchmark, CPU profile, allocation profile, runtime bridge benchmark, or before/after application measurement has been run for either parser. The only measured timings here are CI build/test durations, which are not application performance. The measured-performance backlog remains watchdog subprocess batching, process discovery, world scanning, backup I/O, client export and cold-start work described above.

