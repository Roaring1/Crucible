# Crucible Rust Migration: Context and Architecture Assessment

**Status:** Phase 0 audit documented; first Phase 1 non-production Rust parity pilot validated by GitHub CI  
**Date:** 2026-11-06  
**Repository baseline:** `main`, observed at `0eb89f2517fc3c4ba9417c2bc97e6c63b5f80b8c` during this assessment  
**Scope:** Document the existing product, runtime boundaries, compatibility obligations, migration risks, and a deliberately incremental path. A first isolated Rust parity pilot now exists; it is not connected to production Crucible behavior.

## 1. Executive summary

Crucible is not merely a Minecraft process launcher. It is a Linux desktop server-management application with a substantial safety-sensitive domain: it discovers and registers existing server directories, manages long-lived tmux sessions, interprets uncertain lifecycle state, protects worlds, imports and exports modpacks, edits mods and configuration, inspects logs and players, and provides recovery workflows. Its Qt UI, CLI, installers, desktop shortcuts, registry, and server folders form one user-facing product.

The current application is Python 3.11+ with PyQt6. The package is installed through a Python/setuptools project and exposes the `crucible` command. It intentionally delegates Minecraft process lifetime to tmux: closing the GUI must not stop a server. The GUI is an adapter over shared domain/process modules, not the owner of the Minecraft process.

**Recommended direction:** keep the current Python/PyQt6 application as the reference product while introducing Rust in small, independently verifiable slices. Begin with a toolkit-independent Rust core containing explicit domain types and pure decisions. Keep Qt, dialogs, settings, and user feedback in Python. Put operating-system and external-process work behind narrow adapters. Do not select a Python/Rust bridge or move a safety-critical operation until its contract, parity tests, packaging path, and rollback behavior are established.

A separate high-priority safety finding must be handled before any process-discovery refactor or Rust extraction: current process discovery can treat missing tmux or an incomplete `/proc` scan as proof that no unmanaged server process exists. This affects both sides of lifecycle safety: `safe_to_remove()` can allow unregister/permanent folder deletion without proving absence, and `TmuxManager.start()` does not perform a process-absence scan at the shared launch boundary. The CLI Start and watchdog auto-restart call that method directly; the GUI relies on cached status; the shortcut does a fresh status check but still inherits best-effort scan semantics. The existing IPv4 configured-port collision check is useful, but it is not a complete identity-aware process check. The force-kill verifier also trusts an empty best-effort PID result. A live unmanaged JVM could therefore evade a removal check or a Start preflight, and an incomplete scan could be reported as absence. This requires a separate narrowly scoped safety fix with a tri-state process result and tests for launch, deletion, and kill verification. The call paths and test gaps are documented in `rust-migration-source-audit.md`, `rust-migration-performance-audit.md`, and `rust-migration-contracts.md`. It is not changed by this PR or by the isolated parser pilot.

A full rewrite, a simultaneous UI rewrite, or a change to on-disk formats would multiply risk without proving that Rust improves the actual bottlenecks.

## 2. Product identity and operating assumptions

The README describes Crucible as a focused Linux desktop app for creating, importing, running, and protecting Minecraft dedicated servers. Its intended workflow is local server management and client-pack exchange, not a cloud control plane or router configurator.

Current product surfaces include:

- **Dashboard/server workspace:** server identity, lifecycle state, readiness, and resource information.
- **Console:** live output, command entry, search, TPS/MSPT, and player activity.
- **Setup/import:** existing folders, Prism/MultiMC instances, modpack archives, and guided server installation.
- **Mods and players:** local mod inspection and controlled changes, plus whitelist/ban/operator/player workflows.
- **Worlds and recovery:** world discovery, backups, named slots, safe swaps, rollback, reset, and destructive-action confirmation.
- **Diagnostics:** health checks, logs/crashes, and loading-error investigation.
- **CLI and desktop shortcuts:** explicit server actions without requiring the main GUI to be open.

Important product invariants visible in the current source and docs:

