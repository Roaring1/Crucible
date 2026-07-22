"""
crucible/__main__.py

CLI entry point.  Run as:
    python -m crucible [command] [args]

Or, after `pip install -e .`:
    crucible [command] [args]

Commands
────────
  list                    List all registered server instances
  add <path>              Register a server directory
  remove <name|id>        Unregister an instance (files untouched)
  start  <name|id>        Start the server via tmux
  stop   <name|id>        Stop the server gracefully (sends 'stop', waits)
  restart <name|id>       Stop then start
  status [name|id]        Show running/stopped status (all if no arg)
  attach <name|id>        Open the server console in a new terminal window
  send   <name|id> <cmd>  Send a command to the server console
  scan   <path>           Scan a directory tree for server installs
  scan-prism <path>       Scan for Prism/MultiMC instances
  import-prism <src> <dst> Import a Prism instance/modpack as a server folder
  validate [name|id]      Validate instance paths and configuration
  info   <name|id>        Show full details for one instance
  edit   <name|id>        Edit instance metadata (name, version, notes…)
"""

from __future__ import annotations

import argparse
import sys

from .data.instance_manager import InstanceManager, validate_session_name
from .process.tmux_manager import TmuxManager
from .utils import (
    BOLD, DIM, GREEN, YELLOW, CYAN, RESET,
    ok, warn, err, info, dim, banner, status_dot,
)


# Shared helper

def resolve_instance(manager: InstanceManager, key: str):
    """Look up an instance by name or ID prefix, exit on failure."""
    inst = manager.get_by_name_or_id(key)
    if inst is None:
        err(f"No instance found for: {key!r}")
        dim("Run 'crucible list' to see registered instances.")
        sys.exit(1)
    return inst


# Command handlers

def cmd_list(manager: InstanceManager, tmux: TmuxManager, _args) -> None:
    if not manager.instances:
        dim("No instances registered.")
        dim("Add one with:  crucible add /path/to/server")
        return

    status_map = tmux.status_map(manager.instances)

    hdr = (
        f"\n  {BOLD}{'NAME':<24} {'VER':<8} {'STATUS':<10} "
        f"{'MODS':<6} {'SESSION':<22} PATH{RESET}"
    )
    sep = f"  {'─'*24} {'─'*8} {'─'*10} {'─'*6} {'─'*22} {'─'*28}"
    print(hdr)
    print(sep)

    for inst in manager.instances:
        status = status_map.get(inst.id, "stopped")
        dot    = status_dot(status)
        col    = GREEN if status == "running" else DIM

        problems = inst.validate()
        name_col = inst.name
        if problems:
            name_col = f"{inst.name} {YELLOW}⚠{RESET}"

        mods = str(inst.get_mod_count()) if not problems else f"{DIM}?{RESET}"

        print(
            f"  {dot} {name_col:<24} "
            f"{inst.version:<8} "
            f"{col}{status:<10}{RESET} "
            f"{mods:<6} "
            f"{DIM}{inst.tmux_session:<22}{RESET} "
            f"{DIM}{inst.path}{RESET}"
        )

    print()


def cmd_add(manager: InstanceManager, args) -> None:
    from pathlib import Path
    path    = args.path
    name    = args.name or Path(path).name
    session = args.session or ""
    version = args.version

    try:
        inst = manager.add_instance(path, name, version, tmux_session=session)
    except ValueError as exc:
        err(str(exc))
        sys.exit(1)

    ok(f"Registered '{inst.name}'")
    info(f"Path:         {inst.path}")
    info(f"tmux session: {CYAN}{inst.tmux_session}{RESET}")
    info(f"ID:           {DIM}{inst.id}{RESET}")

    problems = inst.validate()
    if problems:
        print()
        warn("Validation warnings (server may not start correctly):")
        for p in problems:
            dim(f"    {p}")


