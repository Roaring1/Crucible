# Crucible Source and Runtime Audit for the Rust Migration

**Status:** Phase 0 source audit; no implementation changes  
**Date:** 2026-11-06  
**Source baseline:** `main` at `0eb89f2517fc3c4ba9417c2bc97e6c63b5f80b8c`  
**Companion documents:** [Architecture assessment](rust-migration-assessment.md), [Performance audit](rust-migration-performance-audit.md)

## 1. Audit boundary and repository size

The recursive Git tree for the recorded `main` baseline contains 219 entries, including:

- **80 Python source files** under `crucible/`, approximately **1.70 MB** of source text.
- **62 Python test/support files** under `tests/`, approximately **755 KB** of source text.
- Eleven Python package directories/subpackages.

These counts describe source-control contents, not installed package size or runtime memory. They are not a performance benchmark.

The repository is a layered desktop application, not a narrow launcher. Important ownership boundaries are:

- CLI dispatch and user-facing command behavior: `crucible/__main__.py`.
- Persistent server identities and model behavior: `crucible/data/`.
- Host/process observations and tmux lifecycle operations: `crucible/process/`.
- External package formats and provider integrations: `crucible/importers/`, `crucible/exporters/`, and `crucible/mods/`.
- Upload and share lifecycle: `crucible/sharing/`.
- Qt presentation, user decisions, and background worker coordination: `crucible/ui/`.
- Diagnostic parsing and optional repair: `crucible/diagnostics/`.

No runtime implementation was changed during this audit.

## 2. Module map

### 2.1 CLI and application entry

`crucible/__main__.py` is about 2,024 lines and combines argument parsing, command handlers, GUI startup dispatch, shortcut actions, pack maintenance, backups, and recovery commands. Shared behavior is mostly delegated to domain/process modules, but this file is still a large integration surface.

Important CLI families observed:

- Registry and inspection: list, add, remove, scan, info, edit, validate, doctor.
- Lifecycle: start, stop, restart, status, attach, send, shortcut action.
- Import/install: Prism scan/import, mod downloads, modpack/server installation, version discovery.
- Safety/maintenance: property repair, loading diagnostics, backup-fleet run/list/verify, pack update, rollback, previous-install pruning.
- GUI launch: imported only after the selected command dispatches to GUI mode.

Top-level imports include the instance manager/model, world-start inspection, crash heartbeat, tmux manager, and common CLI formatting. Several heavier import/installer/diagnostics modules are imported inside the relevant handler. This is a useful separation to preserve: a normal CLI command should not import all Qt widgets or initialize a GUI.

### 2.2 Data and domain model

- `instance_model.py`: server identity, validation, serialization, launcher/log discovery, world naming and size, Java memory, port, readiness.
- `instance_manager.py`: registry persistence, duplicate checks, external-edit detection/reconciliation, discovery, add/remove/reorder.
- `properties.py`: server.properties parsing, typed validation, repair suggestions, atomic/backup-aware repair.
- `world_start_summary.py`: configured-world safety classification and summary used before Start.
- `fleet_backup.py`: current fleet-wide immutable snapshot generations, incremental hard-link reuse, verification, restore, legacy-source migration.
- `backup_manager.py`: legacy world archives, world swap, reset, wipe, pre-swap rollback data, worker classes.
- `backup_scheduler.py`: user systemd timer installation/removal/status.
- `netinfo.py`: local/public network address helpers.
- `server_icons.py`: user artwork persistence and Prism icon lookup.

The data package exports `ServerInstance` and `InstanceManager` at import time and lazily resolves `BackupManager` through `__getattr__`. Preserve that lazy import behavior if the package is reorganized. The model and registry types are strong candidates for stable cross-language contracts, but they must not be redesigned solely to look more idiomatic in Rust.



### 2.2.1 The instance model is a mixed domain/filesystem object

`ServerInstance` is not a pure data-transfer object. Its stable core is the registry identity and metadata contract:

- `id`, `path`, `name`, `version`, `pack_source`, Minecraft/loader versions, launch args, tmux session, watchdog preference, created/last-started timestamps, FML prompt choice, and `extra` unknown fields.
- `to_dict()` and `from_dict()` preserve forward-compatible unknown registry keys through `extra`, normalize invalid FML prompt values to the safe `ask` default, and retain legacy defaults for missing fields.
- `_derive_session_name()`, memory token parsing/rewriting, loader/version normalization helpers, and TPS command selection are candidates for pure domain functions once fixture behavior is frozen.

The same class also performs filesystem and host queries: validate paths, find launch scripts/logs, count/read mods, inspect world directories, read `server.properties`, inspect EULA, search launcher files, discover Java, and compute world sizes. `set_eula_accepted()` writes a file; `set_memory_mb()` only updates the in-memory Java argument string until the caller persists the model.

This is a key migration boundary: do not port the whole `ServerInstance` class as if it were a clean Rust struct. Separate (a) the versioned registry/domain record, (b) pure parsing/classification helpers, and (c) filesystem/process adapters. The public Python object can continue to be the compatibility facade while these components are gradually extracted.