1. A Crucible window closing is not a Minecraft Stop command. Managed servers live in independent tmux sessions.
2. Unknown or ambiguous status must not authorize lifecycle or destructive actions. Current process-discovery gaps affect Start, removal, and force-kill verification in specific tmux/procfs failure cases; fix them before any related port.
3. Graceful Stop must not silently turn into force-kill.
4. A Start must use a positively confirmed stopped state and must inspect the selected world before launching.
5. World replacement/restoration must validate first, protect the old state, verify the result, and retain a rollback route.
6. Existing server folders, worlds, mods, backups, and user data are user-owned data. An application update must not casually migrate, rewrite, or delete them.
7. The CLI, GUI, generated shortcuts, and tray are alternate front ends to the same lifecycle rules, not separate implementations of those rules.
8. Expensive work must not block Qt's event loop. Large worlds and archives require bounded workers, progress/cancellation semantics, and careful object lifetimes.
9. Performance is intentionally demand-driven. Do not add permanent polling, telemetry, or resident helper processes without a demonstrated need.
10. The UI must report the actual scope and certainty of an action. It must not imply a successful backup, restore, upload, or stop when the result is uncertain.

## 3. Current implementation and packaging baseline

### 3.1 Language, UI, and package

Verified from `pyproject.toml`, `crucible/__init__.py`, and `.github/workflows/tests.yml`:

- Package: `crucible`, current source version observed: `0.8.1`.
- Python requirement: `>=3.11`.
- UI dependency: `PyQt6>=6.5`.
- Build backend: setuptools.
- Installed command: `crucible = crucible.__main__:main`.
- Package data includes PNG/SVG assets, icon licenses, JSON data, and UI icon sets.
- Hosted tests currently target Python 3.11 and 3.13 on Ubuntu 24.04 with `QT_QPA_PLATFORM=offscreen`.
- CI runs the pytest suite, a PyQt interaction smoke test, and UI preview capture.
- The test workflow creates a dedicated temporary tmux socket directory. This isolation is important: tests must never attach to or mutate an owner's live tmux sessions.

The source installer stages an installation under `~/.local/share/crucible`, exposes the command under `~/.local/bin`, creates desktop integration, and has rollback logic for failed installation. The one-line installer downloads the exact release source and checksum assets and verifies them before installation. A Rust port therefore changes more than compilation: it affects release asset layout, staged installation, version discovery, executable paths, desktop entries, and upgrade rollback.

### 3.2 Entry points and shared behavior

- `crucible/__main__.py` is the CLI entry point and contains a large set of command handlers. It uses shared instance, world-safety, crash-recovery, and tmux modules.
- `crucible/ui/main_window.py` owns the top-level Qt window and the five-second health-check schedule. Its persistent worker requests a status snapshot and updates views.
- `crucible/ui/instance_panel.py` owns the selected-server workspace and UI lifecycle presentation. It coordinates workers, log watching, startup detection, watchdog behavior, and tabs.
- `crucible/data/instance_manager.py` owns registry loading/saving and cross-process registry-change handling.
- `crucible/data/instance_model.py` defines the server-instance model and its validation/serialization behavior.
- `crucible/process/tmux_manager.py` centralizes tmux process/session operations.
- `crucible/process/crash_recovery.py` persists heartbeat state to distinguish a host crash from an intentional stop.
- `crucible/data/backup_manager.py` retains compatibility for per-instance world archives and safe world swaps; `crucible/data/fleet_backup.py` implements the current fleet-wide immutable generation system, incremental hard-link reuse, verification, retention, and legacy-source migration. It is mapped in the source and performance audits; it remains a high-risk subsystem, not an early port target.
- `crucible/mods/modrinth.py` contains Modrinth HTTP/API behavior and typed-ish result records. Network/provider behavior should remain behind adapters.
- `crucible/exporters/client_export.py` implements a large client-pack export pipeline with content hashing, deterministic input fingerprints, archive construction, and manifest data.
- `crucible/importers/prism.py` is the Prism/MultiMC and modpack import path.
- `crucible/ui/tabs/` and related dialogs render domain state and collect owner decisions. They should not become the place where Rust business rules are duplicated.

