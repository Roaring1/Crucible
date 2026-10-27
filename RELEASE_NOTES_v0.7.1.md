# Crucible v0.7.1 — Modpack updates, and Forge startup answers in the GUI

## Modpack updates

Crucible can now update an installed modpack from the GTNH version index, or from any pasted link, local zip, or bare version number. Updates are staged and swapped rather than applied in place: the previous install is kept alongside the new one, so `rollback` restores it and `prune-previous` removes it only when you say so. Archives are probed before extraction, and anything that does not look like a modpack asks for confirmation first.

New commands: `check-updates`, `update`, `rollback`, `prune-previous`, `probe-pack`. In the GUI: **Setup → Modpack version** and the Mods tab's **Update pack…** button.

## Forge startup answers in the GUI

GUI Start now handles legacy Forge/FML `confirm` and `cancel` questions. Configure **Setup → Legacy Forge startup questions**, or choose when Start asks. The choice is passed as `-Dfml.queryResult=confirm` or `-Dfml.queryResult=cancel`, including through `JAVA_TOOL_OPTIONS` for third-party scripts. Existing servers default to Ask me for safety.

This matters right after a pack update: the first boot on a changed mod list is exactly when Forge asks, and answering `confirm` permanently deletes content removed by the update.
