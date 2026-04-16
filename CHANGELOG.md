# Changelog

## v0.3.2 — 2026-04-16

### Fixed / Improved
- **Install is now genuinely self-contained.** `install.sh` works from anywhere —
  Downloads, Desktop, /tmp — it copies itself to `~/.local/share/crucible/` before
  installing, so the extracted zip folder is fully disposable after install.
- **No more editable-mode (`-e`) install.** The old approach left the source code
  sitting wherever you extracted the zip and required it to stay there forever.
  v0.3.2 does a real install: you can delete the zip and extracted folder immediately.
- **Auto-cleanup prompt.** At the end of install, the script offers to delete
  the extracted folder for you.
- **One-liner installer.** `get-crucible.sh` lets you install with a single
  `bash <(curl …)` command — no manual downloading or extracting.
- **Flat zip structure.** Removed the confusing nested `build/` folder.
  The zip now extracts to `crucible_v0_3_2/` with `install.sh` at the top level.
- **PATH fix is automatic.** If `~/.local/bin` wasn't in your PATH, the installer
  adds it to `~/.bashrc` without asking.

## v0.3.1 — 2026-04-16

- Initial release with GUI and CLI
- tmux-backed server management
- Backup tab, mods tab, config tab, console tab, players tab
- Instance registry at `~/.config/crucible/instances.json`
