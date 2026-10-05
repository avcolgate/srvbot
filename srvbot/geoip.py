"""Страна, город и провайдер по IP через ipinfo.io (без токена; кэш на сутки).

Вызывается при открытии списка клиентов туннеля (их внешние IP). Синхронно — вызывать через executor.
"""
import http.client
import ipaddress
import json
import re
import time

from .tg import _connect_ipv4, _ssl

_cache: dict[str, tuple[float, dict]] = {}
LEGAL = r"LLC|Ltd\.?|Inc\.?|GmbH"  # формы собственности, которые убираем из названий провайдеров


def lookup(ip: str) -> dict:
    """{"cc": "fr", "city": "Paris", "org": "Example ISP"}; для частных адресов — {"private": True}."""
    if ipaddress.ip_address(ip).is_private:
        return {"private": True}
    hit = _cache.get(ip)
    if hit and time.time() - hit[0] < 86400:
        return hit[1]
    conn = http.client.HTTPSConnection("ipinfo.io", timeout=10, context=_ssl)
    conn._create_connection = _connect_ipv4
    try:
        conn.request("GET", f"/{ip}/json", headers={"Accept": "application/json", "User-Agent": "srvbot"})
        resp = conn.getresponse()
        data = json.loads(resp.read())
    finally:
        conn.close()
    org = data.get("org") or ""
    if org.startswith("AS") and " " in org:  # «AS64500 Example ISP» -> «Example ISP»
        org = org.split(" ", 1)[1]
    org = re.sub(rf"^(?:{LEGAL})\s+|,?\s+(?:{LEGAL})$", "", org)  # «Example ISP LLC» -> «Example ISP»
    info = {"cc": (data.get("country") or "").lower(), "city": data.get("city") or "", "org": org}
    _cache[ip] = (time.time(), info)
    return info
