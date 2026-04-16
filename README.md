# Crucible — GTNH Server Manager

A clean GUI + CLI for running GT: New Horizons dedicated servers on Nobara Linux (Fedora-based).

---

## Install

### Option A — One-liner (recommended, requires internet)
Open a terminal and paste:
```bash
bash <(curl -sL https://raw.githubusercontent.com/Roaring1/Crucible/main/get-crucible.sh)
```
That's it. Downloads, installs, adds to your app launcher. Nothing lands on your Desktop.

---

### Option B — Download the zip yourself
1. Download `crucible_v0_3_2.zip` from the [Releases](https://github.com/Roaring1/Crucible/releases) page
2. Open a terminal and run:
   ```bash
   cd ~/Downloads
   unzip crucible_v0_3_2.zip
   bash crucible_v0_3_2/install.sh
   ```
3. The script asks if you want to delete the extracted folder when it's done — say yes.

---

### Where things end up
| Thing | Location | Touch it? |
|---|---|---|
| Crucible app | `~/.local/share/crucible/` | No |
| Launch command | `~/.local/bin/crucible` | No |
| App launcher entry | `~/.local/share/applications/crucible.desktop` | No |
| Server registry | `~/.config/crucible/instances.json` | No (edit via GUI) |
| Your GTNH server | Wherever you put it | Yes, that's yours |

---

## First Launch

Search **Crucible** in your KDE app launcher, or run:
```bash
crucible gui
```

Click **+ Add Server** and browse to your GTNH server folder (e.g. `~/Servers/Midtech/GT_New_Horizons_2.8.4_Server_Java_17-25`).

That's all the setup there is.

---

## CLI Quick Reference

```bash
crucible list                      # see all registered servers
crucible start Midtech             # start a server
crucible stop  Midtech             # stop gracefully
crucible attach Midtech            # open the live console
crucible send Midtech forge tps    # send a command
crucible status                    # running / stopped at a glance
```

---

## Uninstall

```bash
pip uninstall crucible
rm -rf ~/.config/crucible ~/.local/share/crucible
rm ~/.local/share/applications/crucible.desktop
```

Your server files are **never** touched by Crucible.

---

## Requirements

- Nobara 41–43 (or any Fedora-based distro with dnf)
- Python 3.11+
- tmux (`sudo dnf install tmux`)
- PyQt6 (installed automatically)
