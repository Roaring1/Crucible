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

Top-level imports include the instance manager/model, world-start inspection, crash heartbeat, tmux manager, and common CLI formatting. Several heavier import/installer/diagnostics modules are imported inside the relevant handler. The Qt GUI imports PyQt and the UI package only in `cmd_gui`; CLI commands do not initialize Qt. However, after argparse has selected a valid command, `main()` always constructs and loads `InstanceManager` and constructs `TmuxManager` before dispatch, including for standalone archive probes and version-list requests. `--help` exits in argparse before that work; no-argument invocation prints the banner/help and exits successfully before loading the registry. This is a measurable CLI startup/import boundary, not currently a benchmark result.

The actual `argparse` tree currently registers 32 subcommands (the leading file docstring is only a short, older subset). The command names are:

`gui`, `list`, `check-updates`, `update`, `rollback`, `prune-previous`, `probe-pack`, `add`, `remove`, `start`, `fix-properties`, `fix-loading`, `stop`, `shortcut-action` (hidden), `restart`, `status`, `attach`, `send`, `scan`, `scan-prism`, `import-prism`, `create-server`, `install-modpack`, `list-mc-versions`, `install-server`, `download-mods`, `accept-eula`, `doctor`, `validate`, `backup-fleet`, `info`, and `edit`.

Notable option contracts include explicit `stop --force` for no-save process termination, graceful `stop --timeout` and `restart --timeout`, `update --dry-run`/prerelease/downgrade/source/variant/preserve-previous choices, `rollback --keep-current-world`, `import-prism --overwrite`/EULA/download/install flags, and mutually exclusive fleet backup `--list`, deep `--verify`, `--install-timer`, and `--remove-timer` modes. `shortcut-action` accepts only start/stop and an instance ID; it is hidden from normal help and deliberately has graceful-stop-only behavior in its dispatcher.


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

The current `_unmanaged_pids()` helper catches every exception and returns `[]`; `_iter_pids()` also returns an empty list when `/proc/self/stat` is not visible. That best-effort behavior may be tolerable for a non-authoritative metrics display, but it is not a sound proof of process absence for lifecycle safety. The call sites include `is_server_ready()` (which gates save/backup consistency), `get_status()` and `status_map()` (which can classify an unmanaged live JVM as stopped), `safe_to_remove()` (which gates registry removal and folder deletion), `stop()`/`attach()` (which refuse to control an unmanaged process), and `_force_kill()` (which uses an empty PID list after `tmux kill-session` as proof the JVM died, and again after SIGTERM/SIGKILL as proof escalation succeeded). When process discovery is incomplete, `_force_kill()` can falsely report success while a JVM survives, and status/backup paths can treat an incomplete observation as stopped. A future API should distinguish `complete scan with no match`, `matching PIDs`, and `incomplete/unavailable scan`. Keep metrics best-effort while requiring complete observations for safety actions. For explicit force-kill, report that process exit could not be verified; never declare success from an incomplete scan.


**Separate high-priority safety observation - launch when a server is already running outside tmux:** the same incomplete process-discovery contract affects Start, not only removal. TmuxManager.start() does not call _unmanaged_pids() or require a complete process scan. Its current guards are: tmux must be installed; the exact configured tmux session must not be positively running (via the boolean is_running() wrapper); a start script must exist; and _port_conflict_message() checks whether an IPv4 listener overlaps the port/address currently read from this instance's server.properties. That last check is useful, but it is not proof that no matching Java server process exists: it only examines the configured IPv4 bind/port and may not identify a process that is still starting, uses an unparsed bind address, or has a different effective bind than the registry/config expects.

The launch callers do not all add a fresh, identity-aware process check:
- CLI cmd_start() performs world inspection and property preflight, then calls tmux.start() directly; it does not call get_status() first.
- GUI InstancePanel._begin_start_after_world_check() relies on the cached _current_status == "stopped" value, then calls tmux.start(). The UI status came from the periodic health path and can be stale; it is not a process-absence proof at the final action boundary.
- Hidden desktop shortcut-action start obtains a fresh get_status() and only continues for stopped, but that still inherits the best-effort _unmanaged_pids() behavior that collapses errors to an empty list.
- Watchdog._do_restart() re-inspects world safety and then calls tmux.start() without a process-absence scan. It relies on prior tmux/pane misses and the shared start method.
- CLI cmd_restart() is more defensive: it checks get_status() and rejects any state except running or stopped, but its result still inherits the lossy procfs semantics.

