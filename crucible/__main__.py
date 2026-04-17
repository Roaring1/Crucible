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
  scan   <path>           Scan a directory tree for GTNH server installs
  validate [name|id]      Validate instance paths and configuration
  info   <name|id>        Show full details for one instance
  edit   <name|id>        Edit instance metadata (name, version, notes…)
"""

from __future__ import annotations

import argparse
import sys

from .data.instance_manager import InstanceManager
from .process.tmux_manager import TmuxManager
from .utils import (
    BOLD, DIM, GREEN, YELLOW, RED, CYAN, RESET,
    ok, warn, err, info, dim, banner, status_dot,
)


# ── Shared helper ─────────────────────────────────────────────────────────────

def resolve_instance(manager: InstanceManager, key: str):
    """Look up an instance by name or ID prefix, exit on failure."""
    inst = manager.get_by_name_or_id(key)
    if inst is None:
        err(f"No instance found for: {key!r}")
        dim(f"Run 'crucible list' to see registered instances.")
        sys.exit(1)
    return inst


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_list(manager: InstanceManager, tmux: TmuxManager, _args) -> None:
    if not manager.instances:
        dim("No instances registered.")
        dim("Add one with:  crucible add /path/to/GTNH-Server")
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


def cmd_start(manager: InstanceManager, tmux: TmuxManager, args) -> None:
    inst = resolve_instance(manager, args.name)
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
    info(f"Scanning {search} for GTNH server directories…")

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
        inst.tmux_session = args.session
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
        from PyQt6.QtCore import Qt
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
    app.setApplicationDisplayName("Crucible — GTNH Server Manager")
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


# ── Argument parser ────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog        = "crucible",
        description = "Crucible — GTNH Server Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = root.add_subparsers(dest="command", metavar="<command>")

    # ── gui ──
    sub.add_parser("gui", help="Launch the graphical interface (requires PyQt6)")

    # ── list ──
    sub.add_parser("list", help="List all registered instances")

    # ── add ──
    p_add = sub.add_parser("add", help="Register a server directory")
    p_add.add_argument("path",              help="Path to the GTNH server directory")
    p_add.add_argument("--name",            help="Display name (default: directory name)")
    p_add.add_argument("--version",         default="2.8.4", help="GTNH version (default: 2.8.4)")
    p_add.add_argument(
        "--session",
        metavar="NAME",
        help=(
            "tmux session name to use (default: auto-derived from display name). "
            "Use this to match an existing session, e.g. --session gtnh"
        ),
    )

    # ── remove ──
    p_rm = sub.add_parser("remove", help="Unregister an instance (files untouched)")
    p_rm.add_argument("name", help="Instance name or ID prefix")

    # ── start ──
    p_start = sub.add_parser("start", help="Start the server via tmux")
    p_start.add_argument("name", help="Instance name or ID prefix")

    # ── stop ──
    p_stop = sub.add_parser("stop", help="Stop the server gracefully")
    p_stop.add_argument("name", help="Instance name or ID prefix")
    p_stop.add_argument("--force",   action="store_true", help="Force-kill (no world save)")
    p_stop.add_argument("--timeout", type=int, default=90, metavar="S", help="Graceful timeout in seconds (default: 90)")

    # ── restart ──
    p_restart = sub.add_parser("restart", help="Stop then start the server")
    p_restart.add_argument("name", help="Instance name or ID prefix")
    p_restart.add_argument("--timeout", type=int, default=90, metavar="S", help="Graceful stop timeout (default: 90)")

    # ── status ──
    p_status = sub.add_parser("status", help="Show running/stopped status")
    p_status.add_argument("name", nargs="?", help="Instance name or ID (omit for all)")

    # ── attach ──
    p_attach = sub.add_parser("attach", help="Open server console in a new terminal window")
    p_attach.add_argument("name", help="Instance name or ID prefix")
    p_attach.add_argument(
        "--terminal",
        default="auto",
        choices=["auto", "konsole", "kitty", "alacritty", "gnome-terminal", "xterm"],
        help="Terminal emulator to use (default: auto-detect)",
    )

    # ── send ──
    p_send = sub.add_parser("send", help="Send a command to the server console")
    p_send.add_argument("name",           help="Instance name or ID prefix")
    p_send.add_argument("command", nargs="+", help="Command to send (e.g. say hello)")

    # ── scan ──
    p_scan = sub.add_parser("scan", help="Scan a directory tree for GTNH server installs")
    p_scan.add_argument("path",            help="Directory to scan")
    p_scan.add_argument("--depth", type=int, default=3, metavar="N", help="Max recursion depth (default: 3)")

    # ── validate ──
    p_val = sub.add_parser("validate", help="Validate instance paths and config")
    p_val.add_argument("name", nargs="?", help="Instance name or ID (omit for all)")

    # ── info ──
    p_info = sub.add_parser("info", help="Show full details for one instance")
    p_info.add_argument("name", help="Instance name or ID prefix")

    # ── edit ──
    p_edit = sub.add_parser("edit", help="Edit instance metadata")
    p_edit.add_argument("name",            help="Instance name or ID prefix")
    p_edit.add_argument("--rename",        metavar="NAME",    help="New display name")
    p_edit.add_argument("--version",       metavar="VER",     help="GTNH version string")
    p_edit.add_argument("--session",       metavar="SESSION", help="tmux session name")
    p_edit.add_argument("--java-args",     metavar="ARGS",    help="JVM arguments")
    p_edit.add_argument("--notes",         metavar="TEXT",    help="Notes (replaces existing)")
    p_edit.add_argument("--color",         metavar="HEX",     help="Accent color e.g. #7c3aed")

    return root


# ── Entry point ───────────────────────────────────────────────────────────────

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
        "stop":     lambda: cmd_stop(manager, tmux, args),
        "restart":  lambda: cmd_restart(manager, tmux, args),
        "status":   lambda: cmd_status(manager, tmux, args),
        "attach":   lambda: cmd_attach(manager, tmux, args),
        "send":     lambda: cmd_send(manager, tmux, args),
        "scan":     lambda: cmd_scan(manager, args),
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
