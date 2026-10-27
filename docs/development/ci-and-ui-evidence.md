# CI failures and UI evidence

## Inspect a failed GitHub Actions run

Crucible's public repository exposes workflow status and job logs through the
GitHub Actions API. From the repository root, run:

    python scripts/ci_report.py

To inspect a specific notification or email, use the run ID from its URL:

    python scripts/ci_report.py --run-id 40581792706

The report shows the run status, failed jobs and steps, and a short, redacted
excerpt around test errors. It uses only Python's standard library. Public run
metadata can be read without a token. GitHub may restrict anonymous job-log
downloads; when that happens, the tool falls back to the machine's authenticated
GitHub CLI (`gh`). For a private repository, authenticate `gh` or set
`GH_TOKEN` to an environment variable containing a token with Actions read
permission. Do not put tokens into command-line arguments, screenshots, or commits.

When a workflow is running, the report says so rather than treating an older
failure as the latest result. A failed run can be revisited with its URL and ID
even after a later run succeeds.

Every Python test matrix job writes the last 80 lines of its pytest log into
the Actions summary and uploads the complete pytest log as an artifact for 14
days. Open the run's **Summary** tab to see the failure without downloading
anything. This preserves evidence even when an email arrives after a subsequent
fix.

## Give Crucible screenshots for review

You do not need to paste image data or convert anything to base64. Put screenshots
in a local folder, or in a Dropbox folder synchronized onto this computer. A
suggested inbox is:

    ~/Pictures/CrucibleAudit/inbox/

Then either inspect the original files directly by their local path, or batch
prepare a group into smaller PNGs plus a contact sheet:

    python scripts/prepare_ui_evidence.py ~/Pictures/CrucibleAudit/inbox
    python scripts/prepare_ui_evidence.py ~/Dropbox/Crucible/screenshots --recursive

The output goes to ~/.cache/crucible/ui-evidence by default; it never changes
the source screenshots. The contact sheet helps find a screen, while individual
PNGs preserve readable text. You can specify a different output folder with
--output. Give the reviewer the local absolute path to the folder or image names;
the images stay on disk and are inspected through local file access, not pasted
into chat as base64.

For screenshots captured during a live session, use the desktop's normal
screenshot tool and save them into the inbox above. For UI changes reproducible
from source, capture the six key screens without opening desktop windows:

    QT_QPA_PLATFORM=offscreen python scripts/capture_ui_previews.py

This writes `main-empty.png`, `add-server-menu.png`, `main-selected.png`,
`create-server.png`, `import-files.png`, and `install-modpack.png` to
`~/.cache/crucible/ui-previews` by default. The create form uses its actual
initial size. The selected-server screen uses a synthetic server in a temporary
registry, while version/modpack searches are disabled and clearly marked as
preview-only. The import form shows the default `~/CrucibleServers` destination
and keeps a folder picker optional; indexed archive downloads run inside the
existing import window. Then create a contact sheet and manifest:

    python scripts/prepare_ui_evidence.py ~/.cache/crucible/ui-previews --output ~/.cache/crucible/ui-previews-contact

Both tools can be pointed at a different directory with `--output`.

## What the UX changes are trying to enforce

- One obvious action for each task: create a clean server, install a Modrinth
  modpack, or import files that already exist.
- Do not open a setup dialog from another setup dialog. Choose the task first,
  then show only its form.
- Keep uncommon technical fields under an explicit advanced/registration toggle.
- Do not register an import until the user commits it. Cancel must not silently
  create an entry in the server list.
- Use source folder/archive names as defaults rather than asking the user to
  type a name unnecessarily; users can rename a server later.