### 2.3 Process and host adapters

- `tmux_manager.py`: command execution, exact tmux targets, session/process/readiness probes, start/stop/send/attach, status map.
- `resource_monitor.py`: Linux procfs process discovery, RSS/CPU sampling, PID association.
- `machine_resources.py`: aggregate host/server usage and optional NVIDIA query.
- `crash_recovery.py`: atomic heartbeat and boot-ID reconciliation.
- `watchdog.py`: Qt watchdog state machine and restart coordination.
- `log_tail.py` and `log_watcher.py`: bounded tail reads, filesystem notifications, incremental log parsing, startup/stopping events.
- `startup_patterns.py` and `command_intent.py`: shared parser patterns and explicit console lifecycle intent.
- `netfix.py`: Java/network launch argument compatibility.

The process package exports `TmuxManager` immediately. The watchdog and log watcher remain lazy exports. Process adapters must continue to represent uncertainty explicitly. Rust should not add a second tmux controller, process watcher, or watchdog while the Python UI is still authoritative.



### 2.3.1 Process matching semantics and a destructive-removal edge

The two existing PID matchers share a broad shape, but their precise semantics must be captured before a consolidation:

- Both prefer the exact `-Dcrucible.session=<session>` token in the process command line. **That marker branch does not check `/proc/<pid>/comm` first.**
- The legacy fallback checks that kernel-reported `comm` is exactly `java`, then matches the registered server path in argv or exact process cwd.
- `machine_resources._instance_processes()` uses sets of session markers and paths and scans `/proc` once for the whole fleet, but it returns only a sorted aggregate PID list, not per-instance associations.
- `ResourceSampler.find_instance_pids()` scans `/proc` independently for each server and returns that server's PID list.

`TmuxManager.start()` constructs `CRUCIBLE_JAVA_ARGS` containing the session marker inside a `bash -c` command. The resource tests currently prove matching for a synthetic Java command line, and prove that a Dolphin/cat command whose *path* happens to contain `java` is not mistaken for a JVM. They do not establish whether a launcher shell that retains the `bash -c` command text can itself match the marker token. Add a representative wrapper-plus-JVM fixture before changing this matcher. If wrappers do match, the team must decide whether to preserve wrapper attribution deliberately or restrict server-PID/resource attribution to the Java executable; do not silently change current behavior in a Rust rewrite.

**Separate high-priority safety observation - removal when tmux is unavailable:** `TmuxManager.safe_to_remove()` currently returns `(True, ...)` immediately when `tmux_available()` is false, without checking `_unmanaged_pids()`. The GUI's `_on_remove_requested()` calls this preflight before offering both Remove from list and Delete server files. Therefore, a Java server started outside tmux can evade this preflight when the tmux executable is missing, and the subsequent Delete server files flow may rename and recursively remove its live server directory. This is distinct from Rust migration and should receive a separate narrowly scoped safety fix plus regression tests: with tmux unavailable and a matching unmanaged JVM present, both unregister and destructive deletion must be blocked; with a complete process scan and no match, behavior can remain safely available. Also test a process-scan-unavailable state - an incomplete `/proc` observation must not be silently treated as an empty process list for destructive decisions.

The current `_unmanaged_pids()` helper catches every exception and returns `[]`; `_iter_pids()` also returns an empty list when `/proc/self/stat` is not visible. That best-effort behavior may be tolerable for a non-authoritative metrics display, but it is not a sound proof of process absence for destructive preflight. A future API should distinguish `complete scan with no match`, `matching PIDs`, and `incomplete/unavailable scan`. Keep metrics best-effort while requiring complete observations for safety actions.

### 2.4 Importers and pack-update domain

- `prism.py`: Prism/MultiMC detection, archive probing/extraction, path validation, import receipts, copy/staging.
- `serverloader.py`: Minecraft/loader version selection and external installer execution for Vanilla, Fabric, Forge, NeoForge, Quilt.
- `modpack_auto.py`: Modrinth mrpack extraction, override application, install orchestration.
- `downloader.py`: download-index parsing, bounded/cancellable network downloads, hash verification, safe destination mapping.
- `pack_update.py`: GTNH identity/version parsing, update plan, pack download/stage/apply, preserved data copy, receipt, rollback and previous-install pruning.

`plan_update()` is a useful example of a good seam: it is explicitly intended to build a reviewable plan without touching the filesystem and accepts a tri-state running observation. A Rust implementation could eventually mirror pure plan classification, but it should not own the actual staged update until compatibility fixtures and rollback parity exist.

### 2.5 Export, mods, and sharing

