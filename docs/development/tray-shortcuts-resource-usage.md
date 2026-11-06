# Tray, server shortcuts, and resource usage

## User behavior

The system tray is optional and defaults off for existing installations. The
setting is **Keep Crucible running in the system tray when its window closes**.
When enabled, closing the main window hides it; it does not close the manager.
The tray's **Open Crucible** action restores the same window and worker state.
**Quit Crucible** exits the manager. Neither action stops a Minecraft server:
managed servers remain in their own tmux sessions.

The tray is not a login autostart feature. Crucible starts only when the owner
opens it or chooses an explicit desktop/application shortcut. Opening Crucible
performs an immediate read-only inventory of registered tmux sessions. The
server rail initially shows **Checking**, followed by the shared confirmed
status. A host-reboot crash report is informational; it never starts a server.
The existing per-instance watchdog option still concerns an unexpected server
crash while Crucible is already running.

## Tray menu

The menu is intentionally short:

- **System usage**: managed usage (Crucible GUI plus JVMs belonging to registered
  servers), whole-system usage, and optional GPU/VRAM metrics.
- **Servers**: only instances registered in Crucible. Each item uses its
  existing server artwork and has a small muted color accent derived from that
  artwork. Its submenu shows status and offers only the lifecycle action valid
  for a positively confirmed state, plus Open in Crucible and Open folder.
- **Open Crucible**
- **Quit Crucible**

Status text is authoritative. Color is supplementary. Running exposes Stop;
Stopped exposes Start; Starting, Stopping, Unknown, Missing, Unmanaged, and
tmux-unavailable states expose no lifecycle action. Stop is graceful only.

The controller is a view of MainWindow state. It does not run a separate tmux
poller and does not own Minecraft processes. The existing five-second health
worker supplies both the main UI and tray. While a native menu is open, menu
rebuilds are deferred so a status refresh cannot close the user's submenu.
Server icons are cached by resolved path and modification time. The existing
10-second crash watchdog stops its timer when no server is registered for
monitoring; it resumes when a server becomes ready again.

## Resource sampling and performance budget

No resource or GPU timer is created. The System usage submenu triggers a
short-lived worker, which:

1. samples the Crucible process and registered server JVMs from /proc;
2. waits 350 ms to calculate CPU deltas;
3. reads whole-system CPU and memory;
4. queries NVIDIA `nvidia-smi`, when installed, with a 1.5-second timeout;
5. exits.

The result is cached for ten seconds. There is no recurring GPU query and no
resident telemetry helper. Managed CPU is normalized to a share of all logical
CPUs; managed memory is resident memory. Whole-system memory is based on
MemAvailable. NVIDIA utilization is the mean across adapters and VRAM is
summed; unsupported GPU providers are reported as **Unavailable**, not zero.
The displayed GPU figure is system/GPU usage, not Minecraft process attribution.

The resource module is deliberately separate from Qt widgets so it can become
a platform service/core module during a future Rust port. Its process discovery
and snapshot model should remain typed and UI-independent.

## Server shortcuts

The server context menu offers **Create shortcuts…**. Start and Stop are
separate launchers, with Desktop, Applications menu, and Both destinations.
The Desktop directory is editable or browsable and is remembered in Crucible
settings. The Applications entry is per-user only.

A launcher contains the stable registry instance ID, not the display name, so
renaming a server does not break it. It invokes `python -m crucible
shortcut-action` with argv (no shell). Start requires an exact confirmed
Stopped state. Stop requires an exact confirmed Running state and is graceful
only. Both revalidate state at execution time rather than trusting a possibly
stale tray label.

A Stop first writes an atomic `stop_requested` heartbeat. This disarms any
already-running Crucible watchdog before the tmux session disappears. After
Stop succeeds, the heartbeat becomes `stopped_clean`; if the stop fails and
tmux positively confirms the server is still running, it returns to
`running`. Unknown stays disarmed instead of risking an automatic restart.
If the host reboots during the stop, startup recovery can still report the
interrupted operation rather than treating it as a clean stop. Tray Stop
uses the existing InstancePanel lifecycle path, including a non-selected
server worker, and never offers Force Kill.

Failures use a desktop notification when available and a visible fallback
otherwise. Removing shortcuts removes only Crucible's exact generated
filenames for that instance; it never removes the server or folder.

Shortcut files are written atomically, owner-writable by the user, and checked
against symlink/non-regular targets. When a generated hash filename already
exists, Crucible also verifies its embedded instance/action identity before
updating or removing it; an unrelated user launcher is never overwritten just
because its filename collides. No system-wide directory or root permission is
required.

## Future Rust port boundary

Keep the following boundaries intact:

- **Core/data**: stable instance IDs, shortcut destination model, resource
  snapshot types, exact status/action eligibility, and desktop-entry generation.
- **Process adapters**: tmux command execution, procfs counters, and optional
  GPU provider probes with finite timeouts.
- **Qt UI adapter**: tray menus, QSettings, dialogs, icons, and user feedback.

The Rust implementation should preserve the same CLI/action semantics and
XDG files so Python and Rust builds can coexist without changing server
registries, shortcut identity, or server folders. No Rust migration is part of
this feature; Python remains the reference implementation until its behavior
is verified on Nobara and in CI.

## Lessons taken from the existing owner projects

These are architecture references, not UI templates or new runtime
dependencies:

- [Crosshair `rust-port`](https://github.com/Roaring1/Crosshair/tree/rust-port)
  separates `crosshair_core` from the CLI and optional Slint Settings binary.
  Its Cargo defaults stay empty and its Settings UI is feature-gated. It also
  deliberately limits its system tray to Windows rather than claiming a
  portable Linux/Wayland tray. Crucible should keep its typed status/resource/
  shortcut model UI-independent and select a tray adapter only after runtime
  availability is confirmed.
- [Hearth](https://github.com/Roaring1/Hearth) enforces a toolkit-free core:
  only its UI package imports GTK. Frozen snapshots, one bounded collector,
  and a separate slower reporter prevent the UI from repeatedly recomputing
  expensive diagnostics. Crucible follows the same principle by deriving the
  tray from MainWindow's existing status result and keeping resource/GPU
  sampling on-demand rather than running another permanent poller.
- [Padfire](https://github.com/Roaring1/Padfire) uses user-owned configuration,
  a local control/status API, and an intentionally small tray menu. Its
  per-user boundary and stable external commands are useful references for
  future Crucible integrations; they are not a reason to add a background
  service or socket to this feature.

The practical Rust-port rule is: keep business rules and lifecycle eligibility
in a small core crate; keep tmux/procfs/GPU and XDG interactions behind
replaceable adapters; let Qt/Python or a future native UI render the same
status model. A tray being unavailable must never change server lifecycle
behavior or disable explicit CLI control.
