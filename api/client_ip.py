"""
api/client_ip.py

The client address used for rate limits. Request headers are attacker-controlled, so a header is believed only when the TCP peer is a
proxy we trust:
  * CF-Connecting-IP (set by Cloudflare) is used only when the peer address is inside Cloudflare's published ranges;
  * TRUSTED_PROXY_CIDRS (comma separated, e.g. "127.0.0.1/32") adds proxies of our own that also set CF-Connecting-IP.
Otherwise the socket address (request.client.host) is used. X-Forwarded-For is never read here; uvicorn already replaces the peer
address from it for the proxies listed in its --forwarded-allow-ips (default 127.0.0.1 only).
"""

import ipaddress
import os

# https://www.cloudflare.com/ips/ - review them from time to time
CLOUDFLARE_RANGES = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)
_CLOUDFLARE = [ipaddress.ip_network(n) for n in CLOUDFLARE_RANGES]


def _trusted_networks() -> list:
    extra = []
    for part in (os.environ.get("TRUSTED_PROXY_CIDRS") or "").split(","):
        if part.strip():
            extra.append(ipaddress.ip_network(part.strip(), strict=False))   # a bad value raises ValueError
    return _CLOUDFLARE + extra


def _parse(value: str | None):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


def client_ip(request) -> str:
    peer = request.client.host if request.client else None
    peer_ip = _parse(peer)
    if peer_ip is not None and any(peer_ip in net for net in _trusted_networks()):
        forwarded = _parse(request.headers.get("cf-connecting-ip"))
        if forwarded is not None:
            return str(forwarded)
    return peer or "unknown"