The full Python file inventory (80 source files) and test/support inventory (62 files) have now been enumerated. The fleet-backup implementation, status/resource sampling, watchdog polling, GUI worker structure, importer/exporter boundaries, provider trust rules, filesystem mutation owners, and principal end-to-end flows have been inspected. Persisted/runtime contracts and a detailed golden-fixture plan are in `rust-migration-contracts.md`. A complete per-symbol dependency graph and actual sanitized fixture corpus remain Phase 0 work; see `rust-migration-source-audit.md`. The unmanaged-process launch/removal/kill-verification findings are safety prerequisites, not Rust migration tasks.

### 3.3 Persistent state and compatibility surfaces

Known user-visible paths and formats:

| Purpose | Current location / contract | Migration requirement |
|---|---|---|
| Instance registry | `~/.config/crucible/instances.json` | Preserve schema version 1 and unknown/optional metadata behavior; do not silently rewrite it |
| Legacy backup root | Historical `~/.local/share/crucible/backups/{instance-id}/` | Read old archives and sidecars without destructive migration |
| Current backup root | `~/.local/share/crucible-backups/` | Preserve all existing generations, archives, metadata, and ownership |
| Instance server folder | Owner-selected arbitrary path | Never assume it can be moved into Crucible's app directory |
| Per-server metadata | `<server>/.crucible/` | Preserve receipts, mod history, import summaries, manifests, and other records |
| Crash-recovery heartbeat | User state under the Crucible state directory | Preserve state transitions and boot-ID semantics |
| Desktop shortcuts | Per-user desktop/applications locations | Preserve stable instance-ID arguments and revalidation at action time |
| CLI | `crucible` command and documented subcommands | Preserve argv semantics and exit behavior or provide a deliberate compatibility layer |

The registry manager uses atomic write patterns and a 16 MiB input bound. It checks for duplicate instance IDs, paths, and tmux sessions, and protects against unexpected external registry edits. It has a narrow reconciliation path for lifecycle-only changes made by the CLI. These are domain and persistence contracts, not incidental Python implementation details.

### 3.4 Process and lifecycle behavior

The tmux manager is the only authority for server process/session operations. It uses argument arrays rather than shell command interpolation for its own subprocess calls, bounded command timeouts, and exact tmux targets. It distinguishes a confirmed result from an unknown result. In particular, tmux session existence alone is insufficient for some GTNH start scripts: a wrapper can remain alive while Java is stopped or in a restart countdown. The current implementation also inspects the pane's foreground command and Java process state in relevant decisions.

The crash-recovery heartbeat records a Linux boot ID, tmux session, and state such as `running`, `stop_requested`, `stopped_clean`, or `crashed_handled`. Stop intent is persisted before sending Stop so a live watchdog cannot mistake a requested shutdown for a crash and restart it. If Stop fails, only a positively confirmed still-running server can restore the running marker; uncertainty remains disarmed. On the next app launch, a changed boot ID plus missing tmux session can indicate a host crash. An uncertain tmux probe is never proof of a clean stop.

The GUI's five-second health check shares one status result across the main window and tray. The resource-usage design is on-demand: a short-lived worker samples procfs, waits for CPU deltas, optionally queries `nvidia-smi` with a finite timeout, and caches the result. There should not be a second independent Rust poller competing with the Python/Qt health worker.

### 3.5 World, backup, import/export, and provider risk

These areas have the highest data-loss, compatibility, or external-policy risk and should not be early rewrite targets.

