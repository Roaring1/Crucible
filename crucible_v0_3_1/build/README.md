# Crucible

**GTNH Server Manager** — Stage 1 (CLI)

Crucible is a server management tool for GT: New Horizons dedicated servers
running on Linux. It wraps your existing tmux workflow into a clean command
interface and persists server registry to `~/.config/crucible/instances.json`.

The server process lives in tmux completely independently of Crucible —
closing or restarting Crucible never affects a running server.

---

## Install

```bash
# From the crucible/ directory:
pip install -e .

# Or without installing (run directly):
python -m crucible [command]
```

Requires Python 3.11+ and `tmux` (`sudo dnf install tmux`).

---

## Quick Start

```bash
# Register your existing test server
# Use --session gtnh to match your current bare session name
crucible add ~/GTNH-Server-TEST --name "Test Server" --session gtnh

# Or let Crucible generate a session name (will be gtnh-test-server)
crucible add ~/GTNH-Server-TEST --name "Test Server"

# Check what's registered
crucible list

# Start the server
crucible start "Test Server"

# Check status
crucible status

# Open the console in Konsole
crucible attach "Test Server"

# Send a command
crucible send "Test Server" forge tps

# Stop gracefully (sends 'stop', waits up to 90s)
crucible stop "Test Server"

# Force kill (no world save — use carefully)
crucible stop "Test Server" --force
```

---

## All Commands

| Command | Description |
|---|---|
| `list` | List all registered instances with status |
| `add <path>` | Register a server directory |
| `remove <name>` | Unregister (files untouched) |
| `start <name>` | Start via tmux |
| `stop <name>` | Stop gracefully (or `--force`) |
| `restart <name>` | Stop + start |
| `status [name]` | Show running/stopped (all if no arg) |
| `attach <name>` | Open console in a new terminal window |
| `send <name> <cmd>` | Send a command to the console |
| `scan <path>` | Find GTNH server dirs under a path |
| `validate [name]` | Check instance health |
| `info <name>` | Show full instance details |
| `edit <name>` | Edit metadata (name, version, session…) |

### `add` options
```
--name      Display name (default: directory name)
--version   GTNH version string (default: 2.8.4)
--session   tmux session name (default: auto-derived)
            Use --session gtnh to match an existing bare session
```

### `stop` options
```
--force     Kill session immediately (no world save)
--timeout N Graceful stop timeout in seconds (default: 90)
```

### `attach` options
```
--terminal  konsole | kitty | alacritty | gnome-terminal | xterm | auto
            Default: auto (tries konsole first on KDE/Nobara)
```

### `edit` options
```
--rename     New display name
--version    GTNH version string
--session    tmux session name
--java-args  JVM flags
--notes      Notes text (replaces existing)
--color      Accent hex color for future GUI (#7c3aed)
```

---

## Registry

Stored at `~/.config/crucible/instances.json`.  
Written atomically (write to `.tmp` → rename) — a crash mid-save never corrupts it.

```json
{
  "version": 1,
  "instances": [
    {
      "id": "uuid-here",
      "path": "/home/roaring/GTNH-Server-TEST",
      "name": "Test Server",
      "version": "2.8.4",
      "notes": "",
      "java_args": "-Xms16G -Xmx16G",
      "color": "#7c3aed",
      "tmux_session": "gtnh",
      "created_at": "2025-04-15T10:00:00",
      "last_started": null
    }
  ]
}
```

---

## Relationship with `gtnh_deploy.py`

`gtnh_deploy.py` is your **setup tool** — it handles zip extraction, Java detection,
`ServerStart.sh` generation, EULA acceptance, and systemd service installation.
Run it once per server.

Crucible is your **control panel** — it handles start/stop/console for servers
that `gtnh_deploy.py` already set up. They don't conflict.

Note: `gtnh_deploy.py` installs a systemd service but you're using tmux for
day-to-day management via Crucible. Don't run both simultaneously —
pick one per server. To avoid confusion, don't `systemctl --user start gtnh`
while Crucible is managing the same instance via tmux.

---

## What's Next (Stage 2)

- PyQt6 GUI: sidebar with instance list, tabbed panel per server
- Console tab: live log tail with TPS/player parsing
- Mods tab: enable/disable/add mods with drag-and-drop
- Notes tab: per-instance markdown notes with auto-save
- System tray icon with per-server status dots
- `crucible deploy` subcommand wrapping `gtnh_deploy.py`