- `exporters/client_export.py`: client profile and mod inspection, deterministic input fingerprints, server/client mod-set reconciliation, curated distribution review, archive writing, privacy scanning, manifest sealing and final verification.
- `mods/mod_manager.py`: local JAR listing and metadata inspection, enable/disable/delete/add.
- `mods/modrinth.py`: Modrinth API search, version resolution, dependency handling, file downloads and install records.
- `mods/curseforge.py`: guarded legacy/provider implementation; online integration is disabled by current conservative policy.
- `mods/versions.py`: selected-version/dependency file resolution and replace-file operation.
- `mods/distribution_catalog.py`: bounded parse and exact-SHA catalog lookup.
- `mods/icon_fetch.py`: provider-scoped URL validation, redirects, bounded icon fetching.
- `sharing/gofile.py`: upload flow, streamed file handling, cancellation, response validation and checksums.
- `sharing/history.py`: local validated share history and atomic persistence.
- `sharing/provider.py`: upload provider protocol.

Keep these steps distinct: inventory, review, archive build, archive verification, user approval, upload, and link/history publication. A fast archive builder must not bypass distribution/privacy gates or make upload implicit.



### 2.5.1 Network/provider trust boundaries

Crucible does not have one generic HTTP client today. Its network paths have different policies and should not be consolidated without parity tests:

- `download_http.py` builds a TLS-verifying opener, requires HTTPS without embedded URL credentials, rejects API-key query parameters, strips caller-supplied `x-api-key`, and attaches the user's CurseForge key only to an explicit allowlist of official API/CDN hosts. Its redirect handler rejects cross-host CurseForge redirects rather than forwarding credentials.
- `curseforge.py` retains a provider adapter but hard-disables online integration through `_CURSEFORGE_ONLINE_ENABLED = False`. If re-enabled later, the key must come from the session, environment, or a regular user-owned private key file (mode restricted, no symlink, size-bounded). A Rust port must not silently change this policy.
- `modrinth.py` bounds JSON responses (16 MiB), icon responses (8 MiB), and mod downloads (2 GiB); uses finite request timeouts; resolves exact loader/Minecraft version compatibility; stages downloads to temporary files; and checks a SHA-1 when supplied before replacing the destination.
- `importers/downloader.py` handles indexed pack downloads separately, with cancellation checks, size limits, temporary files, retry/backoff for transient failures, and SHA-512/SHA-1 verification when an expected digest is available. Explicit HTTP denial does not trigger futile alternate-URL retries.
- `importers/serverloader.py` has another installer download path with its own TLS context, finite response/file limits, cancellation and retry behavior, and atomic destination replacement. Do not assume it is behaviorally identical to Modrinth download handling.
- `sharing/gofile.py` has a distinct upload path: it opens the selected ZIP without following symlinks, checks file identity before hashing, validates the SHA-256 against the reviewed artifact, streams from the same file descriptor, and validates the provider's returned URL and metadata. It does not accept an arbitrary URL from the response as a share link, and its optional API token is not persisted or returned.

Migration consequence: any new Rust network adapter must preserve per-provider TLS/root-store behavior, host allowlists, redirect policy, timeouts, body limits, cancellation, retry semantics, hash algorithms, atomic publication and credential redaction. Replacing several Python paths with one convenient HTTP crate is not automatically a correctness-preserving refactor.

### 2.5.2 Existing filesystem mutation owners

The main file-mutating operations are already spread across domain modules and have different rollback contracts:

- Registry publication: `InstanceManager._write_instances()` writes a complete snapshot, flushes/fsyncs the temporary file and atomically replaces `instances.json`; external modification and parse errors block writes.
- Mod inventory changes: `ModManager` controls top-level JAR add/enable/disable/delete; `mods/versions.py` stages compatible files and manages replacement records.
- Server pack update: `importers/pack_update.py` creates a plan first, then stages/downloads/applies the pack while preserving world/config/owner data and retaining rollback/receipt material.
- Configuration repair: `data/properties.py` validates server properties and saves a backup before repair; `ServerInstance.set_eula_accepted()` writes `eula.txt` atomically.
- Loading repair: `diagnostics/loadcheck.py` separates crash analysis from opt-in JAR quarantine/restore operations.
- World operations: `BackupManager` owns legacy world swap/reset/wipe and pre-swap directories; `FleetBackupManager` owns immutable-generation restore and retention cleanup.
- Import: `prism.py` creates staged target content, import receipts and generated setup files; optional server/mod installation occurs after the core source copy.
- Export/share: `client_export.py` writes a temporary archive then publishes it after verification; `gofile.py` uploads the reviewed artifact without modifying the server folder.

A Rust migration must not create duplicate writers for any of these data classes. For each operation, define one owner, the source/destination paths, preconditions, staging location, publication operation, backup/receipt, failure recovery, and whether cancellation is safe before/after publication. A Rust helper should initially return plans/results or perform a single isolated primitive, not take ownership of whole workflows.

### 2.6 Diagnostics

`diagnostics/loadcheck.py` parses logs/crash reports and inspects JAR metadata, maps reported mod IDs back to JARs, and can quarantine/restore selected files. Parsing/diagnosis and filesystem mutation should remain separate operations. The Rust core may eventually help with a pure parser, but a quarantine/restore operation is not a suitable first port.

### 2.7 Qt application and worker surface

The GUI is divided into:

- Top-level workspace: `main_window.py`, `instance_panel.py`, `sidebar.py`, `system_tray.py`.
- Task dialogs: add/import, add-mod, create-server, modpack, download, client export, pack update, player sharing, properties and shortcuts.
- Tabs: Console, Config, Mods, Notes, Info, Backup, Worlds, Players, Setup, System.
- Supporting modules: theme, icons, server tinting, thread lifecycle helper, pack-update presentation model.

Worker classes are generally small Qt adapters that call the existing synchronous domain function and emit a result/error signal. Examples observed include:

- MainWindow's persistent health worker calls `TmuxManager.status_map()`.
- InstancePanel owns persistent log-watcher and watchdog threads, and uses a worker for tmux actions/world-start inspection.
- BackupTab worker runs fleet backup, verification, restore, or scheduler tasks off the GUI thread.
- WorldTab workers perform world-size scans and checkpointed swap/reset/wipe/cleanup.
- Add/import, mod search/icon/resolve/install, modpack install, server creation/installation, client export, downloads, pack update, player avatar, and share upload each use bounded Qt worker patterns.

The shared `connect_thread_cleanup()` waits until the native QThread has actually stopped before releasing feature state. This exists because dropping a Python QThread wrapper after a timed wait caused a real process-abort class of bug. Any future FFI callback or Rust worker must preserve QObject/thread lifetime guarantees, not merely return the right value. Not every operation uses a permanent worker: `_run_tmux()` creates a short-lived QThread per command; the main health, log-watcher and watchdog workers persist; world-start inspections use tracked short-lived QThreads; Copy IP uses a daemon Python thread. These lifetimes and frequencies should be preserved/benchmarked individually rather than replaced by one generic Rust worker.

**Important distinction:** tabs defer some expensive *data loading* until first view, but `ui/tabs/__init__.py` imports all ten tab modules at module import time, and `instance_panel.py` imports the tab package. The InstancePanel then constructs eight tab widget instances immediately in `_build_ui()` and adds them to the QTabWidget; it does not lazily instantiate a widget when the tab is first selected. Thus neither tab-module imports nor widget construction are lazy. A meaningful startup optimization would have to defer both class imports and widget construction while retaining the current page-navigation, object-lifetime, and worker semantics. Measure before taking on that UI refactor.



## 2.8 Import-order and dependency-cycle observations

A line-level import audit distinguishes the following:

- `download_http.py` imports the provider adapters only inside request construction paths. `curseforge.py` imports the Modrinth adapter at module load, but imports shared `download_http` request helpers only inside its request function. Modrinth also imports download helpers, CurseForge, and `versions.py` inside the functions that need them; `versions.py` imports Modrinth at module load and imports CurseForge only inside provider-specific version functions. This is a deliberately deferred dependency knot, not evidence of a top-level `download_http ↔ curseforge ↔ modrinth ↔ versions` import-time cycle.
- `serverloader.py` imports Prism only inside the fresh-server workflow; `prism.py` imports pack-update detection inside a detection helper and imports serverloader only when optional server installation is requested. `pack_update.py` imports Prism's archive extraction helper locally inside the apply-update path (with an explicit comment that this avoids a cycle).
- Package facades intentionally use lazy attributes for selected exports: `crucible.data.BackupManager`, `crucible.process.LogWatcher`, and `crucible.process.Watchdog`.

These local imports reduce module initialization coupling but make first-use latency and import order path-dependent. A Rust migration should not flatten these dependencies into a single giant crate or move provider-specific logic into core types. Capture import-time and first-use profiles for the actual command path before trying to normalize them. Preserve lazy boundaries where they prevent optional Qt/network/provider modules from loading for unrelated CLI operations.

## 3. Important end-to-end flows

### 3.1 GUI startup and health status

1. `crucible gui` dispatch imports MainWindow and Qt.
2. MainWindow constructs InstanceManager, TmuxManager, persistent health thread, sidebar and InstancePanel.
3. The sidebar starts as Checking; the first health request is sent immediately.
4. A 5-second QTimer schedules status refreshes; `_health_busy` prevents overlapping requests.
5. `TmuxManager.status_map()` obtains the tmux session list once for the full instance list.
6. A registered session yields Running. For instances without a session, the code checks for an unmanaged matching Java process, then missing folder, then Stopped. Unknown query errors remain Unknown.
7. MainWindow applies one status map to the sidebar, selected panel and tray. The tray does not own another polling loop.

Performance implication: the session list is already batched. The remaining repeated discovery is per-instance unmanaged-process detection after a session miss. `machine_resources.py` contains a separate one-pass aggregate JVM scan; a shared per-instance PID map is a promising Python refactor to benchmark.

### 3.2 Server lifecycle

Start/stop paths use the shared TmuxManager, exact instance/session targets, state confirmation and action-time revalidation. Start includes world-selection inspection and may require an owner confirmation. LogWatcher recognizes the actual Minecraft ready/stopping lines. Watchdog coordination is driven through queued Qt signals. Heartbeat ordering prevents intentional Stop from being interpreted as a crash.

