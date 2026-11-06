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

For a newly copied server file, `_copy_and_hash_live()` hashes the source bytes while writing the staged copy and fsyncs the destination. Then `_copy_server_tree()` calls `_hash_file(destination)` to read the new copy again and compare its digest.

The legacy-archive path `_snapshot_archive_file()` has a similar post-copy destination hash. This can add a complete additional read of newly copied data. For a large world or pack, the extra read can be material.

This is **not yet classified as waste**. The readback may be an intentional assurance that the staged destination contains the expected bytes after write/flush. Before changing it, measure the time and determine whether the assurance can be preserved with a lower-cost method (for example, validating destination metadata and retaining a deep-verification mode, or using platform-specific copy/verification facilities with explicit durability semantics). Never simply remove integrity checks to make a benchmark look better.

There are other possible I/O costs:
- A flush/fsync for each file can be expensive for trees containing many small files.
- Full inventory traversal occurs before and after the copy.
- Final per-file stat verification is another metadata pass.
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

There is an important existing building block: `crucible/process/machine_resources.py::_instance_processes(instances)` already scans `/proc` once for all registered instances, but returns only an aggregate PID list for resource reporting. The health path in `tmux_manager.py` still calls the per-instance `ResourceSampler.find_instance_pids()` helper. A promising refactor is to share one matcher that returns per-instance PID associations, then use it in the health status map and resource sampler. This would remove repeated enumeration without introducing Rust or changing the safety model. Preserve exact session-marker matching, the Java executable check, path/cwd fallback, and unknown/error semantics. Do not cache stale results across lifecycle actions; safety-critical actions must still revalidate immediately.

### 2.5 Resource sampling is on-demand, not a resident monitor

`sample_machine_usage()` builds the managed PID set, samples procfs counters, waits 350 ms for a CPU delta, samples again, and optionally invokes `nvidia-smi` with a 1.5-second timeout. Its docstring explicitly says no timer is installed and no work continues after the call returns.

This is consistent with Crucible's demand-driven resource philosophy. A Rust daemon or always-running helper would be a regression unless it replaced existing work and measurably reduced total cost.

### 2.6 GUI cold-start imports are a separate measurement target\n\nThe UI data-loading policy is lazy, but the module-import graph is not fully lazy. `crucible/ui/tabs/__init__.py` imports all ten tab modules, and `instance_panel.py` imports those tab classes when the panel module loads. That means code for Mods, Worlds, Players, Backup, Setup and other tabs is imported during GUI startup even though some tab data is loaded only on first view. This may contribute to cold-start latency and resident module memory, but no import-time profile has been collected.\n\nMeasure `python -X importtime` for CLI commands and `crucible gui` separately, then measure process RSS after first paint and after opening each tab. Keep the existing first-use data-loading behavior. Only make module imports lazy if measurements show the import graph is material and Qt construction/lifetime remains stable.\n\n### 2.7 UI threading already addresses important responsiveness hazards

The main window has a persistent health thread. The instance panel uses persistent log-watcher and watchdog workers and avoids some blocking queued transitions when switching servers. Large operations have dedicated worker patterns. This is an area where a rewrite can introduce regressions even if individual Rust functions are faster.

The performance audit should measure event-loop stalls and worker queue latency before changing thread topology. Do not add more threads/processes by default.

## 3. Likely optimization candidates, ranked by evidence

| Priority | Candidate | Evidence | Risk | Next step |
|---|---|---|---|---|
| 1 | Share one per-instance procfs matcher across health and resource reporting | Health uses per-instance scans; machine-resource reporting already has an aggregate one-pass scan that can be refactored to return associations | Medium: false negatives could affect status/removal safety | Benchmark health refresh at 1, 5, 20, and 50 stopped instances with controlled host process counts; preserve action-time revalidation |
| 2 | Measure backup copy + destination readback | Fresh copies are hashed during streaming copy and then hashed again from the destination | High: removing verification could weaken backup integrity | Benchmark full and incremental backup; compare disk read/write counters and CPU; decide whether a safer cheaper integrity proof exists |
| 3 | Reduce backup per-file metadata/fsync overhead if dominant | Each file is copied through a temp file, flushed/fsynced, renamed, then re-inventoried and stat-checked | High: durability and race guarantees | Use synthetic trees with many small files and large files; profile syscall counts and time |
| 4 | Improve GUI cold import cost, if material | Ten tab modules are imported through `ui.tabs.__init__` when InstancePanel loads even though tab data loads lazily | Low-to-medium | Measure `-X importtime`, first-paint latency and RSS before considering import-lazy tabs |
| 5 | Reduce redundant client-export passes without weakening verification | Fingerprinting, JAR inspection, distribution review, archive sealing, and final re-fingerprinting can each read the same inputs | High: every reviewed artifact must match the bytes actually packaged | Measure per-stage time/bytes; explore a staged immutable input inventory only if it preserves source-change and exact-SHA checks |
| 6 | Rust rewrite of Qt or tmux lifecycle | No evidence that language execution is the bottleneck; Qt/process control is mostly waiting on external systems | Very high | Do not pursue in the initial migration |

