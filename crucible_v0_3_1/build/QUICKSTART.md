# Crucible — Quick Start

Your server is at:
```
/home/roaring/Desktop/Midtech/GT_New_Horizons_2.8.4_Server_Java_17-25/
```

---

## 1. Install

```bash
cd ~/crucible          # wherever you extracted this zip
bash install.sh
```

If `crucible` isn't on PATH after install, add this to `~/.bashrc`:
```bash
export PATH="$HOME/.local/bin:$PATH"
source ~/.bashrc
```

---

## 2. Register your server

```bash
crucible add \
  "/home/roaring/Desktop/Midtech/GT_New_Horizons_2.8.4_Server_Java_17-25" \
  --name "Midtech" \
  --session "gtnh" \
  --version "2.8.4"
```

`--session gtnh` matches whatever name you use when you start tmux manually.
The start script Crucible will use is `startserver-java9.sh` (auto-detected).

---

## 3. Launch

```bash
# Graphical interface
crucible gui

# Or CLI
crucible status
crucible start Midtech
crucible attach Midtech       # opens the console in Konsole
crucible send Midtech forge tps
crucible stop Midtech
```

---

## What Crucible does NOT do

- It does not replace `gtnh_deploy.py` — use that for first-time setup
- It does not touch your world files or config
- Closing Crucible does **not** stop a running server (it lives in tmux)

---

## Your exact tmux session name

When you ran the server manually, the session was `gtnh`.
Crucible matches that with `--session gtnh` above.

If you start fresh without `--session`, Crucible would generate `gtnh-midtech` —
then you'd need to use that name in your manual tmux commands too.
Pick one and be consistent.

---

## Removing Crucible

```bash
pip uninstall crucible
rm ~/.config/crucible/instances.json
rm ~/.local/share/applications/crucible.desktop
```

Server files are never touched.