Do not split this state machine between Python and Rust. If a Rust core is used, it should initially return a pure eligibility/classification result from a caller-supplied snapshot; Python remains the only layer issuing tmux commands and presenting user confirmation.

### 3.3 Fleet backup

BackupTab and WorldTab dispatch to FleetBackupManager in workers. The manager serializes backup/world/update operations with a global operation lock, snapshots the full registered fleet, checks process status/readiness, flushes running worlds, stages immutable generations, verifies, publishes by atomic rename/fsync, rotates retention only after successful publication, and defers source cleanup until retained generations are safe.

Incremental behavior is already significant: unchanged content can be hard-linked from the preceding immutable generation, avoiding fresh reads/writes. Do not replace this with an always-full copy or an archive format change under the guise of a Rust port.

### 3.4 Import and pack update

Import paths detect source type and build a plan, extract archives into staging on the destination filesystem when possible, reject unsafe paths/links/oversized entries, copy server-safe content, add metadata/receipt, optionally download indexed mods and install server software, and surface warnings. GTNH pack updates first create a reviewable plan, then separately stage/download/apply, preserve world/config/mod inputs, verify, and keep rollback.

The extraction path intentionally uses the destination parent as the temp directory so a multi-GiB pack is not expanded under a small tmpfs `/tmp` and a second cross-device copy can be avoided. This is a performance feature worth preserving.

### 3.5 Client export and share

The client export pipeline may read the same JAR/config inputs multiple times for different guarantees: metadata inspection, source fingerprinting, distribution SHA-256 review, archive creation, sealed-member hashing/privacy inspection, archive SHA-256 and final source/catalog revalidation. This is expensive by design in places because the exported bytes must match the bytes reviewed and the input set must remain stable.

This is a benchmark candidate, not an invitation to drop checks. Future optimization could consider a verified staged input snapshot with a single digest inventory reused by review and build, but only if it still detects source changes and ties every catalog decision to the exact bytes embedded in the ZIP. Include adversarial mutation and duplicate/unknown JAR cases in parity tests.

## 4. Performance candidates confirmed by source

These are source observations, not measured benchmark results.

| Candidate | Source observation | Hypothesis / next measurement |
|---|---|---|
| Health process discovery | Status map batches tmux sessions but calls per-instance unmanaged PID discovery on session misses; another module has a fleet-wide scan | Compare existing paths with one per-instance association pass at different server/process counts |
| GUI cold start | All ten tab modules are imported through the tabs package when InstancePanel imports it | Record `-X importtime`, cold launch duration, module RSS; consider import-lazy tabs only if it makes a measurable difference |
| Client export | Fingerprinting hashes eligible files; Prism flow can recompute the fingerprint after building; JAR review/build/seal each perform integrity-related reads | Measure total bytes read, CPU, time and peak RSS with representative GTNH and many-small-file fixtures |
| Fleet backup | Fresh copies are streamed+hashed and destination is re-read for checksum; each file flushes/fsyncs; tree inventory is repeated and source stat checked | Separate small-file metadata/fsync cost from large-file streaming and verification cost |
| World statistics | World-size and start-summary traversal is potentially large but dispatched to workers | Measure worker duration, file count, GUI responsiveness, and how frequently users actually open/refresh the view |
| Resource metrics | On-demand sampling waits 350 ms for CPU delta and may run bounded `nvidia-smi`; result is cached | Measure only user-triggered latency and idle wakeups; do not add a recurring sampler |
| Archive import | Bounded streaming extraction stages on destination filesystem, avoiding tmpfs and cross-device copy | Retain this property; profile decompression and file creation separately before considering another archive library |

Potential Python-side improvements should be measured first. A faster algorithm and fewer repeated passes can outperform a language rewrite, particularly when operations are constrained by disk throughput, fsync, process startup or external API latency.

## 5. Benchmark and profiling discipline

The current CI runs tests on Python 3.11 and 3.13 on Ubuntu 24.04, an offscreen Qt smoke run, and UI preview capture. It does not currently build or test Rust and does not run a performance benchmark suite.

Use profiling/benchmark tooling compatible with the existing Python matrix:

- `python -X importtime` for import self-time/cumulative-time analysis. Python documents that it is available from Python 3.7; its multi-threaded output can be misleading, so use it for controlled cold-import runs.
- `cProfile` for deterministic call counts and CPU attribution on Python 3.11/3.13. Do not treat profiler output as benchmark timing.
- `time.perf_counter_ns()` or `timeit` for repeated timing of isolated functions; for end-to-end workflows capture wall time, CPU time, bytes read/written, peak RSS and subprocess count.
- Native Linux tools such as `perf stat` / `perf record` for syscall, CPU and I/O attribution, where permissions and kernel configuration allow.
- When Rust is introduced, use release builds for runtime comparisons, `cargo bench`/Criterion-style harnesses for isolated CPU code, and a native profiler for hot-path attribution. Keep debug symbols in benchmark builds. Record compile/link time and binary size as separate product costs.

