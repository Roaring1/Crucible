"""
crucible/process/netfix.py

Workarounds for environment networking quirks that stop a Minecraft server
from binding its port.

The big one: on machines where IPv6 is disabled or unavailable, modern
Minecraft/Netty tries to open an IPv6 wildcard socket and bind() fails with:

    io.netty.channel.unix.Errors$NativeIoException:
        bind(..) failed with error(-97): Address family not supported by protocol
    **** FAILED TO BIND TO PORT!

Error -97 is EAFNOSUPPORT ("Address family not supported by protocol"). It is
NOT a port-in-use problem, so changing the port does nothing. Forcing the JVM
onto the IPv4 stack with -Djava.net.preferIPv4Stack=true makes the wildcard
bind resolve to 0.0.0.0 and fixes it.

These helpers are pure string/file utilities so they can be unit-tested without
starting a real server.
"""

from __future__ import annotations

from pathlib import Path

# The flag that fixes EAFNOSUPPORT bind(-97). preferIPv6Addresses=false keeps
# name resolution from handing back AAAA records first on dual-stack hosts.
IPV4_FLAG = "-Djava.net.preferIPv4Stack=true"
IPV6_FLAG = "-Djava.net.preferIPv6Addresses=false"

_IPV4_KEY = "-Djava.net.preferIPv4Stack"
_IPV6_KEY = "-Djava.net.preferIPv6Addresses"


def has_ipv4_flag(java_args: str) -> bool:
    """True if the args already pin the IPv4 stack (in any value form)."""
    return any(tok.startswith(_IPV4_KEY) for tok in (java_args or "").split())


def ensure_ipv4(java_args: str) -> str:
    """Return java_args with the IPv4-stack flags guaranteed present.

    Existing flags (and their values) are left untouched so a user who really
    wants IPv6 can override it by setting the flag themselves.
    """
    tokens = (java_args or "").split()
    have_v4 = any(t.startswith(_IPV4_KEY) for t in tokens)
    have_v6 = any(t.startswith(_IPV6_KEY) for t in tokens)
    if not have_v4:
        tokens.append(IPV4_FLAG)
    if not have_v6:
        tokens.append(IPV6_FLAG)
    return " ".join(tokens).strip()


def ensure_user_jvm_args_file(server_dir: str | Path) -> bool:
    """Make sure a Forge/NeoForge user_jvm_args.txt pins the IPv4 stack.

    Modern Forge/NeoForge run scripts read JVM flags from user_jvm_args.txt and
    ignore environment-provided args, so the launch-time injection alone would
    miss them. If that file exists and lacks the flag, append it.

    Returns True if the file was modified, False otherwise. Never raises for
    routine IO problems caught by the caller; only unexpected errors propagate.
    """
    path = Path(server_dir) / "user_jvm_args.txt"
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if _IPV4_KEY in text:
        return False
    sep = "" if text.endswith("\n") or text == "" else "\n"
    addition = (
        f"{sep}# Added by Crucible: force IPv4 so the server can bind its port\n"
        f"# (fixes Netty bind error -97 on IPv6-disabled machines)\n"
        f"{IPV4_FLAG}\n{IPV6_FLAG}\n"
    )
    try:
        path.write_text(text + addition, encoding="utf-8")
    except OSError:
        return False
    return True