### 3.1 Client export has multiple intentional full-content passes\n\n`client_export.py` is a large, safety-sensitive pipeline. `_fingerprint_input_tree()` hashes every eligible file in `mods`, `config`, `kubejs`, Prism `patches` and `libraries`, and optionally resource packs. For Prism exports, the pipeline may compute the input fingerprint before building and recompute it after the archive is sealed to ensure that source inputs did not change. JARs are also inspected for metadata, hashed for exact distribution-catalog decisions, copied into the archive, then read from the archive by `_seal_archive()` to build a member digest and run bounded privacy checks. The final ZIP is hashed as a whole.\n\nThese are not automatically wasteful duplicate loops: they prove that the exact JAR bytes reviewed are the bytes bundled, that the archive itself is intact, and that the source set did not change during the operation. But these full passes can dominate a large pack export. Benchmark each phase and record bytes read, not just wall time. A future optimization might build a staged immutable input snapshot with one digest inventory consumed by review and archive creation, but it must still reject source mutation and tie each catalog decision to the exact payload hash. Never use metadata-only caching as proof of content identity.\n\n### 3.2 World and archive operations already avoid UI-thread blocking\n\nWorld-size scans and world-start inspection run through dedicated workers rather than the Qt event loop. Prism archive extraction uses 1 MiB streaming reads, caps member count/expanded size/single-member size/compression ratio, rejects unsafe paths and symbolic links, and creates its temporary extraction tree under the destination parent when supplied. This avoids expanding a multi-GiB modpack into a small tmpfs `/tmp` and helps the final staged tree stay on the destination filesystem. Preserve these choices.\n\n## 4. Why Rust may or may not improve the hot paths

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

The first Rust work should still be a pure, side-effect-free core crate and parity harness. But the first production integration should be selected only after the baseline identifies a real hot path. If procfs scanning is confirmed dominant, first refactor the existing Python aggregate matcher into a shared per-instance association helper and prove fixture parity; only consider Rust if that optimized Python implementation remains a measured bottleneck. If backup I/O dominates, optimize the Python algorithm and filesystem strategy first; a Rust rewrite may not improve the storage bottleneck.

The best performance outcome may be:
- Rust for one measured CPU-heavy primitive;
- Python retained for Qt, process orchestration, and I/O workflows;
- fewer redundant host scans or data passes;
- zero additional idle services or background polling;
- unchanged server/world safety and exact rollback behavior.

## 7. Source files inspected for this audit

- `crucible/data/fleet_backup.py`
- `crucible/data/backup_manager.py`
- `crucible/data/backup_scheduler.py`
- `crucible/process/resource_monitor.py`
- `crucible/process/machine_resources.py` (including its existing one-pass `_instance_processes` matcher)
- `crucible/process/tmux_manager.py`
- `crucible/process/watchdog.py`
- `crucible/ui/main_window.py`
- `crucible/ui/instance_panel.py`
- `crucible/diagnostics/loadcheck.py`
- `crucible/download_http.py`
- `tests/test_fleet_backup.py`

Relevant existing regression coverage includes incremental hard-link reuse, retention and failed-publish preservation, fsync failure rollback, running-server readiness and save acknowledgement, unknown-state refusal, lock coordination, symlink rejection, deep verification, and restore safety. These are behavioral contracts, not optional test details.


## 8. External measurement and runtime references

- Python `-X importtime` reports cumulative and self import timings and is available in Python 3.11/3.13: https://docs.python.org/3/using/cmdline.html
- Python's `cProfile` documentation explains call counts, direct/cumulative time, and why profiling data is not a substitute for timing: https://docs.python.org/3.14/library/profile.html
- Python `timeit` provides repeated timing for isolated expressions/functions: https://docs.python.org/3/library/timeit.html
- Rust's library benchmarking guidance says to optimize real-world hot paths rather than syscall-dominated operations: https://std-dev-guide.rust-lang.org/development/perf-benchmarking.html
- Microsoft's Rust performance guidance recommends profiling hot paths, benchmarks and CPU/allocation observations: https://microsoft.github.io/rust-guidelines/guidelines/performance/
- Rust codegen options document the trade-offs between optimization levels, codegen units, LTO, compile time and binary size: https://doc.rust-lang.org/rustc/codegen-options/
- PyO3's guide documents how Rust-only work can detach from the Python interpreter while it runs: https://pyo3.rs/main/
- Maturin's guide documents Python wheel/native extension packaging: https://www.maturin.rs/

Crucible's CI currently supports Python 3.11 and 3.13, not 3.15. Use `-X importtime`, `cProfile`, and `timeit` for the current baseline; do not rely on new Python 3.15-only profiling interfaces until the supported runtime matrix changes.
