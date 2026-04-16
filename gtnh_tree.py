#!/usr/bin/env python3
"""
gtnh_tree.py — GTNH Server Instance Filesystem Explorer

Prints a full annotated tree of a GTNH server directory:
  • Indented parent → child hierarchy
  • File type classification (config, mod, world, log, script, etc.)
  • File extension
  • File size

Usage:
    python3 gtnh_tree.py /path/to/server
    python3 gtnh_tree.py /path/to/server --max-depth 4
    python3 gtnh_tree.py /path/to/server --json > tree.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ── File type classification ──────────────────────────────────────────────────

_TYPE_MAP: dict[str, str] = {
    # Mods / Java
    ".jar":        "mod",
    ".class":      "java-class",
    # Configs
    ".cfg":        "forge-config",
    ".conf":       "config",
    ".config":     "config",
    ".toml":       "config",
    ".json":       "json",
    ".json5":      "json",
    ".yaml":       "config",
    ".yml":        "config",
    ".xml":        "config",
    ".ini":        "config",
    ".properties": "properties",
    # Scripts / launchers
    ".sh":         "shell-script",
    ".bat":        "batch-script",
    ".ps1":        "powershell",
    ".py":         "python-script",
    # Logs
    ".log":        "log",
    ".gz":         "compressed-log",
    # World / data
    ".dat":        "world-data",
    ".dat_old":    "world-data-backup",
    ".mca":        "region-chunk",
    ".mcr":        "region-chunk-legacy",
    ".nbt":        "nbt-data",
    ".lck":        "lock-file",
    # Text / docs
    ".txt":        "text",
    ".md":         "markdown",
    ".html":       "html",
    ".htm":        "html",
    # Archives
    ".zip":        "archive",
    ".tar":        "archive",
    ".7z":         "archive",
    # Images / resources
    ".png":        "image",
    ".jpg":        "image",
    ".jpeg":       "image",
    ".gif":        "image",
    ".ogg":        "audio",
    ".wav":        "audio",
    # Mappings / crash reports
    ".csv":        "csv",
    ".tsv":        "tsv",
}

# Directories with known roles
_DIR_ROLES: dict[str, str] = {
    "mods":              "mods",
    "config":            "configs",
    "logs":              "logs",
    "crash-reports":     "crash-reports",
    "world":             "world",
    "saves":             "saves",
    "scripts":           "crafttweaker-scripts",
    "resources":         "resource-pack",
    "libraries":         "forge-libraries",
    "coremods":          "core-mods",
    "asm":               "asm-cache",
    "backups":           "backups",
    "journeymap":        "journeymap",
    "dynmap":            "dynmap",
    "plugins":           "plugins",
    "sponge":            "sponge",
    ".forge":            "forge-internal",
    "server-icon.png":   "server-icon",
}

# Directories to skip entirely (noisy / irrelevant)
_SKIP_DIRS = {
    "__pycache__", ".git", ".idea", ".vscode",
    "node_modules", ".gradle",
}

# Extensions to collapse (show count, not individual files) when large
_COLLAPSE_EXTS = {".mca", ".mcr", ".class"}
_COLLAPSE_THRESHOLD = 8


def classify_file(path: Path) -> str:
    """Return a human-readable type string for a file."""
    suffix = path.suffix.lower()
    name   = path.name.lower()

    # Special filenames
    if name == "eula.txt":         return "eula"
    if name == "server.properties": return "server-config"
    if name == "ops.json":          return "ops-list"
    if name == "whitelist.json":    return "whitelist"
    if name == "banned-players.json": return "ban-list"
    if name == "banned-ips.json":   return "ban-list"
    if name in ("startserver.sh", "serverstart.sh") or name.startswith("startserver"):
        return "start-script"
    if name == "forge.jar" or (name.startswith("forge") and suffix == ".jar"):
        return "forge-loader"
    if name.endswith(".jar.disabled"):
        return "disabled-mod"

    return _TYPE_MAP.get(suffix, "file")


def classify_dir(path: Path) -> str:
    name = path.name.lower()
    return _DIR_ROLES.get(name, "directory")


def human_size(b: int) -> str:
    if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f}GB"
    if b >= 1_048_576:     return f"{b/1_048_576:.0f}MB"
    if b >= 1_024:         return f"{b/1024:.0f}KB"
    return f"{b}B"


# ── Tree node ─────────────────────────────────────────────────────────────────

@dataclass
class TreeNode:
    name:      str
    abs_path:  str
    rel_path:  str
    is_dir:    bool
    role:      str           # classify_file / classify_dir result
    ext:       str           # e.g. ".jar" or "" for dirs
    size:      int           # bytes (0 for dirs — use subtree_size)
    depth:     int
    subtree_size: int = 0   # total bytes for directories
    child_count:  int = 0   # direct children
    children:  list["TreeNode"] = field(default_factory=list)
    collapsed: bool = False  # True when a large group is folded


def build_tree(
    root: Path,
    rel_root: Path,
    depth: int,
    max_depth: int,
) -> TreeNode:
    node = TreeNode(
        name     = root.name or str(root),
        abs_path = str(root.resolve()),
        rel_path = str(rel_root),
        is_dir   = root.is_dir(),
        role     = classify_dir(root) if root.is_dir() else classify_file(root),
        ext      = root.suffix.lower() if not root.is_dir() else "",
        size     = root.stat().st_size if not root.is_dir() else 0,
        depth    = depth,
    )

    if not root.is_dir():
        node.subtree_size = node.size
        return node

    if depth >= max_depth:
        # Count children but don't recurse
        try:
            children = list(root.iterdir())
        except PermissionError:
            children = []
        node.child_count = len(children)
        node.subtree_size = 0
        return node

    try:
        entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except PermissionError:
        return node

    # Group files by extension for collapse logic
    ext_buckets: dict[str, list[Path]] = {}
    dirs: list[Path] = []
    other_files: list[Path] = []

    for e in entries:
        if e.name in _SKIP_DIRS:
            continue
        if e.is_dir():
            dirs.append(e)
        elif e.suffix.lower() in _COLLAPSE_EXTS:
            ext_buckets.setdefault(e.suffix.lower(), []).append(e)
        else:
            other_files.append(e)

    # Recurse into dirs
    for d in dirs:
        child = build_tree(d, rel_root / d.name, depth + 1, max_depth)
        node.children.append(child)
        node.subtree_size += child.subtree_size
        node.child_count  += 1

    # Regular files
    for f in other_files:
        child = build_tree(f, rel_root / f.name, depth + 1, max_depth)
        node.children.append(child)
        node.subtree_size += child.size
        node.child_count  += 1

    # Collapsible extension groups
    for ext, paths in sorted(ext_buckets.items()):
        total_size = sum(p.stat().st_size for p in paths)
        node.subtree_size += total_size
        node.child_count  += len(paths)
        if len(paths) >= _COLLAPSE_THRESHOLD:
            # Synthetic collapsed node
            rep = TreeNode(
                name      = f"[{len(paths)} {ext} files]",
                abs_path  = str(root / paths[0].name),
                rel_path  = str(rel_root / f"*{ext}"),
                is_dir    = False,
                role      = _TYPE_MAP.get(ext, "file"),
                ext       = ext,
                size      = total_size,
                depth     = depth + 1,
                collapsed = True,
            )
            node.children.append(rep)
        else:
            for f in paths:
                child = build_tree(f, rel_root / f.name, depth + 1, max_depth)
                node.children.append(child)

    return node


# ── Rendering ─────────────────────────────────────────────────────────────────

# ANSI colours
_C = {
    "dir":       "\033[1;34m",   # bold blue
    "mod":       "\033[1;35m",   # bold magenta
    "config":    "\033[33m",     # yellow
    "world":     "\033[32m",     # green
    "log":       "\033[36m",     # cyan
    "script":    "\033[1;32m",   # bold green
    "json":      "\033[33m",
    "text":      "\033[37m",
    "collapsed": "\033[2;37m",   # dim white
    "reset":     "\033[0m",
    "dim":       "\033[2m",
    "bold":      "\033[1m",
}

_ROLE_COLOR: dict[str, str] = {
    "mod":                  _C["mod"],
    "disabled-mod":         _C["collapsed"],
    "forge-loader":         _C["mod"],
    "mods":                 _C["mod"],
    "forge-config":         _C["config"],
    "config":               _C["config"],
    "properties":           _C["config"],
    "server-config":        _C["config"],
    "configs":              _C["config"],
    "json":                 _C["json"],
    "ops-list":             _C["json"],
    "whitelist":            _C["json"],
    "ban-list":             _C["json"],
    "world":                _C["world"],
    "world-data":           _C["world"],
    "region-chunk":         _C["world"],
    "region-chunk-legacy":  _C["world"],
    "nbt-data":             _C["world"],
    "log":                  _C["log"],
    "compressed-log":       _C["log"],
    "crash-reports":        _C["log"],
    "logs":                 _C["log"],
    "shell-script":         _C["script"],
    "start-script":         _C["script"],
    "batch-script":         _C["script"],
    "eula":                 _C["text"],
    "text":                 _C["text"],
    "markdown":             _C["text"],
}


def _color(role: str, is_dir: bool) -> str:
    if is_dir:
        return _ROLE_COLOR.get(role, _C["dir"])
    return _ROLE_COLOR.get(role, "")


PIPE  = "│   "
TEE   = "├── "
ELBOW = "└── "
BLANK = "    "


def render_tree(
    node: TreeNode,
    prefix: str = "",
    is_last: bool = True,
    use_color: bool = True,
    show_size: bool = True,
) -> list[str]:
    lines: list[str] = []

    connector = ELBOW if is_last else TEE
    indent    = prefix + connector if node.depth > 0 else ""

    # Size annotation
    size_str = ""
    if show_size:
        if node.is_dir and node.subtree_size > 0:
            size_str = f"  [{human_size(node.subtree_size)}]"
        elif not node.is_dir and node.size > 0:
            size_str = f"  [{human_size(node.size)}]"

    # Build the label
    reset = _C["reset"] if use_color else ""

    if node.collapsed:
        col   = _C["collapsed"] if use_color else ""
        label = f"{col}{node.name}{reset}"
        meta  = f"  {_C['dim'] if use_color else ''}({node.role}){reset}"
    elif node.is_dir:
        col   = _color(node.role, True) if use_color else ""
        name  = node.name + "/"
        label = f"{col}{_C['bold'] if use_color else ''}{name}{reset}"
        meta  = f"  {_C['dim'] if use_color else ''}({node.role}  {node.child_count} items){reset}"
    else:
        col   = _color(node.role, False) if use_color else ""
        label = f"{col}{node.name}{reset}"
        ext_s = f"  {_C['dim'] if use_color else ''}{node.ext}" if node.ext else ""
        meta  = f"{ext_s}  ({node.role}){reset}"

    line = f"{indent}{label}{meta}{size_str}"
    lines.append(line)

    # Recurse
    child_prefix = prefix + (BLANK if is_last else PIPE)
    for i, child in enumerate(node.children):
        last = (i == len(node.children) - 1)
        lines.extend(render_tree(child, child_prefix, last, use_color, show_size))

    return lines


# ── JSON export ───────────────────────────────────────────────────────────────

def node_to_dict(node: TreeNode) -> dict:
    d = {
        "name":          node.name,
        "path":          node.rel_path,
        "abs_path":      node.abs_path,
        "type":          "directory" if node.is_dir else "file",
        "role":          node.role,
        "extension":     node.ext,
        "size_bytes":    node.subtree_size if node.is_dir else node.size,
        "size_human":    human_size(node.subtree_size if node.is_dir else node.size),
        "child_count":   node.child_count,
        "depth":         node.depth,
    }
    if node.children:
        d["children"] = [node_to_dict(c) for c in node.children]
    return d


# ── Summary stats ─────────────────────────────────────────────────────────────

def collect_stats(node: TreeNode, stats: dict) -> None:
    if not node.is_dir and not node.collapsed:
        stats["files"] += 1
        stats["by_role"].setdefault(node.role, 0)
        stats["by_role"][node.role] += 1
        if node.ext:
            stats["by_ext"].setdefault(node.ext, 0)
            stats["by_ext"][node.ext] += 1
    elif node.is_dir:
        stats["dirs"] += 1
    for c in node.children:
        collect_stats(c, stats)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GTNH instance filesystem tree explorer"
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Path to GTNH server directory (default: current dir)"
    )
    parser.add_argument(
        "--max-depth", "-d",
        type=int,
        default=6,
        help="Maximum directory depth to recurse (default: 6)"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON instead of tree"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors"
    )
    parser.add_argument(
        "--no-size",
        action="store_true",
        help="Omit file sizes"
    )
    parser.add_argument(
        "--out", "-o",
        metavar="FILE",
        default=None,
        help="Write plain-text output to FILE (no ANSI codes; easy to upload/read)"
    )
    args = parser.parse_args()

    root = Path(args.path).resolve()
    if not root.exists():
        print(f"Error: path does not exist: {root}", file=sys.stderr)
        sys.exit(1)
    if not root.is_dir():
        print(f"Error: not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    use_color = not args.no_color and sys.stdout.isatty() and args.out is None

    print(f"\nScanning {root} …\n", file=sys.stderr)
    tree = build_tree(root, Path("."), depth=0, max_depth=args.max_depth)

    if args.json:
        print(json.dumps(node_to_dict(tree), indent=2))
        return

    # ── Build output lines (always plain when writing to file) ──────────────
    def _build_output(color: bool) -> list[str]:
        out: list[str] = []
        reset = _C["reset"] if color else ""
        bold  = _C["bold"]  if color else ""
        dim   = _C["dim"]   if color else ""

        out.append(f"\nGTNH Server Tree")
        out.append(f"{tree.abs_path}")
        out.append(f"Total size: {human_size(tree.subtree_size)}  |  Max depth: {args.max_depth}\n")

        out.extend(render_tree(tree, prefix="", is_last=True,
                               use_color=color, show_size=not args.no_size))

        stats: dict = {"files": 0, "dirs": 0, "by_role": {}, "by_ext": {}}
        collect_stats(tree, stats)

        out.append(f"\n── Summary ────────────────────────────────────────────")
        out.append(f"  Directories : {stats['dirs']}")
        out.append(f"  Files       : {stats['files']}")
        out.append(f"  Total size  : {human_size(tree.subtree_size)}")

        out.append(f"\n  By role:")
        for role, count in sorted(stats["by_role"].items(), key=lambda x: -x[1])[:20]:
            col = (_ROLE_COLOR.get(role, "") if color else "")
            out.append(f"    {col}{role:<28}{reset}  {count}")

        out.append(f"\n  By extension:")
        for ext, count in sorted(stats["by_ext"].items(), key=lambda x: -x[1])[:15]:
            out.append(f"    {ext:<16}  {count}")

        out.append("")
        return out

    # ── Write to file if --out was given ────────────────────────────────────
    if args.out:
        plain_lines = _build_output(color=False)
        out_path = Path(args.out)
        out_path.write_text("\n".join(plain_lines), encoding="utf-8")
        print(f"Tree written to: {out_path.resolve()}", file=sys.stderr)
        # Still print a short summary to the terminal
        stats: dict = {"files": 0, "dirs": 0, "by_role": {}, "by_ext": {}}
        collect_stats(tree, stats)
        print(f"  {stats['dirs']} dirs, {stats['files']} files, "
              f"{human_size(tree.subtree_size)} total", file=sys.stderr)
        return

    # ── Normal stdout render ─────────────────────────────────────────────────
    print("\n".join(_build_output(color=use_color)))


if __name__ == "__main__":
    main()