Do not compare Python debug execution to optimized Rust release binaries. Do not compare warm-cache runs with cold-cache runs. Repeat on the same filesystem and device. Record p50/p95, not only the best single result. Measure total workflow latency, including FFI/IPC, serialization, input hashing, UI handoff and disk flushes.

### External technical references

- Python command-line documentation for `-X importtime`: https://docs.python.org/3/using/cmdline.html
- Python profiler documentation for cProfile and the difference between profiling and timing: https://docs.python.org/3.14/library/profile.html
- Python `timeit` documentation: https://docs.python.org/3/library/timeit.html
- Rust performance benchmarking guidance: https://std-dev-guide.rust-lang.org/development/perf-benchmarking.html
- Microsoft Rust guidance to identify/profile hot paths and benchmark before optimizing: https://microsoft.github.io/rust-guidelines/guidelines/performance/
- Rust codegen option trade-offs (optimization level, codegen units, LTO, compile time and output size): https://doc.rust-lang.org/rustc/codegen-options/
- PyO3 current user guide, including detaching Rust-only work from the Python interpreter: https://pyo3.rs/main/
- Maturin packaging and Python binding guide: https://www.maturin.rs/

The Rust guidance explicitly recommends prioritizing code with material real-world cost and cautions that shaving overhead from a syscall-dominated operation may yield less benefit than optimizing a true hot path. Apply that principle to Crucible's subprocess- and storage-heavy workload.

## 6. Crosshair Rust branch: current reference snapshot

The owner's `Roaring1/Crosshair:rust-port` branch was rechecked during this audit:

- Rust edition 2024 and declared `rust-version = "1.92"`.
- Library crate named `crosshair_core`, separate CLI binary, and optional Settings binary behind `settings-ui`.
- Cargo default features remain empty; UI dependencies are not required for the core build.
- Uses serde/serde_json and thiserror, target-gated Linux/Windows dependencies.
- Release profile currently favors small binaries: `opt-level = "s"`, thin LTO, one codegen unit, stripping and abort-on-panic.
- README still labels this as a development branch, not the published stable release; it is explicit about tested vs unclaimed platform behavior.

The separation pattern is useful. The release profile is not automatically the right choice for Crucible: Crosshair targets a small on-demand overlay, while Crucible has server-management, large-file and archive workloads. Rust documents that codegen-unit and LTO settings trade compile/link time against runtime/code size. Benchmark Crucible's actual release profile rather than copy settings based on another project's use case.

## 7. Packaging and release constraints

Current installation behavior observed in `install.sh`:

- Chooses Python 3.11+ and prefers an interpreter that already imports PyQt6.
- Creates a per-user venv using `--system-site-packages`.
- Copies source into a staging directory, runs `compileall`, imports PyQt6/Crucible, and invokes `crucible --help`.
- Publishes by rename, retains rollback copy until final-path smoke check, creates per-user desktop integration.
- The one-line installer verifies exact source/checksum release assets and validates ZIP paths, links, expansion limits and CRC before invoking install.sh.
- No Rust toolchain is currently required from the owner at installation time.

Therefore, a production Rust component must be delivered as a verified prebuilt artifact or compatible wheel. Do not make every user install Rust or compile the application during normal updates. The build pipeline must produce, include, verify, stage and smoke-test the Rust artifact before publishing the new install, with rollback if it is missing or incompatible.

If PyO3 is selected, measure wheel size/build matrix and Python 3.11/3.13 compatibility. PyO3's newest stable-ABI features and Python 3.15/free-threaded support are not a reason to raise Crucible's runtime requirement. If a JSON worker is selected, keep it long-lived and batch meaningful work rather than paying process startup on every status check.

## 8. Current tests and behavioral coverage

The repository tree contains 62 Python test/support files. Relevant named suites include:

- Lifecycle and crash recovery: `test_crash_recovery.py`, `test_watchdog_stop_coordination.py`, `test_thread_lifecycle_safety.py`, `test_readiness_placeholder_detection.py`, `test_startup_patterns.py`.
- tmux/process/resource: `test_machine_resources.py`, `test_resource_monitor.py`, `test_port_conflicts.py`, `test_log_tail.py`, `test_tray_shared_status.py`.
- Backups/world safety: `test_fleet_backup.py`, `test_world_backup_swap.py`, `test_world_recovery_worker.py`, `test_world_start_summary.py`, `test_world_wipe_confirmation_qt.py`, `test_reset_wipe_and_memory.py`.
- Import/update: `test_prism_import_worker.py`, `test_prism_prebuilt_server_import.py`, `test_pack_update.py`, `test_pack_update_bugfixes.py`, `test_pack_update_sources.py`, `test_modpack_installer_safety.py`.
- Export/share/provider: `test_client_export.py`, `test_client_export_review.py`, `test_gofile_upload.py`, `test_share_history.py`, `test_source_page_url_guard.py`, `test_mod_distribution_catalog.py`.
- Qt/UI: `test_refinement.py`, `test_qt_slot_contracts.py`, `test_setup_responsive_layout_qt.py`, `test_share_players_dialog.py`, `test_system_tray.py`, plus `qt_refinement_smoke.py` and preview tests.