Consequently, a manual Java server outside the registered tmux session can be missed by the CLI, GUI race window, shortcut (when the scan is incomplete), or watchdog restart. The configured-port check may catch common conflicts, but it does not establish process identity/absence. A Start path that proceeds can create a new tmux session and launch a second JVM against the same files. Treat this as a separate high-priority safety prerequisite alongside removal: before a launch, require a complete process observation and block if any PID is associated with this instance; if the scan is incomplete, return Unknown and do not launch. Revalidate at the shared TmuxManager.start() action boundary, not just in individual front ends. Keep the existing port-conflict check as an additional guard, not a substitute.

Existing Start regression coverage is insufficient for this contract. StartTests.test_start_wrapper_has_no_unbounded_tee patches is_running=False and _port_conflict_message=None, then expects launch success; it does not assert any unmanaged-process scan. Watchdog security tests cover the reboot wrapper still owning its tmux session (where TmuxManager.start() sees the session and refuses a competing launch), but not a detached/unmanaged Java process after the session disappears. Add shared-start tests for a matching process, an incomplete procfs scan, tmux session absent plus unmanaged Java, and complete no-match; add caller-level checks for CLI, GUI, shortcut and watchdog dispatch. Do not change this behavior as part of a performance batching or Rust migration patch.



### 2.3.2 Watchdog polling: implementation does not match its batch comment

The module docstring in `watchdog.py` says one `tmux list-sessions` call per poll covers all watched instances. The current `Watchdog._poll()` does not use that operation:

- It reads each watched instance's heartbeat JSON inside the loop.
- For each ordinary `running` heartbeat, it calls `probe_running(instance)`, which launches `tmux has-session -t =<session>`.
- When the session is present, it calls `is_java_foreground(instance)`, which launches `tmux list-panes -t =<session>:`.
- A pending `stop_requested` heartbeat also probes the exact session, and queries the pane while the wrapper remains present.

This is roughly two tmux client subprocesses per watched instance on a normal 10-second poll, not one list-sessions command total. The 10-second timer is demand-driven and stops when no watch or pending Stop remains; do not replace that with an always-running poller.

