# Crucible UX audit

## Scope

This pass reviewed the first-run screen, Add server entry point, create-server form,
import form, modpack form, selected-server navigation, and the failure/return paths
between them. Screens were rendered from the real Qt widgets in offscreen mode,
then checked visually. No production server registry or world data was changed.

## Design principles used

- **Make the next step obvious.** A new installation has one prominent Add server
  action. That opens a short menu with three distinct tasks: create a server,
  install a modpack, or import existing files.
- **Show only the options needed for the current task.** The default import screen
  keeps manual registration details collapsed. The create form keeps snapshot and
  loader-version overrides under More version options. The server workspace keeps
  five primary pages visible and moves Config, Players, Notes, and Info under More.
- **Prefer safe defaults over extra questions.** Server folders are named from the
  server/pack name, collisions are given a free suffix, and the ordinary import
  flow defaults to ~/CrucibleServers. Choosing another destination is optional.
  Creating or importing a server refuses to overwrite existing files.
- **Prevent errors instead of teaching users to dismiss them.** Add server is
  disabled until an import is ready or a folder is selected. Import failures stay
  in the current window and identify where partial files may remain. Indexed
  archive imports attempt best-effort mod downloads in the same operation, with
  progress and logs in the import window.
- **Use direct, consistent language.** Forms use Server software, Minecraft
  version, Server name, and Server folder. “Create a server” is distinct from
  “Install a modpack” and “Import existing files”.

These principles follow the established usability heuristics for error prevention,
recognition rather than recall, minimalist design, system-status visibility, and
clear recovery guidance. References:

- Nielsen Norman Group, [10 usability heuristics for user interface design](https://www.nngroup.com/articles/ten-usability-heuristics/)
- GOV.UK Design System, [Error message](https://design-system.service.gov.uk/components/error-message/)
- GOV.UK Service Manual, [Designing good questions](https://www.gov.uk/service-manual/design/designing-good-questions)
- DWP Design System, [Hint text](https://design-system.dwp.gov.uk/guidance/hint-text)

## Changes covered by regression tests

- One Add server menu routes to Create, Install a modpack, or Import existing files.
- First-run state hides server-only tabs and actions; it explains where to start.
- Five primary tabs remain visible; the More menu opens secondary tabs and tracks
  the selected page.
- Create-server advanced version settings start collapsed; folder naming avoids
  both on-disk and registry collisions; a non-empty directory and a leaf symlink
  are rejected without modifying their targets.
- Import registration is deferred until Add server. The commit action is disabled
  before a source import or a manual folder is ready.
- Import destination defaults visibly to ~/CrucibleServers. A Change action can
  override it, but importing does not force another folder-picker window.
- Archive imports with an index attempt server-mod downloads in the same dialog.
  Installed Prism folders and full server archives without an index do not produce
  a misleading “no index” warning. Copy/download logs and progress stay in the
  same window; errors do not open another modal message box.
- Import destinations avoid existing folders, symbolic links, and paths already
  reserved in Crucible's registry.
- GitHub Actions triage redacts common credentials and emits bounded log excerpts.
- UI preview capture creates six repeatable PNGs without launching desktop windows
  or reading/writing the real server registry.

## Reproducible review commands

Run all tests:

    python -m pytest -q -p no:cacheprovider

Capture the six standard screens:

    QT_QPA_PLATFORM=offscreen python scripts/capture_ui_previews.py

Create an indexed contact sheet of those images:

    python scripts/prepare_ui_evidence.py ~/.cache/crucible/ui-previews --output ~/.cache/crucible/ui-previews-contact

Inspect the latest or a specific GitHub Actions run:

    python scripts/ci_report.py
    python scripts/ci_report.py --run-id RUN_ID

## Limits and next validation

This is a heuristic/code audit plus screenshot review, not a moderated usability
study. It cannot establish that a first-time player will find every feature without
help. Offscreen screenshots are useful for hierarchy and clipping checks, but the
final app should also be checked on the user's normal desktop theme and display
scaling. Mod downloads depend on pack metadata, network access, and the APIs the
pack uses; unavailability is reported rather than guaranteed to be repairable.
Some operational warnings (for example, missing Java for a loader installer or a
failed server-program download) remain explicit because they affect whether the
server can run.


## Next product workflow: Share with players

The next planned usability step is one primary **Share with players** action that
uses a saved, known-good client profile, validates a Prism instance ZIP, checks
what will be shared, then uploads it and copies the link. This is a separate
workflow from ordinary export: network upload must not begin before the pack has
passed validation and the user has reviewed the file list and privacy warnings.

The phased design, safety requirements, and acceptance checks are in the
[client-pack sharing roadmap](client-pack-sharing-roadmap.md) and
[Crucible issue #5](https://github.com/Roaring1/Crucible/issues/5). A code review
also found a current exporter mismatch: `defaultconfigs/` was included even
though the checkbox named only `config/` and `kubejs/`. The immediate fix is
to exclude `defaultconfigs/` from player exports and warn when that directory
exists; no hosted upload is implemented by that fix.