The workflow creates a private temporary tmux socket directory. Do not point Rust integration tests at the user's normal tmux socket, installed app, server registry, or server root. Use generated temporary roots and fixture-only process snapshots.



### 8.1 Coverage strengths and migration gaps

The existing Python tests already form a useful reference for several safety contracts:

| Contract area | Existing coverage evidence | What the Rust migration still needs |
|---|---|---|
| Registry and identity | `test_core.py` covers duplicate sessions, path/session validation, unknown/malformed registry writes, external-change reconciliation, delete guards, and Java argument handling | Sanitized golden registry rows that explicitly test every known/unknown key round-trip through Rust |
| Process state/lifecycle | `test_core.py` covers exact tmux targets, unknown timeouts, unmanaged Java, wrapper survival, graceful-only Stop, and action-time removal checks | A shared per-instance PID association contract, fixture parity, and performance tests with scaled fake process tables |
| Heartbeats | `test_crash_recovery.py` covers all four states, atomic writes, same/different boot, unknown probes, and interrupted Stop with Java/wrapper distinctions | Golden heartbeat JSON and pure state-transition parity tests |
| World start | `test_world_start_summary.py` covers existing/new/incomplete saves, alternate saves, selection change, traversal, symlink and modded dimension detection | More malformed inventory/permission fixtures and direct pure-classifier fixtures separate from filesystem scan tests |
| Properties | `test_properties_validation.py` covers legacy GTNH 1.7.10 keys, modern-version removals, unknown versions, loader inference, Java property escapes and semantically unsafe guesses | Text-level parser/validator fixtures with ordering, duplicate keys, comments, escapes and version matrix |
| Pack update | `test_pack_update.py` covers version parsing, variant policy, world preservation, running/unknown blockers, downgrade warnings, insufficient disk, repeated live-state checks, rollback and cancellation | Serialized update-plan golden fixtures and parity on warnings/blocker reason codes |
| Client export/share | `test_client_export.py` and `test_client_export_review.py` cover archive membership, source hash changes, exact SHA decisions, privacy checks, symlinks, duplicate mods and prohibited JARs | Golden sealed-manifest fixtures and a source-mutation parity corpus; do not benchmark by dropping review passes |
| Fleet backup/world swap | `test_fleet_backup.py` and `test_world_backup_swap.py` cover incremental reuse, immutable generations, fsync/publication failures, unknown/running refusal, symlinks, archive safety, rollback and restore | Golden schema-1 manifests and fault-injection fixtures before any backup core is moved |
| General security boundaries | `test_security_boundaries.py` covers registry shapes, last-key-wins properties, bounded crash logs/JAR metadata, uncertain probes, path escapes and ZIP limits | Reuse identical malicious/oversized fixture files against both languages |

Some representative test files contain dozens of focused cases (for example, 83 tests in `test_pack_update.py`, 57 in `test_core.py`, and 29 + 21 in the client-export/archive-review suites). The suite is not merely smoke testing; it includes behavioral and failure-path coverage.

Two important limits remain:

1. The current CI workflow runs Python 3.11 and 3.13 tests, Qt smoke and UI preview. It does **not** run Cargo checks/tests or a repeatable performance benchmark suite.
2. Existing tests protect current Python behavior. They are not yet a cross-language parity harness and do not prove that a Rust replacement returns identical output, errors, ordering or side-effect boundaries.

For the candidate procfs optimization specifically, `test_machine_resources.py` confirms exact session-marker matching and excludes false Java matches from non-Java commands, while `test_resource_monitor.py` covers per-instance marker and Java/path matching. A new batch association map needs explicit multi-instance tests: marker-to-ID association, legacy Java fallback, overlapping paths, empty/inaccessible `cmdline`, process exit during scan, and no cross-instance PID attribution. Performance tests should use controlled fake procfs or helper processes, not the owner's live server process table.

## 9. Full Python source inventory

The following list was generated from the recursive tree at the recorded `main` baseline.

### `crucible/`

`__init__.py`, `__main__.py`, `desktop_shortcuts.py`, `download_http.py`

### `crucible/data/`

`__init__.py`, `backup_manager.py`, `backup_scheduler.py`, `fleet_backup.py`, `instance_manager.py`, `instance_model.py`, `netinfo.py`, `properties.py`, `server_icons.py`, `world_start_summary.py`

### `crucible/diagnostics/`

`__init__.py`, `loadcheck.py`

### `crucible/exporters/`

`__init__.py`, `client_export.py`

### `crucible/importers/`

`__init__.py`, `downloader.py`, `modpack_auto.py`, `pack_update.py`, `prism.py`, `serverloader.py`

### `crucible/mods/`

`__init__.py`, `curseforge.py`, `distribution_catalog.py`, `icon_fetch.py`, `mod_manager.py`, `modrinth.py`, `versions.py`