def cmd_remove(manager: InstanceManager, args) -> None:
    inst = resolve_instance(manager, args.name)

    # Confirm
    print(f"\n  Remove '{inst.name}' from registry?")
    print(f"  {DIM}Path: {inst.path}{RESET}")
    print(f"  {DIM}Files on disk will NOT be deleted.{RESET}\n")
    try:
        reply = input("  Confirm [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        reply = "n"

    if reply != "y":
        dim("Aborted.")
        return

    manager.remove_instance(inst.id)
    ok(f"Removed '{inst.name}' from registry (files untouched)")


def _preflight_properties(inst) -> None:
    """Auto-repair crash-causing server.properties values before starting.

    Catches the classic blank/garbled numeric setting (e.g. ``server-port=``)
    that makes Minecraft die with ``NumberFormatException`` before it boots.
    """
    from pathlib import Path
    from .data import properties as props
    path = Path(inst.path) / "server.properties"
    if not props.has_blocking_errors(path):
        return
    res = props.autorepair_file(path, only_errors=True)
    if res.changed:
        warn("Fixed server.properties values that would have crashed the server:")
        for k, o, n in res.changed:
            dim(f"    {k}: '{o}' -> '{n}'")
        if res.backup_path:
            dim(f"    (backup saved: {res.backup_path})")


def cmd_fix_properties(manager: InstanceManager, args) -> None:
    """Detect & fix invalid server.properties values (the 'did you mean' helper)."""
    from pathlib import Path
    from .data import properties as props
    inst = resolve_instance(manager, args.name)
    path = Path(inst.path) / "server.properties"
    if not path.exists():
        warn("This server has no server.properties yet (it is generated on first start).")
        return
    issues = props.validate_file(path)
    if not issues:
        ok("No problems found in server.properties.")
        return
    errors = [i for i in issues if i.is_error]
    warn(f"Found {len(issues)} issue(s) ({len(errors)} would crash the server):")
    for i in issues:
        tag = "CRASH" if i.is_error else "note "
        suggestion = f"  -> did you mean '{i.suggestion}'?" if i.suggestion else ""
        dim(f"    [{tag}] {i.message}{suggestion}")
    apply_fixes = getattr(args, "apply", False)
    full = getattr(args, "all", False)
    if not apply_fixes:
        print()
        info("Re-run with --apply to fix the crash-causing values automatically.")
        info("Add --all to also apply suggested fixes for the advisory items.")
        return
    res = props.autorepair_file(path, only_errors=not full, fix_unknown_keys=full)
    ok(res.summary())
    for k, o, n in res.changed:
        dim(f"    {k}: '{o}' -> '{n}'")
    if res.backup_path:
        dim(f"    backup: {res.backup_path}")
    if any(i.is_error for i in res.remaining):
        err("Some crash-causing values could not be auto-fixed; edit them manually.")


def cmd_fix_loading(manager: InstanceManager, args) -> None:
    """Diagnose & fix server start/loading crashes (e.g. client-only mods)."""
    from pathlib import Path
    from .diagnostics import loadcheck as lc
    inst = resolve_instance(manager, args.name)
    root = Path(inst.path)

    # --restore: re-enable everything Crucible previously quarantined.
    if getattr(args, "restore", False):
        restored = lc.restore_quarantined(root)
        if restored:
            ok(f"Re-enabled {len(restored)} mod(s): " + ", ".join(restored))
        else:
            dim("No quarantined mods to restore.")
        return

    # --scan: static best-effort scan for client-only mods (no crash needed).
    if getattr(args, "scan", False):
        flagged = lc.scan_client_only(root)
        if not flagged:
            ok("No mods statically declare themselves client-only.")
        else:
            warn(f"{len(flagged)} mod(s) declare environment=client:")
            for name, reason in flagged:
                dim(f"    {name}  ({reason})")
            info("These only run on the client. Disable them with the Mods tab "
                 "or by running with --apply after a start attempt.")
        return

    log_text = None
    if getattr(args, "log", None):
        p = Path(args.log)
        if not p.exists():
            err(f"Log file not found: {p}")
            sys.exit(1)
        log_text = p.read_text(encoding="utf-8", errors="replace")

    apply_fix = getattr(args, "apply", False)
    res = lc.autofix_loading(root, apply=apply_fix, log_text=log_text)
    diag = res.diagnosis

    if not diag.found_crash:
        ok("No crash report found for this server. If it just crashed, start it "
           "once more so a crash report is written, then re-run this command.")
        return

    if diag.source:
        dim(f"Analysed: {diag.source}")
    if diag.is_clean:
        warn("A crash log was found, but no known loading problem was recognised.")
        dim("Open the crash report above for the full stack trace.")
        return

    warn("Loading problems detected:")
    for line in diag.human_summary().splitlines():
        dim("    " + line)

    culprits = diag.client_on_server_modids
    if not culprits:
        info("No client-only mod could be auto-quarantined. Resolve the issues above manually.")
        return

    if not apply_fix:
        print()
        if res.quarantined:
            info("Re-run with --apply to disable these client-only mod(s): "
                 + ", ".join(res.quarantined))
        if res.unresolved:
            dim("Could not locate jars for: " + ", ".join(res.unresolved))
        return

    if res.quarantined:
        ok(f"Disabled {len(res.quarantined)} client-only mod(s): "
           + ", ".join(res.quarantined))
        dim("They were renamed to *.jar.disabled (re-enable any time with "
            "'crucible fix-loading <name> --restore' or the Mods tab).")
        info("Now try starting the server again.")
    if res.unresolved:
        warn("Could not find jars for: " + ", ".join(res.unresolved))
        dim("Remove these client-only mods from mods/ manually.")


def cmd_start(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    inst = resolve_instance(manager, args.name)
    _preflight_properties(inst)
    success, msg = tmux.start(inst)

    if success:
        manager.update_instance(inst)  # persist last_started
        ok(msg)
        info(f"Console: {CYAN}tmux attach -t {inst.tmux_session}{RESET}")
        info(f"Or use:  {CYAN}crucible attach {inst.name}{RESET}")
    else:
        err(msg)
        sys.exit(1)


def cmd_stop(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    inst  = resolve_instance(manager, args.name)
    force = args.force

    if force:
        warn(f"Force-killing '{inst.name}' (no world save!)")
    else:
        info(f"Stopping '{inst.name}' gracefully (timeout: {args.timeout}s)…")

    success, msg = tmux.stop(inst, graceful=not force, timeout_s=args.timeout)

    if success:
        ok(msg)
    else:
        err(msg)
        sys.exit(1)


def cmd_restart(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    inst = resolve_instance(manager, args.name)

    if tmux.is_running(inst):
        info(f"Stopping '{inst.name}'…")
        success, msg = tmux.stop(inst, graceful=True, timeout_s=args.timeout)
        if not success:
            err(f"Stop failed: {msg}")
            sys.exit(1)
        ok(msg)
    else:
        info("Server was not running — starting fresh")

    success, msg = tmux.start(inst)
    if success:
        manager.update_instance(inst)
        ok(msg)
        info(f"Console: {CYAN}tmux attach -t {inst.tmux_session}{RESET}")
    else:
        err(msg)
        sys.exit(1)


def cmd_status(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    if args.name:
        instances = [resolve_instance(manager, args.name)]
    else:
        instances = manager.instances

    if not instances:
        dim("No instances registered.")
        return

    status_map = tmux.status_map(instances)

    print()
    for inst in instances:
        status = status_map.get(inst.id, "stopped")
        dot    = status_dot(status)
        col    = GREEN if status == "running" else DIM
        print(
            f"  {dot}  {inst.name:<28} "
            f"{col}{status:<10}{RESET}"
            f"  {DIM}{inst.tmux_session}{RESET}"
        )
        if status == "running":
            log = inst.get_log_path()
            if log:
                dim(f"       log: {log}")
    print()


def cmd_attach(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    inst = resolve_instance(manager, args.name)
    terminal = getattr(args, "terminal", "auto")

    success, msg = tmux.attach(inst, terminal=terminal)
    if success:
        ok(msg)
    else:
        err(msg)
        # Always print the manual fallback
        info(f"Manual: {CYAN}tmux attach -t {inst.tmux_session}{RESET}")
        sys.exit(1)


def cmd_send(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    inst    = resolve_instance(manager, args.name)
    command = " ".join(args.command)

    if not tmux.is_running(inst):
        err(f"'{inst.name}' is not running")
        sys.exit(1)

    if tmux.send_command(inst, command):
        ok(f"Sent: {CYAN}{command!r}{RESET}")
    else:
        err("send-keys failed")
        sys.exit(1)


def cmd_scan(manager: InstanceManager, args) -> None:
    from pathlib import Path
    search = Path(args.path).expanduser()
    info(f"Scanning {search} for Minecraft server directories…")

    found = manager.find_server_dirs(search, max_depth=args.depth)

    if not found:
        dim(f"No server directories found under {search}")
        return

    registered = {inst.path for inst in manager.instances}
    print(f"\n  Found {len(found)} candidate(s):\n")

    for p in sorted(found):
        path_str = str(p)
        already  = path_str in registered
        flag     = f"  {DIM}(already registered){RESET}" if already else ""
        print(f"  {CYAN}→{RESET}  {p}{flag}")

    print()
    if any(str(p) not in registered for p in found):
        info(f"Register with: {CYAN}crucible add <path>{RESET}")
    print()


def cmd_scan_prism(_manager: InstanceManager, args) -> None:
    from .importers.prism import scan_prism_instances, detect_prism_source
    found = scan_prism_instances(args.path, max_depth=args.depth)
    if not found:
        dim(f"No Prism instances found under {args.path}")
        return
    print(f"\n  Found {len(found)} Prism instance(s):\n")
    for p in found:
        try:
            plan = detect_prism_source(p)
            label = plan.info.version_label
            print(f"  {CYAN}→{RESET}  {p}  {DIM}{plan.info.name} {label}{RESET}")
        except Exception:
            print(f"  {CYAN}→{RESET}  {p}")
    print()


def cmd_import_prism(manager: InstanceManager, args) -> None:
    from pathlib import Path
    from .importers.prism import import_prism_source
    try:
        info_obj = import_prism_source(
            args.source, args.target,
            accept_eula=args.accept_eula,
            overwrite=args.overwrite,
            download_mods=getattr(args, "download_mods", False),
            download_log_cb=lambda m: dim(f"    {m}"),
            install_server=getattr(args, "install_server", False),
            install_log_cb=lambda m: dim(f"    {m}"),
        )
    except Exception as exc:
        err(str(exc))
        sys.exit(1)

    name = args.name or info_obj.name or Path(args.target).name
    version = args.version or info_obj.version_label
    try:
        inst = manager.add_instance(
            args.target,
            name,
            version,
            pack_source=info_obj.source_type,
            minecraft_version=info_obj.minecraft_version,
            loader=info_obj.loader,
            loader_version=info_obj.loader_version,
            prism_source=args.source,
        )
    except ValueError as exc:
        warn(str(exc))
        inst = None

    ok(f"Imported Prism-compatible source to {args.target}")
    if inst:
        info(f"Registered: {inst.name} ({inst.short_id()})")
    if info_obj.warnings:
        warn("Import warnings:")
        for w in info_obj.warnings:
            dim(f"    {w}")
    info(f"Read next: {args.target}/CRUCIBLE_IMPORT.md")


def cmd_download_mods(manager: InstanceManager, args) -> None:
    """Best-effort download of a server's pack mods from its stored index."""
    from .importers.downloader import download_pack_mods, has_downloadable_index
    inst = resolve_instance(manager, args.name)
    if not has_downloadable_index(inst.path):
        warn("No pack index found for this server (nothing to download).")
        info("This is normal for fully-installed imports that already include mod jars.")
        return
    info(f"Downloading mods for {inst.name}…")
    try:
        result = download_pack_mods(inst.path, log_cb=lambda m: dim(f"    {m}"))
    except Exception as exc:  # download_pack_mods shouldn't raise, but be safe
        err(f"Download failed unexpectedly: {exc}")
        sys.exit(1)
    ok("Download finished: " + result.summary())
    if result.failed:
        warn("Could not download these (grab them manually or set CURSEFORGE_API_KEY):")
        for name, reason in result.failed:
            dim(f"    {name}: {reason}")


def cmd_accept_eula(manager: InstanceManager, args) -> None:
    """Write eula=true for a server (user is accepting the Minecraft EULA)."""
    inst = resolve_instance(manager, args.name)
    try:
        inst.set_eula_accepted(True)
    except Exception as exc:
        err(f"Could not write eula.txt: {exc}")
        sys.exit(1)
    ok(f"eula.txt set to true for {inst.name}")
    info("You have indicated agreement to the Minecraft EULA (https://aka.ms/MinecraftEULA).")


def cmd_list_mc_versions(manager: InstanceManager, args) -> None:
    """List available Minecraft versions from Mojang's manifest."""
    from .importers import serverloader as sl
    info("Fetching Minecraft version list…")
    versions = sl.list_versions(include_snapshots=getattr(args, "snapshots", False))
    if not versions:
        err("Could not fetch the version list (offline?).")
        dim("You can still create a server by passing an explicit --mc version.")
        sys.exit(1)
    latest = sl.latest_release()
    if latest:
        ok(f"Latest release: {latest}")
    shown = versions[: getattr(args, "limit", 30) or 30]
    for v in shown:
        marker = "  ← latest" if v.id == latest else ""
        dim(f"  {v.id:16} ({v.type}){marker}")
    if len(versions) > len(shown):
        dim(f"  …and {len(versions) - len(shown)} more (use --limit to see more)")


def cmd_create_server(manager: InstanceManager, args) -> None:
    """Create a brand-new server from scratch (vanilla is the simplest)."""
    from pathlib import Path
    from .importers import serverloader as sl

    loader = sl.normalize_loader(args.loader or "vanilla")
    mc = args.mc
    if not mc:
        mc = sl.latest_release()
        if not mc:
            err("Could not determine a Minecraft version. Pass one with --mc (e.g. --mc 1.21.1).")
            sys.exit(1)
        info(f"No --mc given; using latest release {mc}")

    if args.dir:
        target = Path(args.dir).expanduser()
    else:
        name_slug = "".join(c for c in (args.name or f"{loader}-{mc}") if c.isalnum() or c in "-_.").strip() or "server"
        target = Path.home() / "CrucibleServers" / name_slug

    info(f"Creating {loader} server for Minecraft {mc} at {target}…")
    result = sl.create_fresh_server(
        target,
        minecraft_version=mc,
        loader=loader,
        loader_version=args.loader_version or "",
        accept_eula=args.accept_eula,
        overwrite=args.overwrite,
        log_cb=lambda m: dim(f"    {m}"),
    )

    # Register regardless — the folder is well-formed even if the download failed.
    version_label = f"MC {mc}" + (f" · {loader}" if loader != "vanilla" else "")
    try:
        inst = manager.add_instance(
            str(target), args.name or target.name, version_label,
            pack_source=f"new_{loader}",
            minecraft_version=mc,
            loader=("" if loader == "vanilla" else loader),
            loader_version=args.loader_version or "",
        )
        info(f"Registered: {inst.name} ({inst.short_id()})")
    except ValueError as exc:
        warn(str(exc))

    if result.ok:
        ok("Server created: " + result.summary())
        if not args.accept_eula:
            dim("Run 'crucible accept-eula <name>' (and read the Minecraft EULA) before starting.")
    else:
        err("Server program not installed: " + (result.failed_reason or "unknown"))
        dim("The folder was still created. Retry with internet via 'crucible install-server <name>'.")


def cmd_install_modpack(manager: InstanceManager, args) -> None:
    """Install a Modrinth modpack (or local .mrpack) as a ready-to-run server."""
    from pathlib import Path
    from .importers import modpack_auto

    if args.dir:
        target = Path(args.dir).expanduser()
    else:
        base = args.name or args.id_or_slug or "modpack"
        slug = "".join(c for c in base if c.isalnum() or c in "-_.").strip() or "modpack"
        target = Path.home() / "CrucibleServers" / slug

    if args.mrpack:
        src = Path(args.mrpack).expanduser()
        info(f"Installing modpack from {src} into {target}\u2026")
        result = modpack_auto.install_modpack_from_mrpack(
            src, target,
            accept_eula=args.accept_eula,
            log_cb=lambda m: dim(f"    {m}"),
        )
    else:
        if not args.id_or_slug:
            err("Provide a Modrinth modpack id/slug, or use --mrpack FILE.")
            sys.exit(1)
        info(f"Installing modpack '{args.id_or_slug}' into {target}\u2026")
        result = modpack_auto.install_modpack_from_modrinth(
            args.id_or_slug, target,
            mc_version=args.mc or "",
            loader=args.loader or "",
            accept_eula=args.accept_eula,
            log_cb=lambda m: dim(f"    {m}"),
        )

    # Register regardless \u2014 the folder is well-formed even on partial failure.
    name = args.name or Path(result.path or target).name
    version_label = f"MC {result.minecraft_version}" if result.minecraft_version else ""
    if result.loader:
        version_label = (version_label + f" \u00b7 {result.loader}").strip(" \u00b7")
    try:
        inst = manager.add_instance(
            result.path or str(target), name, version_label,
            pack_source="modrinth_modpack",
            minecraft_version=result.minecraft_version,
            loader=result.loader,
            loader_version=result.loader_version,
        )
        info(f"Registered: {inst.name} ({inst.short_id()})")
    except ValueError as exc:
        warn(str(exc))

    if result.ok:
        ok("Modpack installed: " + result.summary())
        if not args.accept_eula:
            dim("Run 'crucible accept-eula <name>' (and read the Minecraft EULA) before starting.")
    else:
        err("Modpack install failed: " + (result.failed_reason or "unknown"))
        dim("The folder was still created. Retry with internet, or check the log above.")


def cmd_install_server(manager: InstanceManager, args) -> None:
    """Install / repair the dedicated server program for a registered instance."""
    from .importers import serverloader as sl
    inst = resolve_instance(manager, args.name)
    mc = args.mc or inst.minecraft_version
    loader = sl.normalize_loader(args.loader or inst.loader or "vanilla")
    if not mc:
        err("This instance has no recorded Minecraft version. Pass one with --mc.")
        sys.exit(1)
    info(f"Installing {loader} server program for {inst.name} (MC {mc})…")
    result = sl.install_server_loader(
        inst.path,
        minecraft_version=mc,
        loader=loader,
        loader_version=args.loader_version or inst.loader_version or "",
        log_cb=lambda m: dim(f"    {m}"),
    )
    if result.ok:
        ok("Installed: " + result.summary())
    else:
        err("Install failed: " + (result.failed_reason or "unknown"))
        if sl.requires_java(loader):
            dim("This loader needs Java on PATH to run its installer. Vanilla and Fabric don't.")
        sys.exit(1)


def cmd_doctor(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    """Friendly readiness check: is this server ready for friends to join?"""
    if args.name:
        instances = [resolve_instance(manager, args.name)]
    else:
        instances = manager.instances
    if not instances:
        warn("No instances registered yet. Import a modpack or add a server folder first.")
        return
    for inst in instances:
        print(f"\n  {BOLD}{inst.name}{RESET}  {DIM}({inst.short_id()}){RESET}")
        try:
            items = inst.readiness()
        except Exception as exc:
            err(f"    readiness check failed: {exc}")
            continue
        for item in items:
            state = item.get("ok")
            label = item.get("label", "")
            detail = item.get("detail", "")
            line = f"  {label}: {detail}"
            if state is True:
                ok(line)
            elif state is False:
                err(line)
            else:
                warn(line)
        # Helpful next-step hints
        fixes = [i.get("fix") for i in items if i.get("fix")]
        if "accept_eula" in fixes:
            dim(f"      → run: crucible accept-eula {inst.name!r}")
        if "install_server" in fixes:
            dim("      → import a fully-installed Prism instance, or add the server jar/loader")


def cmd_validate(manager: InstanceManager, args) -> None:
    if args.name:
        instances = [resolve_instance(manager, args.name)]
    else:
        instances = manager.instances

    if not instances:
        dim("No instances to validate.")
        return

    all_ok = True
    print()
    for inst in instances:
        problems = inst.validate()
        if problems:
            all_ok = False
            print(f"  {YELLOW}⚠{RESET}  {inst.name}  {DIM}({inst.short_id()}){RESET}")
            for p in problems:
                dim(f"       {p}")
        else:
            mods = inst.get_mod_count()
            print(
                f"  {GREEN}✓{RESET}  {inst.name}  "
                f"{DIM}({mods} mods){RESET}"
            )
    print()

    if not all_ok:
        sys.exit(1)


def cmd_info(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    inst   = resolve_instance(manager, args.name)
    status = tmux.get_status(inst)

    print(f"""
  {BOLD}{inst.name}{RESET}  {DIM}({inst.id}){RESET}

  {BOLD}Path:{RESET}         {inst.path}
  {BOLD}Version:{RESET}      {inst.version}
  {BOLD}tmux session:{RESET} {CYAN}{inst.tmux_session}{RESET}
  {BOLD}Status:{RESET}       {status_dot(status)}  {status}
  {BOLD}Java args:{RESET}    {inst.java_args}
  {BOLD}Mods:{RESET}         {inst.get_mod_count()} enabled
  {BOLD}Worlds:{RESET}       {', '.join(inst.get_world_names()) or 'none found'}
  {BOLD}Log:{RESET}          {inst.get_log_path() or 'not found'}
  {BOLD}Start script:{RESET} {inst.get_startscript() or 'NOT FOUND'}
  {BOLD}Created:{RESET}      {inst.created_at}
  {BOLD}Last started:{RESET} {inst.last_started or 'never (via Crucible)'}
""")

    problems = inst.validate()
    if problems:
        warn("Validation problems:")
        for p in problems:
            dim(f"    {p}")
        print()

    if inst.notes.strip():
        print(f"  {BOLD}Notes:{RESET}")
        for line in inst.notes.splitlines():
            dim(f"    {line}")
        print()


def cmd_edit(manager: InstanceManager, args) -> None:
    inst = resolve_instance(manager, args.name)
    changed = False

    if args.rename:
        old_name = inst.name
        inst.name = args.rename
        # Update session name to match new name (unless manually set)
        ok(f"Renamed '{old_name}' → '{inst.name}'")
        changed = True

    if args.version:
        inst.version = args.version
        ok(f"Version set to '{inst.version}'")
        changed = True

    if args.session:
        try:
            session = validate_session_name(args.session)
        except ValueError as exc:
            err(str(exc))
            sys.exit(2)
        conflict = next((i for i in manager.instances
                         if i.id != inst.id and i.tmux_session == session), None)
        if conflict:
            err(f"tmux session '{session}' is already used by '{conflict.name}'")
            sys.exit(2)
        inst.tmux_session = session
        ok(f"tmux session set to '{inst.tmux_session}'")
        changed = True

    if args.java_args:
        inst.java_args = args.java_args
        ok(f"java_args set to '{inst.java_args}'")
        changed = True

    if args.notes:
        inst.notes = args.notes
        ok("Notes updated")
        changed = True

    if args.color:
        inst.color = args.color
        ok(f"Color set to '{inst.color}'")
        changed = True

    if changed:
        manager.update_instance(inst)
        ok("Registry saved")
    else:
        dim("Nothing changed. Use --help to see edit options.")


def cmd_gui(manager: InstanceManager) -> None:
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QIcon
    except ImportError:
        err("PyQt6 is not installed.")
        dim("Install with:  pip install PyQt6")
        sys.exit(1)

    import sys as _sys
    from pathlib import Path
    from .ui.theme import STYLESHEET
    from .ui.main_window import MainWindow

    app = QApplication(_sys.argv)
    app.setApplicationName("Crucible")
    app.setApplicationDisplayName("Crucible — Minecraft Server Manager")
    # Required for KDE/Wayland task manager and icon-only task manager to pick up
    # the icon.  The string must match the base name of the .desktop file
    # (crucible.desktop) and the Icon= entry inside it.
    app.setDesktopFileName("crucible")
    app.setStyleSheet(STYLESHEET)

    # Set window icon (shows on X11 task bar and as fallback on Wayland)
    _icon_path = Path(__file__).resolve().parent / "assets" / "crucible.png"
    if _icon_path.exists():
        app.setWindowIcon(QIcon(str(_icon_path)))

    win = MainWindow(manager)
    win.show()

    _sys.exit(app.exec())


# Argument parser

def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog        = "crucible",
        description = "Crucible — Minecraft Server Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = root.add_subparsers(dest="command", metavar="<command>")

    # gui
    sub.add_parser("gui", help="Launch the graphical interface (requires PyQt6)")

    # list
    sub.add_parser("list", help="List all registered instances")

    # add
    p_add = sub.add_parser("add", help="Register a server directory")
    p_add.add_argument("path",              help="Path to the Minecraft server directory")
    p_add.add_argument("--name",            help="Display name (default: directory name)")
    p_add.add_argument("--version",         default="", help="Server / modpack version label")
    p_add.add_argument(
        "--session",
        metavar="NAME",
        help=(
            "tmux session name to use (default: auto-derived from display name). "
            "Use this to match an existing session, e.g. --session gtnh"
        ),
    )

    # remove
    p_rm = sub.add_parser("remove", help="Unregister an instance (files untouched)")
    p_rm.add_argument("name", help="Instance name or ID prefix")

    # start
    p_start = sub.add_parser("start", help="Start the server via tmux")
    p_start.add_argument("name", help="Instance name or ID prefix")

    # fix-properties
    p_fixprops = sub.add_parser(
        "fix-properties", help="Check & fix invalid server.properties values")
    p_fixprops.add_argument("name", help="Instance name or ID prefix")
    p_fixprops.add_argument(
        "--apply", action="store_true", help="Apply fixes (default: just report)")
    p_fixprops.add_argument(
        "--all", action="store_true",
        help="Also apply advisory fixes & rename typo'd keys")

    # fix-loading
    p_fixload = sub.add_parser(
        "fix-loading",
        help="Diagnose & fix start/loading crashes (client-only mods, etc.)")
    p_fixload.add_argument("name", help="Instance name or ID prefix")
    p_fixload.add_argument(
        "--apply", action="store_true",
        help="Quarantine the offending client-only mod(s) (default: just report)")
    p_fixload.add_argument(
        "--scan", action="store_true",
        help="Statically scan mods/ for client-only mods (no crash log needed)")
    p_fixload.add_argument(
        "--restore", action="store_true",
        help="Re-enable mods Crucible previously quarantined")
    p_fixload.add_argument(
        "--log", metavar="FILE",
        help="Analyse a specific crash/log file instead of the newest one")

    # stop
    p_stop = sub.add_parser("stop", help="Stop the server gracefully")
    p_stop.add_argument("name", help="Instance name or ID prefix")
    p_stop.add_argument("--force",   action="store_true", help="Force-kill (no world save)")
    p_stop.add_argument("--timeout", type=int, default=90, metavar="S", help="Graceful timeout in seconds (default: 90)")

    # restart
    p_restart = sub.add_parser("restart", help="Stop then start the server")
    p_restart.add_argument("name", help="Instance name or ID prefix")
    p_restart.add_argument("--timeout", type=int, default=90, metavar="S", help="Graceful stop timeout (default: 90)")

    # status
    p_status = sub.add_parser("status", help="Show running/stopped status")
    p_status.add_argument("name", nargs="?", help="Instance name or ID (omit for all)")

    # attach
    p_attach = sub.add_parser("attach", help="Open server console in a new terminal window")
    p_attach.add_argument("name", help="Instance name or ID prefix")
    p_attach.add_argument(
        "--terminal",
        default="auto",
        choices=["auto", "konsole", "kitty", "alacritty", "gnome-terminal", "xterm"],
        help="Terminal emulator to use (default: auto-detect)",
    )

    # send
    p_send = sub.add_parser("send", help="Send a command to the server console")
    p_send.add_argument("name",           help="Instance name or ID prefix")
    p_send.add_argument("command", nargs="+", help="Command to send (e.g. say hello)")

    # scan
    p_scan = sub.add_parser("scan", help="Scan a directory tree for Minecraft server installs")
    p_scan.add_argument("path",            help="Directory to scan")
    p_scan.add_argument("--depth", type=int, default=3, metavar="N", help="Max recursion depth (default: 3)")

    # scan-prism
    p_scan_prism = sub.add_parser("scan-prism", help="Scan for Prism/MultiMC instances")
    p_scan_prism.add_argument("path", help="Directory to scan, e.g. ~/.local/share/PrismLauncher/instances")
    p_scan_prism.add_argument("--depth", type=int, default=4, metavar="N", help="Max recursion depth (default: 4)")

    # import-prism
    p_import_prism = sub.add_parser("import-prism", help="Import Prism/MultiMC/Modrinth/CurseForge source as a server folder")
    p_import_prism.add_argument("source", help="Prism instance folder or .zip/.mrpack")
    p_import_prism.add_argument("target", help="Destination server folder")
    p_import_prism.add_argument("--name", help="Registered server name")
    p_import_prism.add_argument("--version", help="Registered version label")
    p_import_prism.add_argument("--overwrite", action="store_true", help="Allow importing into a non-empty target")
    p_import_prism.add_argument("--accept-eula", action="store_true", help="Write eula=true. Only use if you accept the Minecraft EULA.")
    p_import_prism.add_argument("--download-mods", action="store_true", help="After import, try to download mods listed in the pack index (needs internet).")
    p_import_prism.add_argument("--install-server", action="store_true", help="After import, try to install the dedicated server program if the pack didn't ship one (needs internet).")

    # create-server
    p_create = sub.add_parser("create-server", help="Create a brand-new server (vanilla is easiest: just pick a version)")
    p_create.add_argument("--mc", help="Minecraft version (default: latest release)")
    p_create.add_argument("--loader", default="vanilla", help="vanilla (default), fabric, neoforge, forge, or quilt")
    p_create.add_argument("--loader-version", help="Loader version (default: latest)")
    p_create.add_argument("--name", help="Registered server name")
    p_create.add_argument("--dir", help="Folder to create the server in (default: ~/CrucibleServers/<name>)")
    p_create.add_argument("--overwrite", action="store_true", help="Allow creating into a non-empty folder")
    p_create.add_argument("--accept-eula", action="store_true", help="Write eula=true. Only use if you accept the Minecraft EULA.")

    # install-modpack
    p_imp = sub.add_parser("install-modpack", help="Install a Modrinth modpack (or local .mrpack) as a ready-to-run server")
    p_imp.add_argument("id_or_slug", nargs="?", help="Modrinth modpack id or slug (omit when using --mrpack)")
    p_imp.add_argument("--mrpack", help="Install from a local .mrpack file instead of Modrinth")
    p_imp.add_argument("--dir", help="Folder to install the server in (default: ~/CrucibleServers/<name>)")
    p_imp.add_argument("--name", help="Registered server name")
    p_imp.add_argument("--mc", help="Preferred Minecraft version (default: the pack's own)")
    p_imp.add_argument("--loader", help="Preferred loader override (default: the pack's own)")
    p_imp.add_argument("--accept-eula", action="store_true", help="Write eula=true. Only use if you accept the Minecraft EULA.")

    # list-mc-versions
    p_lmv = sub.add_parser("list-mc-versions", help="List available Minecraft versions from Mojang")
    p_lmv.add_argument("--snapshots", action="store_true", help="Include snapshot versions")
    p_lmv.add_argument("--limit", type=int, default=30, help="How many to show (default 30)")

    # install-server
    p_is = sub.add_parser("install-server", help="Install/repair the dedicated server program for an instance")
    p_is.add_argument("name", help="Instance name or ID prefix")
    p_is.add_argument("--mc", help="Minecraft version (default: the instance's recorded version)")
    p_is.add_argument("--loader", help="Loader override (default: the instance's recorded loader)")
    p_is.add_argument("--loader-version", help="Loader version (default: latest / recorded)")

    # download-mods
    p_dl = sub.add_parser("download-mods", help="Try to download a server's mods from its imported pack index")
    p_dl.add_argument("name", help="Instance name or ID prefix")

    # accept-eula
    p_eula = sub.add_parser("accept-eula", help="Write eula=true for a server (accept the Minecraft EULA)")
    p_eula.add_argument("name", help="Instance name or ID prefix")

    # doctor
    p_doctor = sub.add_parser("doctor", help="Friendly readiness check: is the server ready for friends to join?")
    p_doctor.add_argument("name", nargs="?", help="Instance name or ID (omit for all)")

    # validate
    p_val = sub.add_parser("validate", help="Validate instance paths and config")
    p_val.add_argument("name", nargs="?", help="Instance name or ID (omit for all)")

    # info
    p_info = sub.add_parser("info", help="Show full details for one instance")
    p_info.add_argument("name", help="Instance name or ID prefix")

    # edit
    p_edit = sub.add_parser("edit", help="Edit instance metadata")
    p_edit.add_argument("name",            help="Instance name or ID prefix")
    p_edit.add_argument("--rename",        metavar="NAME",    help="New display name")
    p_edit.add_argument("--version",       metavar="VER",     help="Server / modpack version string")
    p_edit.add_argument("--session",       metavar="SESSION", help="tmux session name")
    p_edit.add_argument("--java-args",     metavar="ARGS",    help="JVM arguments")
    p_edit.add_argument("--notes",         metavar="TEXT",    help="Notes (replaces existing)")
    p_edit.add_argument("--color",         metavar="HEX",     help="Accent color e.g. #7c3aed")

    return root


# Entry point

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    if args.command is None:
        print(banner())
        parser.print_help()
        sys.exit(0)

    # GUI launches without the CLI banner (it has its own window)
    if args.command != "gui":
        print(banner())

    manager = InstanceManager()
    manager.load()
    tmux = TmuxManager()

    dispatch = {
        "gui":      lambda: cmd_gui(manager),
        "list":     lambda: cmd_list(manager, tmux, args),
        "add":      lambda: cmd_add(manager, args),
        "remove":   lambda: cmd_remove(manager, args),
        "start":    lambda: cmd_start(manager, tmux, args),
        "fix-properties": lambda: cmd_fix_properties(manager, args),
        "fix-loading": lambda: cmd_fix_loading(manager, args),
        "stop":     lambda: cmd_stop(manager, tmux, args),
        "restart":  lambda: cmd_restart(manager, tmux, args),
        "status":   lambda: cmd_status(manager, tmux, args),
        "attach":   lambda: cmd_attach(manager, tmux, args),
        "send":     lambda: cmd_send(manager, tmux, args),
        "scan":     lambda: cmd_scan(manager, args),
        "scan-prism": lambda: cmd_scan_prism(manager, args),
        "import-prism": lambda: cmd_import_prism(manager, args),
        "create-server": lambda: cmd_create_server(manager, args),
        "list-mc-versions": lambda: cmd_list_mc_versions(manager, args),
        "install-server": lambda: cmd_install_server(manager, args),
        "install-modpack": lambda: cmd_install_modpack(manager, args),
        "download-mods": lambda: cmd_download_mods(manager, args),
        "accept-eula": lambda: cmd_accept_eula(manager, args),
        "doctor":   lambda: cmd_doctor(manager, tmux, args),
        "validate": lambda: cmd_validate(manager, args),
        "info":     lambda: cmd_info(manager, tmux, args),
        "edit":     lambda: cmd_edit(manager, args),
    }

    fn = dispatch.get(args.command)
    if fn is None:
        err(f"Unknown command: {args.command!r}")
        sys.exit(1)

    fn()


if __name__ == "__main__":
    main()
