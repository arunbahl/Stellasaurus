"""Resolve which addresses the dashboard should bind to.

Goal: reachable from **localhost and the tailnet only** — never the broader LAN.
We achieve that by binding explicit addresses (loopback + this host's Tailscale
IP) rather than ``0.0.0.0``. A LAN peer cannot route to a ``100.64.0.0/10``
tailnet address, so binding it does not expose the dashboard to the LAN.

Expose modes:
  * ``tailnet``   -> ["127.0.0.1", <tailscale ipv4 if present>]   (default)
  * ``localhost`` -> ["127.0.0.1"]
  * ``all``       -> ["0.0.0.0"]   (explicit opt-in to LAN exposure)
"""

from __future__ import annotations

import ipaddress
import subprocess

from stellasaurus.common.logging import get_logger

_log = get_logger("control.net")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale uses this range


def tailscale_ipv4() -> str | None:
    """Best-effort discovery of this host's Tailscale IPv4 address.

    Tries the ``tailscale`` CLI first, then scans local interfaces for an address
    in the tailnet (CGNAT) range. Returns ``None`` if Tailscale is not up.
    """
    ip = _from_cli()
    if ip:
        return ip
    return _from_interfaces()


def _from_cli() -> str | None:
    try:
        out = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        candidate = line.strip()
        if _is_tailnet(candidate):
            return candidate
    return None


def _from_interfaces() -> str | None:
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=3
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        # format: "<idx>: <iface> inet <addr>/<prefix> ..."
        for i, token in enumerate(parts):
            if token == "inet" and i + 1 < len(parts):
                addr = parts[i + 1].split("/", 1)[0]
                if _is_tailnet(addr):
                    return addr
    return None


def _is_tailnet(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr) in _CGNAT
    except ValueError:
        return False


def resolve_bind_hosts(expose: str, host_override: str | None) -> list[str]:
    """Return the list of addresses to bind, per the expose mode.

    A single ``host_override`` always wins when set (escape hatch).
    """
    if host_override:
        return [host_override]
    mode = expose.lower()
    if mode == "all":
        return ["0.0.0.0"]
    if mode == "localhost":
        return ["127.0.0.1"]
    # default: tailnet -> loopback + tailscale ip (if available)
    hosts = ["127.0.0.1"]
    ts_ip = tailscale_ipv4()
    if ts_ip:
        hosts.append(ts_ip)
    else:
        _log.warning("tailscale_ip_not_found", note="binding loopback only; is tailscale up?")
    return hosts
