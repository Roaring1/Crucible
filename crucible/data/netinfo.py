"""
crucible/data/netinfo.py

Network address helpers for the "copy connection address" feature.

Gives the user every address a friend might need, computed without guesswork:

  * loopback  127.0.0.1:<port>    - same PC (this machine only)
  * lan       192.168.x.x:<port>  - another device on the home network
  * public    <wan-ip>:<port>     - over the internet (needs port-forwarding)

loopback and LAN are derived locally and instantly (no network calls). The
public address needs one short HTTPS lookup and is fetched separately so the UI
can show the instant answers immediately. Everything is wrapped so a locked
down / offline machine never raises.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass


@dataclass
class Address:
    key: str          # "loopback" | "lan" | "public"
    label: str        # short human label
    host: str         # bare IP/host, may be "" if unknown
    description: str   # one-line explanation for a tooltip/menu

    def with_port(self, port) -> str:
        return f"{self.host}:{port}" if self.host else ""


def loopback_host() -> str:
    return "127.0.0.1"


def lan_host() -> str:
    """Best-effort primary LAN IPv4 for this machine.

    Uses the standard "connect a UDP socket and read back the local address"
    trick. No packets are actually sent, and it works offline because UDP
    connect() only sets the socket's default peer. Falls back to hostname
    resolution, then "".
    """
    for target in (("8.8.8.8", 80), ("192.168.1.1", 80), ("10.255.255.255", 1)):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(target)
                ip = s.getsockname()[0]
            finally:
                s.close()
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return ""


def is_private(ip: str) -> bool:
    """True if ip is in a private/LAN range (RFC1918) or loopback."""
    parts = (ip or "").split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if a == 10 or a == 127:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 192 and b == 168:
        return True
    return False


def public_host(timeout: float = 5.0) -> str:
    """Fetch the WAN IP via keyless services. Returns "" when offline."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip",
                "https://icanhazip.com"):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = r.read(129)
                if len(data) > 128:
                    continue
                ip = data.decode("ascii", "strict").strip()
            if ip and ip.count(".") == 3 and all(
                p.isdigit() and 0 <= int(p) <= 255 for p in ip.split(".")
            ):
                return ip
        except Exception:
            continue
    return ""


def local_addresses() -> list[Address]:
    """Instant (no-network) addresses: loopback + LAN."""
    out = [
        Address("loopback", "This PC", loopback_host(),
                "Only works on the computer running the server."),
    ]
    lan = lan_host()
    out.append(Address(
        "lan", "Same Wi-Fi / LAN", lan,
        "For phones/PCs on the same home network. No port-forwarding needed."
        if lan else "Could not detect a LAN address.",
    ))
    return out