- **World selection and startup:** the recent mainline change inspects the raw configured `level-name`, reports the selected save's state/size/timestamps, remembers the last successfully started world, and requires confirmation for new/changed selections. Unsafe paths, non-interactive shortcut starts, and watchdog restarts needing review fail closed.
- **Backup and restore:** large GTNH worlds can be many gigabytes. Backup correctness includes snapshot consistency, file-type/path validation, hashes, free-space checks, archive integrity, generation retention, and restore verification. A Rust implementation must not infer that a faster ZIP library makes the workflow safe.
- **World swaps:** preserve the current world before replacement, validate the incoming archive/root, reject traversal, links, special files, duplicate destinations and truncation, verify restored output, and roll back on failure.
- **Mod changes:** stop the server before changing JARs; stage downloads and dependencies; verify exact hashes/identity; retain previous files and operation receipts; recover from partial publication. World backups are a separate concern because a mod replacement does not migrate a world.
- **Client export/share:** the exporter computes hashes and deterministic fingerprints, validates archive members, emits a manifest, reviews privacy and distribution decisions, and protects local output. Public upload is a separate provider operation. Do not collapse build, review, and upload into one Rust function.
- **Provider policy:** current README says CurseForge online browsing/API/CDN downloads, provider-backed changes, and media/icon fetching are disabled in the conservative fallback while Crucible-specific authorization, data-retention, and compatibility questions remain unresolved. Modrinth and local/manual workflows remain available. A Rust port must preserve this policy; it must not re-enable a provider as an accidental side effect.
- **External APIs:** Modrinth and any later provider need bounded response sizes, finite timeouts, validated URLs/redirects, explicit error types, hash checks, and credential redaction. API tokens must not leak into logs, archives, manifests, crash reports, or error dialogs.

## 4. What Rust should and should not own

### 4.1 Good candidates for a Rust core

The first Rust crate should be pure, small, deterministic, and independent of Qt:

- Domain types: instance identity, server status, lifecycle action eligibility, world-selection summary, resource snapshot, and structured operation outcomes.
- Pure validation and decision functions: whether Start/Stop is allowed for a given confirmed state; whether a world selection requires review; how to classify uncertainty; whether a state transition is legal.
- Serialization compatibility helpers for explicitly versioned JSON contracts, after golden fixtures exist.
- Bounded input parsing and canonicalization for data that crosses process boundaries.
- Later, isolated high-cost or security-sensitive primitives such as archive path validation, manifest verification, and content hashing, but only after Python parity tests and a clear performance or correctness case.

### 4.2 Keep in Python initially

- PyQt6 widgets, dialogs, menus, settings, tray, notifications, icons, accessibility, and visual feedback.
- The existing application entry point and installer until a replacement has proven parity.
- UI thread/worker orchestration and Qt object lifetimes.
- Existing CLI handlers until the Rust core has stable contracts.
- Provider-specific HTTP workflows, release download/install behavior, and external upload orchestration.
- Any code whose real semantics depend on user interaction, confirmation, or owner review.

### 4.3 Process adapters, not core logic

tmux, procfs, filesystem operations, Java detection, desktop integration, and optional GPU tools are environmental adapters. Their core-facing interfaces should return typed outcomes that distinguish success, a negative observation, and inability to observe. For example, `Running`, `Stopped`, and `Unknown(reason)` must remain distinct. A command timeout, missing tmux binary, permission failure, malformed output, or vanished process is not equivalent to a clean stopped state.

Rust should not run its own independent lifecycle scheduler or watchdog. The initial boundary should be invoked by the existing application at explicit decision points and consume a snapshot supplied by the caller.

## 5. Python/Rust integration options

Do not choose the bridge based on fashion. Measure the build, packaging, debugging, and runtime costs against the actual first slice.

| Approach | Advantages | Costs / risks | Appropriate use |
|---|---|---|---|
| Rust executable with versioned JSON stdin/stdout | Strong process boundary; language-neutral contract; easy to test in isolation; Python can retain control | Process startup and serialization; executable discovery/packaging; protocol versioning; must avoid spawning per tiny operation | A bounded worker or substantial operation where process isolation is useful |
| PyO3 extension (often packaged with maturin) | Direct typed calls; no per-call process startup; natural for pure functions and CPU-heavy routines | Python ABI/build matrix; native wheel and local install complexity; native crashes affect GUI process; debugging and distribution complexity | Once a stable pure core is valuable to multiple Python call sites |
| Rust library behind a narrow C ABI | Stable cross-language ABI possibility | Manual ownership and error mapping; unsafe FFI; more engineering than needed for a Python-only application | Only if another non-Python consumer becomes a real requirement |
| Full Rust CLI/UI replacement | One language for most runtime code | Highest scope and compatibility risk; Qt replacement is a separate project; installer and desktop integration need redesign | Not recommended as the initial plan |

