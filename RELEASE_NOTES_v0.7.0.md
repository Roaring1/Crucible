# Crucible v0.7.0 — stable, responsive, and publish-ready

This is the final hardening release for the local Linux Minecraft server manager.

## Highlights
- Responsive bounded log tailing and batched console rendering.
- Persistent health/log workers plus safe deferred cleanup for every short-lived Qt worker.
- Reliable Stopped detection when no tmux server exists, keeping Start available.
- Accessible CPU and memory progress bars alongside numeric resource values.
- Whole-host power-loss recovery and optional automatic restart.
- World restores reject traversal paths, links, special files, duplicate destinations, wrong roots, truncated entries, and insufficient disk space before touching the live world.
- Correct staged installer, GitHub Release downloader, package version metadata, and checksum asset workflow.

Crucible intentionally remains a focused local server creator/manager. It is not a cloud control plane, router configurator, or Prism Launcher replacement.