### `crucible/process/`

`__init__.py`, `command_intent.py`, `crash_recovery.py`, `log_tail.py`, `log_watcher.py`, `machine_resources.py`, `netfix.py`, `resource_monitor.py`, `startup_patterns.py`, `tmux_manager.py`, `watchdog.py`

### `crucible/sharing/`

`__init__.py`, `gofile.py`, `history.py`, `provider.py`

### `crucible/ui/`

`__init__.py`, `add_dialog.py`, `add_mod_dialog.py`, `client_export_dialog.py`, `download_dialog.py`, `icons.py`, `instance_panel.py`, `main_window.py`, `modpack_dialog.py`, `new_server_dialog.py`, `pack_update_dialog.py`, `pack_update_model.py`, `properties_dialog.py`, `server_tint.py`, `share_history_dialog.py`, `share_players_dialog.py`, `shortcuts_dialog.py`, `sidebar.py`, `system_tray.py`, `theme.py`, `thread_lifecycle.py`

### `crucible/ui/tabs/`

`__init__.py`, `backup_tab.py`, `config_tab.py`, `console_tab.py`, `info_tab.py`, `mods_tab.py`, `notes_tab.py`, `players_tab.py`, `setup_tab.py`, `system_tab.py`, `world_tab.py`

### `crucible/utils/`

`__init__.py`, `term.py`

## 10. Test/support file inventory

`tests/conftest.py`, `tests/qt_refinement_smoke.py`, `tests/test_add_mod_icons.py`, `tests/test_add_mod_icons_qt.py`, `tests/test_backup_scheduler.py`, `tests/test_backup_ui.py`, `tests/test_ci_report.py`, `tests/test_client_export.py`, `tests/test_client_export_dialog_source.py`, `tests/test_client_export_review.py`, `tests/test_core.py`, `tests/test_crash_recovery.py`, `tests/test_curseforge_downloads.py`, `tests/test_curseforge_fallback.py`, `tests/test_desktop_shortcuts.py`, `tests/test_fleet_backup.py`, `tests/test_fml_gui_start.py`, `tests/test_gofile_upload.py`, `tests/test_local_svg_icons.py`, `tests/test_log_tail.py`, `tests/test_machine_resources.py`, `tests/test_mod_distribution_catalog.py`, `tests/test_modpack_installer_safety.py`, `tests/test_pack_update.py`, `tests/test_pack_update_bugfixes.py`, `tests/test_pack_update_sources.py`, `tests/test_pack_update_ui.py`, `tests/test_player_avatar_states_qt.py`, `tests/test_port_conflicts.py`, `tests/test_prism_import_worker.py`, `tests/test_prism_prebuilt_server_import.py`, `tests/test_properties_validation.py`, `tests/test_qt_slot_contracts.py`, `tests/test_readiness_placeholder_detection.py`, `tests/test_refinement.py`, `tests/test_release_scripts.py`, `tests/test_reset_wipe_and_memory.py`, `tests/test_resource_monitor.py`, `tests/test_security_boundaries.py`, `tests/test_server_icons.py`, `tests/test_setup_responsive_layout_qt.py`, `tests/test_setup_scroll_keyboard_qt.py`, `tests/test_share_history.py`, `tests/test_share_history_dialog.py`, `tests/test_share_players_dialog.py`, `tests/test_shortcut_action.py`, `tests/test_shortcut_notifications.py`, `tests/test_source_page_url_guard.py`, `tests/test_startup_patterns.py`, `tests/test_status_accessibility.py`, `tests/test_system_tray.py`, `tests/test_thread_lifecycle_safety.py`, `tests/test_tray_main_window.py`, `tests/test_tray_shared_status.py`, `tests/test_ui_preview_capture.py`, `tests/test_watchdog_stop_coordination.py`, `tests/test_world_backup_swap.py`, `tests/test_world_recovery_worker.py`, `tests/test_world_start_summary.py`, `tests/test_world_stats_thread_guard.py`, `tests/test_world_wipe_confirmation_qt.py`, `tests/tmux_isolation.py`

## 11. Current Phase 0 gaps

The source inventory is now complete by file path, principal runtime flows and the largest deferred dependency knots have been traced. Remaining discovery before choosing production Rust scope:

1. Finish per-symbol call-site mapping for every candidate module, especially the UI-to-worker edges and all import/export/backup callers.
2. Build sanitized, representative fixtures for registry, heartbeat, lifecycle observations, world-start summaries, fleet manifests, import receipts, export manifests, catalog records, and provider responses.
3. Record real baseline timings on the owner's normal hardware, without using or modifying a live GTNH server/world.
4. Decide whether the first measured optimization should be a Python algorithm refactor (likely first for shared process discovery) or a standalone Rust CPU-heavy primitive.
5. Prototype packaging/rollback in CI before any production Rust call site is enabled.

No benchmark numbers are claimed in this document. The source observations above are hypotheses to validate, not permission to remove safety checks.