**Provisional recommendation:** start with a standalone Rust core crate that has no Python or Qt dependency and can be tested with `cargo test`. Do not immediately bind it into the app. First prove its model and contract with fixtures. Then compare a small PyO3 binding against a small JSON subprocess adapter using the same functions and measure both. Choose only when there is evidence. If a bridge is needed early, prefer a single long-lived, request/response worker for meaningful batches rather than spawning a process for every tiny check.

## 6. Crosshair Rust-port reference: lessons, not a template

The owner's Crosshair `rust-port` branch is a useful internal reference. Its observed Cargo manifest:

- Uses Rust 2024 edition and declares Rust 1.92. That was the branch's observed toolchain/MSRV context when inspected, not today's current stable compiler. The official Rust release feed lists Rust 1.99.1 (2026-10-15) as current stable on this assessment date; validate early experiments on the current stable toolchain, then choose an explicit and separately tested MSRV for the shipped crate. See the toolchain snapshot in `rust-migration-performance-audit.md`.
- Names its library `crosshair_core`, separate from the CLI binary.
- Keeps Cargo default features empty.
- Makes the Slint Settings UI an explicit optional feature.
- Uses serde/serde_json and thiserror for typed serialization/errors.
- Keeps platform-specific dependencies target-gated.
- Has explicit release profile settings and a separate CI validation record.
- Keeps the established Python implementation on `main` while the Rust implementation lives on a dedicated `rust-port` branch. The Rust README is explicit that the branch is a development implementation, not a published stable release.

Useful transferable practices:

1. Separate the core library from binaries and UI.
2. Keep optional UI dependencies out of the default core build.
3. Make platform-specific capabilities explicit instead of pretending all Linux display environments behave alike.
4. Define bounded parsers and structured errors at boundaries.
5. Keep the original implementation available as a behavioral reference while parity is being established.
6. State exactly what has and has not been user-tested; do not turn a passing compile into a platform-support claim.

Important differences: Crosshair is an overlay/configuration tool with different process and data-safety semantics. Crucible owns workflows that can affect multi-gigabyte worlds and long-lived Minecraft servers. Reusing Crosshair's Rust patterns is sensible; copying its UI, platform architecture, dependency list, or release strategy without analysis is not.

## 7. Migration hazards and non-negotiable guardrails

### 7.1 Data compatibility

- Do not change the registry schema or path as part of the first Rust slice.
- Build golden fixtures from real-shaped but sanitized registry records, including optional/unknown fields and older records.
- Test round trips without normalizing away metadata.
- Never run a one-time mass migration against the user's live registry during development.
- Preserve backup archives, sidecars, receipts, and per-server `.crucible` metadata exactly.
- Keep file permissions, atomic replacement, symlink behavior, and ownership semantics explicit.

### 7.2 Lifecycle safety

- No Rust implementation may issue Start/Stop/Kill on its own during tests.
- No automatic fallback from graceful Stop to force kill.
- Never map timeout/error/permission-denied to Stopped.
- Revalidate the exact instance ID and current status immediately before an external action.
- Preserve the Stop heartbeat ordering and crash-recovery state machine.
- Never start a server just to inspect it or back it up.
- Do not use display names as persistent identifiers; use the stable registry instance ID.

### 7.3 Filesystem and archive safety

