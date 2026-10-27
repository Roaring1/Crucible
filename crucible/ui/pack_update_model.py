"""
crucible/ui/pack_update_model.py

PyQt-free glue between the GUI and crucible.importers.pack_update.

Every decision the Update-pack and Rollback dialogs make -- which release to
install, whether a pasted source is trustworthy, what to warn about, and what
to write back to the registry afterwards -- lives here so it can be tested
headlessly, exactly like the rest of the pack-update feature. The dialog
module owns widgets and threads only.

Nothing in here raises for an expected failure: a GUI that shows a traceback
in the middle of a modpack update is worse than one that says "I did not do
that, and here is why". Hard stops come back as .error, "nothing to do"
comes back as .note, and neither ever touches the disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ARROW = "\u2192"


def human_bytes(size: float) -> str:
    """Byte count as something a person can read."""
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size or 0)
    idx = 0
    while abs(value) >= 1024.0 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {units[idx]}"


def resolve_running(instance):
    """Is this server up? None means "could not tell", which the planner
    treats as a blocker rather than a green light."""
    try:
        from ..process.tmux_manager import TmuxManager
        return TmuxManager().probe_running(instance)
    except Exception:
        return None


@dataclass
class UpdatePreview:
    """The whole update, worked out before a single byte is written."""

    identity: object | None = None      # PackIdentity
    source: object | None = None        # PackSource, when the user supplied one
    probe: object | None = None         # ArchiveProbe, for a local archive
    target: object | None = None        # PackRelease
    plan: object | None = None          # UpdatePlan
    local_archive: str = ""
    note: str = ""                      # friendly "nothing to do" / offline text
    error: str = ""                     # hard stop; nothing was planned

    @property
    def ok(self) -> bool:
        """True only when this preview is safe to apply as-is."""
        return bool(not self.error and self.plan is not None and self.plan.ok)

    @property
    def questions(self) -> list:
        return list(getattr(self.source, "questions", None) or [])

    @property
    def warnings(self) -> list:
        out = list(getattr(self.source, "warnings", None) or [])
        out.extend(getattr(self.probe, "notes", None) or [])
        out.extend(getattr(self.plan, "warnings", None) or [])
        return out

    @property
    def blockers(self) -> list:
        out = list(getattr(self.probe, "problems", None) or [])
        out.extend(getattr(self.plan, "blockers", None) or [])
        return out

    @property
    def needs_confirmation(self) -> bool:
        """True when a human should look hard before this is installed."""
        if self.source is not None and self.source.needs_confirmation:
            return True
        if self.probe is not None and not self.probe.ok:
            return True
        return False

    @property
    def confirm_word(self) -> str:
        """What the user must type to arm the update (mirrors the CLI)."""
        return getattr(self.target, "version", "") or ""

    def headline(self) -> str:
        current = self.identity.label() if self.identity is not None else "unknown pack"
        if self.target is None:
            return current
        return f"{current}  {ARROW}  {self.target.version}"


def build_preview(
    instance,
    *,
    source_text: str = "",
    to_version: str = "",
    include_prerelease: bool = False,
    running: bool | None = False,
    allow_downgrade: bool = False,
    as_version: str = "",
    log_cb=None,
) -> UpdatePreview:
    """Work out exactly what an update would do. Reads only.

    With no source_text this follows the pack's own release index. With one,
    it accepts anything the CLI accepts: a direct link, a release index URL,
    a local zip, or a bare version string. A local archive is probed from its
    central directory only -- nothing is extracted here.
    """
    from ..importers import pack_update as pu

    log = log_cb or (lambda _m: None)
    preview = UpdatePreview()

    try:
        ident = pu.identify_pack(instance)
    except Exception as exc:
        preview.error = f"Could not work out which pack is installed: {exc}"
        return preview
    preview.identity = ident

    target = None
    local_archive = None
    text = (source_text or "").strip()

    if not text:
        # No source given: follow the pack's own index.
        if to_version:
            try:
                releases = pu.fetch_releases(log_cb=log)
            except pu.PackUpdateError as exc:
                preview.error = str(exc)
                return preview
            target = pu.find_release(releases, to_version)
            if target is None:
                preview.error = f"Version {to_version!r} is not in the pack index."
                return preview
        else:
            ident, newest, _releases, note = pu.check_for_update(
                instance, include_prerelease=include_prerelease, log_cb=log)
            preview.identity = ident
            preview.note = note
            if newest is None:
                # Up to date, offline, or an unidentifiable pack. All three are
                # "nothing to plan", not errors -- the user can still paste a link.
                return preview
            target = newest
    else:
        source = pu.resolve_pack_source(text, expected_variant=ident.variant)
        preview.source = source
        if not source.usable:
            preview.error = (source.questions[0] if source.questions else
                             "Crucible could not tell what that link or file is.")
            return preview

        if source.kind == pu.SOURCE_INDEX_URL:
            try:
                releases = pu.fetch_releases(url=source.url, log_cb=log)
            except pu.PackUpdateError as exc:
                preview.error = str(exc)
                return preview
            if to_version:
                target = pu.find_release(releases, to_version)
                if target is None:
                    preview.error = f"Version {to_version!r} is not in that index."
                    return preview
            else:
                target = pu.newest_release(
                    releases, include_prerelease=include_prerelease)
                if target is None:
                    preview.error = "That index has no installable server pack."
                    return preview
        elif source.kind == pu.SOURCE_INDEX_VERSION:
            try:
                releases = pu.fetch_releases(log_cb=log)
            except pu.PackUpdateError as exc:
                preview.error = str(exc)
                return preview
            target = pu.find_release(releases, source.version)
            if target is None:
                preview.error = f"Version {source.version!r} is not in the pack index."
                return preview
        else:
            probe = None
            if source.kind == pu.SOURCE_LOCAL_FILE:
                try:
                    probe = pu.probe_archive(source.path)
                except Exception as exc:
                    preview.error = f"Could not read that archive: {exc}"
                    return preview
                preview.probe = probe
                if probe.problems:
                    # A client pack / index-only mrpack / non-zip stops here,
                    # before anything is extracted.
                    preview.error = probe.problems[0]
                    return preview
                local_archive = source.path or None
            try:
                target = pu.release_from_source(source, probe=probe)
            except Exception as exc:
                preview.error = f"Could not read that source: {exc}"
                return preview
            if as_version:
                target.version = as_version
                target.channel = pu.channel_of(as_version)

    preview.target = target
    preview.local_archive = str(local_archive or "")

    try:
        plan = pu.plan_update(
            instance, target,
            running=running,
            identity=preview.identity,
            installed_java=pu.installed_java_major(),
            allow_downgrade=allow_downgrade,
            local_archive=local_archive,
        )
    except Exception as exc:
        preview.error = f"Could not build an update plan: {exc}"
        return preview

    preview.plan = plan
    return preview


def describe_preview(preview: UpdatePreview) -> list:
    """The plan as reviewable lines, in the order a human should read them."""
    lines: list = []
    ident = preview.identity
    if ident is not None:
        lines.append(f"Installed:  {ident.label()}   [detected via {ident.confidence}]")
        if not ident.known:
            lines.extend(f"            {line}" for line in (ident.evidence or []))
    if preview.error:
        lines.append("")
        lines.append(f"Cannot continue: {preview.error}")
        return lines
    if preview.target is None:
        if preview.note:
            lines.append("")
            lines.append(preview.note)
        return lines

    lines.append(f"Target:     {preview.target.label()}")
    if preview.source is not None:
        lines.append(f"Source:     {preview.source.label()}")
    plan = preview.plan
    if plan is not None and plan.download_url:
        lines.append(f"Download:   {plan.download_url}")
    if preview.local_archive:
        lines.append(f"Install from: {preview.local_archive}")
    if preview.probe is not None:
        lines.append(f"Archive:    {preview.probe.label()}")
    changelog = getattr(preview.target, "changelog_url", "")
    if changelog:
        lines.append(f"Changelog:  {changelog}")

    if plan is not None:
        lines.append("")
        lines.append(f"Kept from the current install ({len(plan.preserve)}):")
        lines.extend(f"    {name}" for name in plan.preserve)
        if plan.carry_mods:
            lines.append("")
            lines.append(f"Your added mods, carried over ({len(plan.carry_mods)}):")
            lines.extend(f"    {name}" for name in plan.carry_mods)

    questions = preview.questions
    if questions:
        lines.append("")
        lines.append(f"Please confirm ({len(questions)}):")
        lines.extend(f"    {line}" for line in questions)

    warnings = preview.warnings
    if warnings:
        lines.append("")
        lines.append(f"Warnings ({len(warnings)}):")
        lines.extend(f"    {line}" for line in warnings)

    blockers = preview.blockers
    if blockers:
        lines.append("")
        lines.append(f"Blockers ({len(blockers)}) -- nothing will be changed:")
        lines.extend(f"    {line}" for line in blockers)

    return lines


def backup_callback(instance, plan, *, log_cb=None):
    """The mandatory pre-update world backup, or None when there is no world.

    Injected into apply_update rather than imported by it, so the importer
    stays free of the UI's BackupManager.
    """
    try:
        world = instance.world_root_path()
    except Exception:
        return None
    if not world or not Path(world).is_dir():
        return None

    current = getattr(getattr(plan, "current", None), "version", "") or "unknown"
    target = getattr(getattr(plan, "target", None), "version", "") or "unknown"
    label = f"Before update {current} {ARROW} {target}"

    def _backup() -> str:
        from ..data.backup_manager import BackupManager
        if log_cb:
            log_cb("Backing up the world before touching anything...")
        return str(BackupManager(instance).create_backup(slot_name=label))

    return _backup


def sync_registry_after_update(manager, instance, *, to_version: str,
                               provider: str = "") -> str:
    """Keep the registry honest about what is now installed.

    Mirrors the CLI: record the new version, and fill in the GTNH defaults
    only when they are missing (never overwrite what the user set).
    """
    if not to_version:
        return ""
    try:
        instance.version = to_version
        if provider == "gtnh":
            if not getattr(instance, "minecraft_version", ""):
                instance.minecraft_version = "1.7.10"
            if not getattr(instance, "loader", ""):
                instance.loader = "forge"
    except Exception as exc:
        return f"Could not update the instance record: {exc}"
    if manager is None:
        return f"Recorded version {to_version} for this session."
    try:
        manager.update_instance(instance)
    except KeyError:
        return "This server is not in the registry, so its recorded version was left alone."
    except Exception as exc:
        return f"Could not save the registry: {exc}"
    return f"Registry updated: version = {to_version}"


def rollback_entries(instance) -> list:
    """Kept pre-update installs, newest first. Never raises."""
    try:
        from ..importers import pack_update as pu
        return list(pu.list_previous_installs(instance))
    except Exception:
        return []


def prune_preview(instance, keep: int = 1) -> tuple:
    """Split kept installs into (kept, doomed) for a given keep count."""
    entries = rollback_entries(instance)
    keep = max(int(keep), 0)
    return entries[:keep], entries[keep:]


def describe_rollback(entry, *, keep_current_world: bool) -> list:
    """What a rollback is about to do, in plain language."""
    lines = [f"Roll back to: {entry.label()}", f"    {entry.path}", ""]
    if keep_current_world:
        lines.append(
            "The CURRENT world will be carried onto the old pack. If the newer "
            "version damaged it, that carries the damage back too.")
    else:
        lines.append("The world from before the update will be restored with it.")
    lines.append("The install that is live now will be moved aside, not deleted.")
    return lines


FIX_HINTS = {
    "accept_eula": "Fix: accept the Minecraft EULA -- Setup tab, or run "
                   "'crucible accept-eula'.",
    "install_server": "Fix: install the server program -- Setup tab, or run "
                      "'crucible install-server'.",
    "start_once": "Fix: start the server once so it can finish setting "
                  "itself up.",
}


def readiness_report(instance) -> list:
    """After an update: what, if anything, is not ready to start.

    Returns [] when every check the instance knows how to make passed, so
    callers can stay quiet on the happy path. Never raises -- a readiness
    check that fails must not hide the result of the update itself.
    """
    try:
        items = instance.readiness()
    except Exception as exc:
        return [f"Could not finish the readiness check: {exc}"]

    lines = []
    for item in items or []:
        state = item.get("ok")
        if state is True:
            continue
        label = item.get("label") or item.get("key") or "Check"
        detail = str(item.get("detail") or "").strip()
        mark = "?" if state is None else "!"
        lines.append(f"[{mark}] {label}" + (f" -- {detail}" if detail else ""))
        hint = FIX_HINTS.get(str(item.get("fix") or ""))
        if hint:
            lines.append("    " + hint)

    if not lines:
        return []
    return ["Not ready to start yet:"] + lines