A batched adapter is plausible: one `list-sessions` observation, followed by one `list-panes -a -F` snapshot when relevant sessions exist. The tmux manual documents `list-panes -a` as listing all panes on the server and `-F` as a format for each line; formats include `session_name`, `window_active`, `pane_index`, `pane_active`, and `pane_current_command` (see https://man.archlinux.org/man/tmux.1.en and https://github.com/tmux/tmux/wiki/Formats). The prototype must map the batch rows to the same active-window/first-pane behavior that the current per-target query returns. Simply taking the first arbitrary pane or the active pane could silently change crash detection. Keep unknown query/format output distinct from Java-not-foreground.

Current tests cover unknown probes, wrapper survival, two-miss confirmation, pending Stop, crash loop limits and world-selection confirmation, but set up one watched instance at a time. There is no multi-instance query-count test, global pane snapshot fixture, or parity test for multiple windows/panes. Add these before changing the watchdog implementation.

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



### 2.8.1 External network/provider adapters and a redirect caveat

The external I/O surface is intentionally split across multiple modules rather than one common client:

- `download_http.py` is the security-aware shared request/redirect layer used by indexed mod downloads and Modrinth JAR installs. It rejects non-HTTPS input URLs, credentials embedded in URL userinfo, unsafe ports and API-key query parameters. CurseForge authentication is attached only for its exact host set and is currently disabled before the key file is read. Authenticated/CurseForge redirects cannot leave the approved CurseForge hosts; generic requests are rebuilt without credentials.
- `downloader.py` uses that shared layer for modpack-index downloads. It has 64 KiB chunks, cancellation checks, 2 GiB per-file limits, 3 retry attempts, SHA-512 or SHA-1 verification where supplied, and same-directory temporary-file publication. A file with no provider hash is accepted only if non-empty, so do not silently change hash-absence semantics during a port.
- `modrinth.py` has API JSON requests capped at 16 MiB and a 12-second timeout. Its real mod download path uses the shared request opener and a 2 GiB limit. It still contains a separate `fetch_bytes()` utility that calls default `urlopen`; the current Add Mod dialog's icon worker instead calls the stricter `icon_fetch.py` path.
- `icon_fetch.py` is intentionally separate: only `cdn.modrinth.com` is enabled, redirects stay within the provider host allowlist, no credentials are sent, 8 MiB body cap, 8-second socket/deadline cap. CurseForge icon previews are blocked in code, not merely hidden in the dialog. `_IconWorker` uses a bounded 48-job queue, prioritizes selected/visible icons, suppresses obsolete results, and drains one network job at a time.
- `serverloader.py` has fixed public Mojang/Fabric/Forge/NeoForge/Quilt endpoints, 30-second timeout, three retries, 16 MiB metadata cap, 2 GiB download cap, 64 KiB streaming and `.part` same-directory temporary files. It uses Python's default `urlopen` redirect handler.
- `pack_update.py` obtains GTNH server URLs from the authoritative `versions.json` index or accepts an explicit owner-supplied HTTPS source. It caps metadata at 16 MiB, server packs at 8 GiB, streams 256 KiB chunks, supports cancellation and staging, and uses default `urlopen` for the initial fetch/download. It does not construct GTNH archive URLs from version names.
- `gofile.py` is not an urllib client: it opens a direct TLS connection to fixed `upload.gofile.io/uploadfile`, guest-first, optional owner token only in Authorization, hashes and rechecks the same no-follow file descriptor, streams 256 KiB chunks, checks upload hash and file identity again, and honors cancellation before the final multipart boundary. Response size is capped at 1 MiB; the returned link must be an HTTPS `gofile.io`/`www.gofile.io` download page of the expected `/d/<id>` shape. Optional remote size/MD5 are not claimed verified when omitted.

A source/redirect audit also found a pre-existing HTTPS-policy gap: `serverloader.py` and `pack_update.py` validate the *initial* URL as HTTPS but use the default `urllib.request.urlopen()` opener. `modrinth._get()` and its older `fetch_bytes()` helper also use default `urlopen()` with no custom redirect validation. The Python 3.11 and 3.13 standard-library docs explicitly describe the default `HTTPRedirectHandler` as accepting redirect targets on HTTP, HTTPS or FTP schemes. Therefore an original HTTPS URL is not by itself an end-to-end HTTPS guarantee for these paths. This matters for server installers and modpack archives that contain executable code. The shared downloader and icon fetcher have custom redirect controls; other paths need an explicit HTTPS-on-every-hop policy and regression tests before Rust replaces them. Preserve legitimate cross-host HTTPS CDN redirects where the source design requires them; a strict provider host allowlist is appropriate for icons/authenticated endpoints but not necessarily for arbitrary user-supplied pack archives. Do not add credentials to unauthenticated redirecting requests.

Tests already cover disabled CurseForge before key read/network, forged or non-HTTPS redirect targets in the shared downloader, icon redirect allowlists and credential dropping, icon queue capacity/priority/cancellation/slow-drip deadline, and Gofile source mutation/hash/size/MD5/URL/cancellation/error redaction. Missing coverage should include explicit HTTPS-to-HTTP and HTTPS-to-FTP redirects in every default-urllib path, while preserving normal cross-host HTTPS CDN behavior.

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

The per-action tmux polling is another explicit performance boundary. After `tmux new-session`, `TmuxManager.start()` calls `_verify_started()` with a six-second deadline and 250 ms interval. Each iteration checks the exit-marker file and then calls `probe_running()` unless the marker appeared. A healthy start therefore runs roughly 24 `tmux has-session` subprocesses over the six-second grace window before the method returns. Early exit is caught by the marker/session checks; any optimization must retain immediate-failure diagnostics and not report success if the session died.

Graceful `TmuxManager.stop()` defaults to a 90-second timeout and 2-second polling interval. Each iteration calls `probe_running()`; while the tmux session persists, it calls `is_java_foreground()` and can send Ctrl-C to interrupt the GTNH wrapper's automatic-restart countdown. This can reach about 45 session probes plus up to 45 pane probes on the maximum-duration path. It is intentionally action-driven, not an idle poller, and its wrapper countdown behavior must not be removed. Benchmark only with isolated fake tmux responses or a disposable synthetic server, never by stopping the owner's live world.



### 3.2.1 Heartbeat and watchdog state are separate from live tmux status

The lifecycle model uses two related but distinct sources of truth:

- Live status comes from a fresh tmux/process observation. The GUI's cached five-second status map is presentation state only; destructive operations and lifecycle actions probe again.
- Crash recovery is an on-disk per-instance heartbeat under Crucible's state directory. It stores `state`, Linux `boot_id`, `tmux_session`, and `updated_at` when running is recorded. Stop-intent/clean-stop/handled-crash transitions preserve the prior boot/session metadata where present.

The observed heartbeat states are `running`, `stop_requested`, `stopped_clean`, and `crashed_handled`. Ordering is material: graceful Stop records `stop_requested` before the server is stopped (disarming watchdog restart); only confirmed completion records `stopped_clean`. If the machine reboots during a requested Stop, the old boot ID plus `stop_requested` still produces an owner-visible crash report. An uncertain tmux probe or missing boot ID is not treated as evidence of clean shutdown. A crash report is marked handled so it is not repeatedly shown on later launches.

`HeartbeatStore._write()` writes a same-directory temporary file, flushes/fsyncs the file, and publishes it with `os.replace()`. The code does not currently fsync the parent state directory after the rename. This makes the rename atomic to concurrent readers but is not the same as the full directory-entry durability protocol used by fleet backup publication. Record this as a durability question to assess separately; a Rust implementation must not claim stronger crash durability than it actually provides.

The watchdog is a Qt QObject living in its own QThread. It creates its QTimer only after `start()` runs on that thread. It polls every 10 seconds, requires two consecutive misses to confirm a crash, waits 30 seconds before an automatic restart, stops after three consecutive crashes, and resets the count only after 10 minutes of stable uptime. It separately tracks tmux-session misses and Java-foreground misses because GTNH-style wrapper shells can survive JVM exits. `stop_requested` disables restart while still observing a pending wrapper countdown. Restart re-runs world-start inspection and fails closed if the selected world is unsafe, unavailable, or requires an owner confirmation.

The current polling implementation does not match the watchdog module docstring's claim that one `tmux list-sessions` call covers every watched instance. `_poll()` loops over instances serially; for each watched server it calls `probe_running()` (`tmux has-session`, timeout 2 seconds), then, if the session exists, `is_java_foreground()` (`tmux list-panes`, timeout 2 seconds). This is up to two tmux subprocesses per watched server per 10-second tick, apart from heartbeat reads. The main window's five-second health worker separately does batch `list-sessions`, so this is a second observation path with a different process cost. With N healthy watched servers the watchdog can issue up to 2N tmux calls each poll; if commands time out, the serial loop can be delayed by seconds per instance. This is a source-confirmed count, not a measured latency benchmark. Benchmark at 1/5/20/50 watched servers and test a possible batch snapshot (`list-sessions` once plus a batch `list-panes` result) while preserving per-instance unknown/Java-miss/Stop-intent semantics. Do not replace per-instance uncertainty with one global false result.

Tests already cover heartbeat read/write transitions, reboot detection, same-boot interrupted Stop, cross-boot interrupted Stop, uncertain tmux/Java probes, wrapper-alive/Java-gone pending state, repeated crash confirmation, and Stop coordination. Preserve these as Rust/Python cross-implementation fixtures if watchdog logic is ever extracted. Do not move the QTimer, watchdog thread, restart scheduling, or tmux commands into Rust as a first slice.

### 3.2.2 World-start confirmation is a read-only gate with a persisted last choice

The configured `level-name` is read raw and preserved. The inspector rejects empty, dot, traversal, absolute, and path-like names before using the value as one direct child of the server root. It rejects symlinks and non-directory targets, never follows directory symlinks, recognizes developed alternate saves using `level.dat` plus region/player data (including selected Forge dimension subtrees), and scans the configured world for size, newest regular-file mtime, region count, and playerdata count. Permission or walk errors result in `unavailable`, not an assumed empty/new world.

The summary is immutable data returned from a short-lived worker thread. The UI checks its generation and selected instance ID to discard stale results. Immediately before dispatching Start it re-checks that the server is still confirmed stopped and that `level-name` still matches the inspected folder; it repeats that check after the FML prompt. Only after `tmux.start()` succeeds does the UI persist `instance.extra["last_started_world"]` through the ordinary registry update. New/incomplete folders, changed selections, or older records with other developed saves require explicit confirmation. Other save folders are retained.

The watchdog uses the same inspection helper but cannot show a modal confirmation during an automatic restart. It therefore refuses the restart whenever the selection is unsafe/unavailable or `requires_confirmation` is true. Keep the inspection pure/read-only and separate from the lifecycle action that follows it.

**World-selection error distinction still needs tightening:** `ServerInstance.world_level_name()` checks `server.properties` with `Path.exists()`, reads it when present, catches `OSError`, and returns the default string `"world"` both when the file is absent and when it exists but cannot be read. `inspect_instance_world_start()` consumes only this string, so it cannot distinguish a legitimate missing-file default from an unreadable configuration; a symlinked `server.properties` is also followed by the current read. Before a Rust port, preserve legacy defaulting only for a genuinely absent regular config file. A present but unreadable, non-regular, or symlinked `server.properties` should produce an explicit unavailable/unsafe result and block Start/watchdog restart. Introduce a typed `WorldSelectionRead` (`MissingUseDefault`, `Value(raw)`, `Unavailable(reason)`) rather than returning a string that silently loses the I/O failure. Add fixtures for absent file, regular file with no `level-name`, permission/read failure, symlinked file, broken symlink, malformed/empty value, and a valid custom world name.

**World-scan cost:** when a selected world folder exists, `inspect_world_start_state()` recursively walks it and stats every regular file to calculate total bytes, newest mtime, region count, and playerdata count. The pre-start summary worker runs when an instance is selected and is run again afresh when Start is pressed; WorldTab may separately run `ServerInstance.world_size_bytes()` when that tab is opened. The work is off the Qt event loop, but Start still waits for the full traversal to finish, and very large GTNH trees can contain many thousands of region files. This is not a measured latency claim. Benchmark by file count and tree shape. A candidate design is to separate a fast safety classification (safe selected path, selected save state, alternate developed saves, confirmation requirement) from optional detailed size/mtime/count statistics, or to reuse a versioned read-only inventory only after action-time revalidation. Do not let a partial/failed safety scan become "new" or "no other worlds"; if any required path cannot be inspected, return unavailable and block the action.

### 3.2.3 Watchdog polling and the multi-instance scaling gap

The active watchdog timer runs every 10 seconds, but the current code performs fresh tmux client calls inside the watched-instance loop: normal running instances use one `has-session` subprocess and one `list-panes` subprocess each. The `watchdog.py` module comment says one `list-sessions` call covers all watched instances, but that batch query is not used by the current `_poll()` implementation. This is a mismatch between the module comment and implementation.

A candidate refactor is a single per-tick session list and a global pane snapshot, with per-instance observations derived from the exact registered tmux session names. The pane snapshot must preserve the current exact-session/current-window/first-pane result. It must not choose an arbitrary pane or silently substitute the active pane without proving equivalence. The tmux manual supports `list-panes -a -F` and the required session/window/pane format fields, but the implementation still needs compatibility tests against the installed tmux versions Crucible supports. See the detailed benchmark and command design in the performance audit.

Preserve the active-only timer and all fail-closed semantics: two misses before crash classification, unknown session or pane is not a miss, a GTNH wrapper with Java absent is different from Java foreground, a requested Stop stays disarmed, and world-start confirmation still gates automatic restarts. Existing tests establish these semantics for one watched instance but do not test multi-instance query counts, multiple active windows, or multiple panes.

### 3.3 Fleet backup

BackupTab and WorldTab dispatch to FleetBackupManager in workers. The manager serializes backup/world/update operations with a global operation lock, snapshots the full registered fleet, checks process status/readiness, flushes running worlds, stages immutable generations, verifies, publishes by atomic rename/fsync, rotates retention only after successful publication, and defers source cleanup until retained generations are safe.

Incremental behavior is already significant: unchanged content can be hard-linked from the preceding immutable generation, avoiding fresh reads/writes. Do not replace this with an always-full copy or an archive format change under the guise of a Rust port.

### 3.4 Import and pack update

Import paths detect source type and build a plan, extract archives into staging on the destination filesystem when possible, reject unsafe paths/links/oversized entries, copy server-safe content, add metadata/receipt, optionally download indexed mods and install server software, and surface warnings. GTNH pack updates first create a reviewable plan, then separately stage/download/apply, preserve world/config/mod inputs, verify, and keep rollback.

The extraction path intentionally uses the destination parent as the temp directory so a multi-GiB pack is not expanded under a small tmpfs `/tmp` and a second cross-device copy can be avoided. This is a performance feature worth preserving.

### 3.5 CLI invocation and compatibility flow

The CLI entry is `crucible.__main__:main`. Its observed order is:

1. Build the `argparse` parser and parse argv. Argparse handles `--help` before Crucible loads the registry or initializes any UI.
2. With no subcommand, print the banner and root help, then exit with code 0.
3. For normal commands, print the banner. `gui` and hidden `shortcut-action` suppress it.
4. Construct `InstanceManager`, call `load()`, construct `TmuxManager`, then dispatch by exact command string.
5. Each handler owns its output and uses `sys.exit(1)` or `sys.exit(2)` for failure classes in different paths; successful handlers often return `None`. The exit-code convention is not currently centralized in a typed result.

Start is a special behavior contract: `_confirm_cli_world_start()` rejects unsafe/unavailable summaries, requires a TTY for confirmation, and requires the owner to type the exact word `START`. Non-interactive invocations cannot bypass confirmation. The dispatcher does not duplicate the GUI, but it still calls the same shared TmuxManager/domain routines.

A Rust CLI replacement should not be the first Rust integration slice. Before even prototyping it, capture golden invocations for every command family, all aliases/options and their defaults, stdout/stderr, exit status, whether the command touches the registry, filesystem/network/processes, TTY behavior, and environment/working-directory assumptions. The current CLI is a public compatibility surface and also a useful Python reference path while the GUI remains Python.

### 3.6 Client export and share

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

The installed layout is important to the Rust integration choice. Current install.sh behavior:

- Candidate interpreter order is Python 3.13, 3.12, 3.11, then python3; a valid CRUCIBLE_PYTHON override is honored. It prefers an interpreter that already imports PyQt6.
- Creates a per-user venv with --system-site-packages. If Qt is not already visible, Nobara/Fedora gets a dnf package attempt; otherwise PyQt6 is installed with pip inside the venv.
- Copies distributable source into $APP_ROOT/source, excluding Git metadata, caches, build output and previous venvs.
- Runs compileall, imports PyQt6 and Crucible, checks the package version, and invokes crucible --help before publishing.
- The launcher resolves its own symlink with readlink -f, derives the real app root, prepends $APP_ROOT/source to PYTHONPATH, and execs $APP_ROOT/venv/bin/python -m crucible.
- Existing app directory is renamed to a .crucible-previous.* rollback path. Staged app is atomically renamed into place; the final-path launcher is smoke-tested before the rollback copy is deleted. Desktop integration is written after publication.
- Backups live outside the app root; a legacy in-tree backup directory is copied with no-overwrite behavior.
- The one-line installer queries GitHub's latest release, requires the exact Crucible-tag-source.zip and Crucible-tag-SHA256.txt asset names, bounds download sizes, verifies SHA-256, rejects unsafe ZIP paths/symlinks/oversized expansion, requires exactly one install.sh, tests ZIP CRCs, then invokes the installer.
- No Rust toolchain is currently required from the owner at installation time.

**Packaging trap for PyO3:** the launcher prepends the copied source tree to PYTHONPATH. Simply pip-installing a wheel into the venv may not make its extension visible, because crucible is imported from $APP_ROOT/source before site-packages. A production design must deliberately place the native module inside the imported package tree, install the complete project as a wheel and stop shadowing it with a source path, or define another explicit loader strategy. Do not assume "pip installed the wheel" means the imported crucible package can see its extension.

Therefore, any production Rust component must be delivered as a verified prebuilt artifact or compatible wheel. Do not make owners install Rust or compile the app during routine updates. The build pipeline must produce platform/Python-compatible artifacts, include and verify them, stage them with the app, test import from the final path, and restore the old installation if the artifact is missing or incompatible.

tests/test_release_scripts.py contains nine focused checks covering installer staging/rollback markers, interpreter selection (including CRUCIBLE_PYTHON), symlink-resolving launcher behavior, lazy tab data loading, CLI lifecycle wiring, exact verified release assets, and package data declarations. These tests use source checks/stubs; they do not replace a real installed-upgrade test on each supported target environment.

If PyO3 is selected, measure wheel size/build matrix and Python 3.11/3.13 compatibility. PyO3's newest stable-ABI features and Python 3.15/free-threaded support are not a reason to raise Crucible's runtime requirement. If a JSON worker is selected, package a matching executable with the app and keep it long-lived, using batched meaningful work rather than paying process startup on every small status check.

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
| Process state/lifecycle | `test_core.py` covers exact tmux targets, unknown timeouts, unmanaged Java, wrapper survival, graceful-only Stop, and action-time removal checks | A shared per-instance PID association contract, tri-state scan fixtures, and performance tests with scaled fake process tables |
| Heartbeats/watchdog | `test_crash_recovery.py`, `test_watchdog_stop_coordination.py`, and `test_security_boundaries.py` cover heartbeat ordering, unknown probes, pending Stop, Java/wrapper distinctions, two-miss crash confirmation, restart limits and world gates | Golden heartbeat JSON plus multi-instance watchdog snapshot fixtures and query-count tests |
| World start | `test_world_start_summary.py` covers existing/new/incomplete saves, alternate saves, selection change, traversal, symlink and modded dimension detection | More malformed inventory/permission fixtures and direct pure-classifier fixtures separate from filesystem scan tests |
| Properties | `test_properties_validation.py` covers legacy GTNH 1.7.10 keys, modern-version removals, unknown versions, loader inference, Java property escapes and semantically unsafe guesses | Text-level parser/validator fixtures with ordering, duplicate keys, comments, escapes and version matrix |
| Pack update | `test_pack_update.py` covers version parsing, variant policy, world preservation, running/unknown blockers, downgrade warnings, insufficient disk, repeated live-state checks, rollback and cancellation | Serialized update-plan golden fixtures and parity on warnings/blocker reason codes |
| Client export/share | `test_client_export.py` and `test_client_export_review.py` cover archive membership, source hash changes, exact SHA decisions, privacy checks, symlinks, duplicate mods and prohibited JARs | Golden sealed-manifest fixtures and a source-mutation parity corpus; do not benchmark by dropping review passes |
| Fleet backup/world swap | `test_fleet_backup.py` and `test_world_backup_swap.py` cover incremental reuse, immutable generations, fsync/publication failures, unknown/running refusal, symlinks, archive safety, rollback and restore | Golden schema-1 manifests and fault-injection fixtures before any backup core is moved |
| Unmanaged-process launch safety | `StartTests.test_start_wrapper_has_no_unbounded_tee` stubs `is_running=False` and port-conflict=None; watchdog security tests block only when the original wrapper still owns its tmux session | Add shared-start tests proving a matching unmanaged PID or incomplete process scan blocks launch; cover CLI, GUI cached state, shortcut, and watchdog auto-restart callers |
| Destructive removal/process discovery | Existing tests block a known unmanaged PID and a tmux timeout, but do not cover tmux missing, `/proc` enumeration failure, incomplete scans, or force-kill verification when procfs is unavailable | Add tests proving tmux missing still triggers a complete process check, incomplete procfs blocks unregister/delete, and force-kill never reports success without verified process absence |
| Installer/release | `test_release_scripts.py` covers safe staging/rollback markers, interpreter selection, symlink-resolving launcher, exact verified assets, package data and UI loading contract | Test a prebuilt Rust artifact in the final installed path, supported ABI/platform matrix, failed-import rollback and source-package shadowing |
| General security boundaries | `test_security_boundaries.py` covers registry shapes, last-key-wins properties, bounded crash logs/JAR metadata, uncertain probes, path escapes and ZIP limits | Reuse identical malicious/oversized fixture files against both languages |

Some representative test files contain dozens of focused cases (for example, 83 tests in `test_pack_update.py`, 57 in `test_core.py`, and 29 + 21 in the client-export/archive-review suites). The suite is not merely smoke testing; it includes behavioral and failure-path coverage.

Two important limits remain:

1. The current CI workflow runs Python 3.11 and 3.13 tests, Qt smoke and UI preview. It does **not** run Cargo checks/tests or a repeatable performance benchmark suite.
2. Existing tests protect current Python behavior. They are not yet a cross-language parity harness and do not prove that a Rust replacement returns identical output, errors, ordering or side-effect boundaries.

For the candidate procfs optimization specifically, `test_machine_resources.py` confirms exact session-marker matching and excludes false Java matches from non-Java commands, while `test_resource_monitor.py` covers per-instance marker and Java/path matching. A new batch association map needs explicit multi-instance tests: marker-to-ID association, legacy Java fallback, overlapping paths, empty/inaccessible `cmdline`, process exit during scan, and no cross-instance PID attribution. The removal tests also need negative cases for tmux absent and procfs unavailable; a mocked empty PID list is not proof that discovery completed. Performance tests should use controlled fake procfs or helper processes, not the owner's live server process table.

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