- Reject traversal, absolute paths, drive prefixes, symlinks where disallowed, special files, duplicate normalized destinations, and unexpected archive roots.
- Bound archive entry count, individual size, total expanded size, compression ratio, metadata size, and time where appropriate.
- Check available disk space conservatively and re-check inputs before publishing results.
- Use staging plus atomic publication where possible; retain rollback receipts.
- Avoid following symlinks during recursive scans or deletion.
- Treat archive paths as untrusted input even when the archive came from a familiar modpack tool.

### 7.4 UI, workers, and responsiveness

- Keep Qt objects on the GUI thread.
- Rust workers return immutable results; Python/Qt owns presentation.
- Every long-running operation needs cancellation, bounded progress updates, clear completion/error states, and cleanup semantics.
- Do not create a second poller for status, resources, tray, or watchdog.
- Preserve the existing persistent-worker lifetime discipline. A Rust callback must not outlive the Python object or Qt event target it references.
- Avoid holding the GIL during long CPU/I/O work if a PyO3 binding is eventually selected, but do not add binding complexity before the boundary is stable.

### 7.5 Build and release

- Keep Python 3.11/3.13 CI green during the coexistence phase.
- Add a locked Rust toolchain and `cargo fmt --check`, `cargo clippy`, `cargo test --locked`, and reproducible release builds. Keep the development toolchain and the declared MSRV as separate decisions; do not simply copy Crosshair's Rust 1.92 pin.
- Test the installed package, not only the source checkout.
- Ensure release archives contain the correct platform executable and required runtime assets.
- Verify staged update rollback if any Rust binary is missing, corrupt, incompatible, or fails its self-check.
- Do not replace the `crucible` command until its arguments, output, exit codes, environment handling, and install/uninstall behavior are covered.
- Keep the old Python path available for rollback until the Rust slice has been used successfully in real owner workflows.

## 8. Proposed phased plan

Each phase is a separate reviewable change. Stop and reassess when a phase exposes an undocumented behavior.

### Phase 0 - context and contract inventory (this document)

- [x] Inspect README, package/build metadata, CI, primary entry points, registry, instance model, tmux manager, crash recovery, backup manager, importer/exporter, provider module, and Crosshair Rust reference.
- [x] Record current architecture and high-risk invariants.
- [x] Enumerate all 80 Python source files and 62 test/support files; map the main package boundaries, fleet backup, status/resource sampling, principal worker routes and main end-to-end flows. A complete per-symbol dependency graph is still pending.
- [ ] Implement representative sanitized fixtures from the matrix in `rust-migration-contracts.md` for registry, heartbeat, lifecycle status, world-start summaries, backup manifest, import receipts, export manifest, catalog records, and provider responses.
- [x] Identify benchmark candidates and separate source-confirmed costs from unmeasured hypotheses in `rust-migration-performance-audit.md`, including repeated process discovery, serial watchdog tmux probes, eager tab-module imports, synchronous/repeated archive inventories, redundant Backup-tab generation/legacy-source scans, client-export content passes, and backup I/O. The log tail/console path is already byte- and document-bounded; only metric-signal coalescing remains a measurement-gated Python candidate.

### Phase 1 - Rust core skeleton, no production call sites

