# Crucible Changelog

## v0.3.1

### Bug Fixes

**Console tab now shows actual server output for GTNH 1.7.10**

In GTNH 1.7.10 (FML era), the server writes all meaningful output — mod loading,
GT recipe compilation, player activity, crash traces — to `logs/fml-server-latest.log`,
not `logs/latest.log`. `latest.log` exists but contains only a brief boot wrapper
(typically a handful of lines). The console tab was tailing the wrong file.

`get_log_path()` now resolves in priority order:
1. `logs/fml-server-latest.log` (GTNH 1.7.10 primary log)
2. `logs/latest.log` (vanilla / 1.12+ Forge / fallback)

`LogWatcher` follows the same resolution automatically.

The Info tab LOG row now appends `(FML primary log)` when `fml-server-latest.log`
is the active file, so it's clear which log is being tailed.

---

**Mods tab now surfaces nested/bundled jars**

`ModManager.list_mods()` previously only scanned the top level of `mods/`.
Jars inside subdirectories (e.g. `mods/ic2/EJML-core-0.26.jar`) were completely
invisible — not shown, not counted, not inspectable.

These "bundled" jars are now listed at the bottom of the mods table with a 🔒
icon and a `[bundled in <subdir>/]` label. Enable/disable/delete actions are
suppressed for them (they must be managed manually). Hovering the lock icon
shows a tooltip explaining why.

The footer count now reads e.g. `212 enabled  ·  0 disabled  ·  1 bundled (not manageable)`.

The Info tab MODS row now reads e.g. `212 enabled  +  1 bundled (unmanaged)`.

---

**`validate()` warns about nested jars**

If any `.jar` files are found inside `mods/` subdirectories, `validate()` now
emits a warning listing them so they appear in the Info tab's Validation Issues
section. Previously this situation was silently ignored.

---

**`get_bundled_jars()` helper added to `ServerInstance`**

New method that returns a list of `Path` objects for all `.jar` files nested
inside `mods/` subdirectories. Used by `validate()`, the Info tab, and available
for future tooling.

---

## v0.3.0

Initial public release.