- [x] Add a small dependency-free crate at `rust/crucible-core`.
- [x] Add the first pure primitive: GTNH pack-version parsing, channel classification, and ordering.
- [x] Add a second pure primitive: GTNH server-pack name parsing into captured version and Java variant, without filesystem or receipt discovery.
- [x] Add separate shared TSV fixture corpora consumed by Rust unit tests and Python pytest reference tests.
- [x] Add dedicated GitHub Actions jobs for stable Rust formatting/tests/Clippy and the declared MSRV. Verified on tested head `81bf49f6d70abede8e4374b966893e289ee6c23b`: stable Rust 1.99.1 passed formatting, 12 unit tests and Clippy; Rust 1.85.0 passed all 12 unit tests. Python 3.11 and 3.13 each passed 848 tests plus 95 subtests and the Qt interaction smoke. See [Rust core CI run](https://github.com/Roaring1/Crucible/actions/runs/42333476845) and [Python tests run](https://github.com/Roaring1/Crucible/actions/runs/42333476850).
- [x] Keep the crate independent of Qt, tmux, filesystem mutation, and network access; no Python production call site invokes it.
- [x] Document and pin known parser differences in Rust-only and Python-only tests: Unicode decimal digits, Python `re.IGNORECASE` dotted/dotless I and long-s folding, and Python-specific U+001C..U+001F trimming.
- [ ] Resolve those differences or provide a tested Python fallback before either parser can be considered drop-in parity.
- [ ] Add representative domain enums/structs only after their serialized compatibility contracts and golden fixtures are ready.
- Do not access the user's filesystem, invoke tmux, or integrate with the GUI in this phase.

#### First Rust parity pilot: GTNH version ordering

The pilot lives in `rust/crucible-core/src/pack_version.rs`. It mirrors the existing ASCII GTNH version syntax and sort key used by `crucible/importers/pack_update.py`: major, minor, patch (default zero), channel rank (alpha 0, beta 1, RC 2, stable 3), and prerelease number (default zero). Decimal components are normalized and compared as strings, avoiding fixed-width overflow. This is not unlimited Python parity: CPython 3.11+ limits decimal `int()` conversions (default 4,300 digits, configurable), and the Python reference currently raises `ValueError` when a captured component exceeds the active limit; the Rust parser accepts the 4,301-digit case. The shared vectors live in `rust/crucible-core/fixtures/pack-version-cases.tsv`; Rust tests and `tests/test_rust_pack_version_fixtures.py` consume the same file. The Rust parser also tests the 4,301-digit case; the Python reference test records the active CPython conversion limit rather than assuming it is fixed. See [Python's integer conversion limit](https://docs.python.org/3.11/library/stdtypes.html#integer-string-conversion-length-limitation).

The current tested head `81bf49f6d70abede8e4374b966893e289ee6c23b` passed formatting, 12 Rust unit tests and Clippy with warnings denied on stable Rust 1.99.1; Rust 1.85.0 MSRV passed the same 12 tests. Python 3.11 and 3.13 each passed 848 tests plus 95 subtests, and the Qt interaction smoke passed in both jobs. See [Rust CI run](https://github.com/Roaring1/Crucible/actions/runs/42333476845) and [Python CI run](https://github.com/Roaring1/Crucible/actions/runs/42333476850). These are contract/toolchain pilots, not hot-path optimizations. Python remains authoritative. Current parser differences are pinned in tests: Rust version/name numeric parsing accepts ASCII digits only; Python `re.IGNORECASE` also folds U+017F long-s and U+0130/U+0131 dotted/dotless I in “Horizons”; and Python `str.strip()` removes U+001C..U+001F where Rust `str::trim()` does not. The Rust server-name test now covers the same additional Unicode gaps. No runtime behavior, pack choice, update, or installed application uses the Rust code.

#### Second Rust parity pilot: GTNH server-pack name and Java variant

The second primitive lives in `rust/crucible-core/src/pack_identity.rs`. It mirrors only the pure string portion of Python's `identify_gtnh_name()`: locate a GT New Horizons server name, validate its captured GTNH version, and classify Java 8 versus Java 17+. It searches the full input string, preserves the original trimmed version spelling, respects the first syntactic match, and permits archive suffix text. It does not open files, read provenance receipts, inspect instance paths, or choose identity confidence.

The shared vectors live in `rust/crucible-core/fixtures/pack-name-cases.tsv`; Rust tests and `tests/test_rust_pack_identity_fixtures.py` use the same file. The corpus covers official filenames, folder/path prefixes, case/separator variants, prerelease versions, trailing extensions, malformed names, unsupported Java tokens and first-match precedence. Python and Rust both passed these vectors. Known Unicode mismatches remain: Python's Java token uses Unicode-aware `\d`, and its `re.IGNORECASE` matches dotted/dotless I and long-s variants inside the name; Rust currently uses ASCII token matching and requires an ASCII first digit after `17-`. Rust-only tests pin those divergences, while Python-only tests preserve the reference behavior. Python `str.strip()` also removes control separators around the captured version which Rust `str::trim()` retains. The parser has no production call site and makes no performance claim.

### Phase 2 - parity harness

- Create a fixture corpus and a Python reference harness.
- Run the same inputs through Python and Rust and compare normalized outputs.
- Include malformed inputs, missing fields, future/unknown enum values, and boundary sizes.
- Keep Python authoritative when behavior differs; explain and resolve each mismatch rather than silently changing behavior.

### Phase 3 - choose one integration seam

- Prototype one small bridge around a pure operation.
- Compare PyO3 and JSON worker options using build time, startup/IPC overhead, packaged size, debuggability, error handling, and platform support.
- Add a feature flag or explicit fallback so a broken Rust component cannot make existing server management unavailable.
- Do not move multiple domains at once.

### Phase 4 - first production slice

Candidate order, subject to measured benefit and parity evidence:

1. Pure status/action eligibility and world-start classification.
2. Hashing and manifest verification helpers used by export/import.
3. Archive path/member validation as a standalone tested component.
4. Selected CPU-heavy file scanning/fingerprinting.

Do not start with backup orchestration, world swaps, tmux lifecycle execution, Qt, or provider workflows.

### Phase 5 - expand only with evidence

- Port one cohesive module at a time.
- Keep Python wrappers and old implementation until owner usage and CI demonstrate parity.
- Add migration notes and rollback paths for every changed operation.
- Reassess whether a native Rust UI or Rust CLI offers enough value to justify a separate product-level project.

## 9. Initial acceptance criteria

The discovery phase is complete only when:

- The current module/dependency map is complete enough to identify all call sites and shared contracts for the proposed first slice.
- Persistent data formats and paths are documented with fixtures.
- The Python reference behavior can be exercised without touching live server processes or owner data.
- The first Rust slice is pure and has no Qt, tmux, filesystem mutation, or network side effects.
- CI proves Python remains functional and the new Rust crate builds/tests independently. The first pilot has met this criterion on the current PR head; broader Phase 0 fixture/dependency discovery remains incomplete.
- A reviewer can explain exactly how to disable the Rust slice and return to the Python implementation.
- No live server, world, backup, installed Crucible copy, or user registry is modified by the migration scaffolding.

## 10. Verified source references

The following files were inspected on the `main` baseline for this assessment (the complete 80-file inventory and runtime flow details are in `rust-migration-source-audit.md`):

- `README.md`
- `pyproject.toml`
- `crucible/__init__.py`
- `crucible/__main__.py`
- `crucible/data/instance_manager.py`
- `crucible/data/instance_model.py`
- `crucible/data/backup_manager.py`
- `crucible/process/tmux_manager.py`
- `crucible/process/crash_recovery.py`
- `crucible/ui/main_window.py`
- `crucible/ui/instance_panel.py`
- `crucible/mods/modrinth.py`
- `crucible/exporters/client_export.py`
- `crucible/importers/prism.py`
- `get-crucible.sh`
- `install.sh`
- `.github/workflows/tests.yml`
- `docs/development/tray-shortcuts-resource-usage.md`
- `docs/development/client-pack-sharing-roadmap.md`

Crosshair reference files inspected on `Roaring1/Crosshair:rust-port`:

- `Cargo.toml`
- `README.md`
- `src/lib.rs`

The source inventory above is deliberately explicit about what was and was not inspected. The fleet-backup implementation is now inspected in detail, and the top-level package subdirectories/tests are inventoried. A complete per-call-site dependency graph, all UI-to-worker routes, and sanitized compatibility fixtures remain discovery work, not assumed facts. Performance findings and a benchmark plan are in `docs/development/rust-migration-performance-audit.md`. The full module inventory and end-to-end runtime map are in `docs/development/rust-migration-source-audit.md`. Persisted/runtime data contracts and the golden fixture plan are in `docs/development/rust-migration-contracts.md`.
