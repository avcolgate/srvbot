"""Доступность сервера из разных стран через check-host.net.

Узлы check-host.net (~60 по миру) пробуют подключиться к TCP-порту сервера и пропинговать его:
видно, из каких стран до сервера не доходят подключения.
Сетевые функции синхронные (вызывать через executor), check() — асинхронная обёртка.
"""
import asyncio
import http.client
import json
import time
from dataclasses import dataclass, field

from .tg import _connect_ipv4, _ssl

HOST = "check-host.net"
CONTROL_COUNTRIES = ("de", "nl", "fi")


class CheckHostError(Exception):
    pass


def _get(path: str, timeout: float = 20):
    conn = http.client.HTTPSConnection(HOST, timeout=timeout, context=_ssl)
    conn._create_connection = _connect_ipv4
    try:
        conn.request("GET", path, headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise CheckHostError(f"{path.split('?')[0]}: HTTP {resp.status}")
        return json.loads(body)
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise CheckHostError(f"{type(e).__name__}: {e}") from None
    finally:
        conn.close()


_nodes_cache: tuple[float, dict] = (0, {})


def node_info() -> dict[str, tuple[str, str, str]]:
    """{узел: (код страны, страна, город)}, кэш на сутки."""
    global _nodes_cache
    if time.time() - _nodes_cache[0] > 86400 or not _nodes_cache[1]:
        data = _get("/nodes/hosts")["nodes"]
        info = {k: tuple((v.get("location") or ["", "", ""])[:3]) for k, v in data.items()}
        _nodes_cache = (time.time(), {k: (cc.lower(), country, city) for k, (cc, country, city) in info.items()})
    return _nodes_cache[1]


def nodes(country: str) -> tuple[list[str], list[str]]:
    """(узлы в стране country, пара контрольных узлов в других странах)."""
    info = node_info()
    target = sorted(k for k, v in info.items() if v[0] == country)
    ctl = sorted(k for k, v in info.items() if v[0] in CONTROL_COUNTRIES and v[0] != country)[:2]
    return target, ctl


async def check(pool, target: str, node_list: list[str], tries: int = 5, kind: str = "tcp") -> dict[str, bool | None]:
    """Проверка target с указанных узлов (kind: tcp — подключение к host:port, ping — к host);
    ждёт, пока ответят все (до ~8 с × tries)."""
    loop = asyncio.get_running_loop()
    rid = await loop.run_in_executor(pool, start, kind, target, node_list)
    res: dict = {}
    for _ in range(tries):
        await asyncio.sleep(8)
        res = await loop.run_in_executor(pool, result, rid, kind)
        if res and all(v is not None for v in res.values()):
            break
    return res


def flag(cc: str) -> str:
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in cc.lower()) if len(cc) == 2 and cc.isalpha() else "🏳"


@dataclass
class Country:
    cc: str
    name: str
    ok: list[str] = field(default_factory=list)       # города, откуда подключились
    fail: list[str] = field(default_factory=list)     # города, откуда не подключились
    silent: list[str] = field(default_factory=list)   # не ответили


def by_country(res: dict[str, bool | None], info: dict[str, tuple[str, str, str]],
               first: str = "") -> list[Country]:
    """Результаты по странам: страна first первой, затем страны с проблемами, потом остальные по алфавиту."""
    countries: dict[str, Country] = {}
    for node, ok in res.items():
        cc, name, city = info.get(node, ("??", node, node))
        c = countries.setdefault(cc, Country(cc, name))
        (c.ok if ok is True else c.fail if ok is False else c.silent).append(city)
    return sorted(countries.values(), key=lambda c: (c.cc != first, not c.fail, c.name))


def start(kind: str, target: str, node_list: list[str]) -> str:
    q = "&".join(f"node={n}" for n in node_list)
    r = _get(f"/check-{kind}?host={target}&{q}")
    if not r.get("ok") or "request_id" not in r:
        raise CheckHostError(f"check-{kind}: {str(r)[:200]}")
    return r["request_id"]


def parse_tcp(res: dict) -> dict[str, bool | None]:
    """node -> True (подключился) / False (ошибка, таймаут) / None (ответа ещё нет)."""
    out: dict[str, bool | None] = {}
    for node, v in (res or {}).items():
        if v is None or (isinstance(v, list) and (not v or v[0] is None)):
            out[node] = None
        elif isinstance(v, list) and isinstance(v[0], dict):
            out[node] = "error" not in v[0]
        else:
            out[node] = False
    return out


def parse_ping(res: dict) -> dict[str, bool | None]:
    """node -> True (хоть один ответ на ping) / False (все попытки неудачны) / None (ответа ещё нет).
    Ответ узла — список попыток [["OK", 0.05, "ip"], ["TIMEOUT", 3.0], …]; пока узел не ответил — null."""
    out: dict[str, bool | None] = {}
    for node, v in (res or {}).items():
        tries = v[0] if isinstance(v, list) and v else v
        if tries is None or (isinstance(tries, list) and not tries):
            out[node] = None
        elif isinstance(tries, list) and all(isinstance(t, list) for t in tries):
            out[node] = any(t and t[0] == "OK" for t in tries)
        else:  # {"message": …} или другой неожиданный ответ — считаем неудачей
            out[node] = False
    return out


def result(request_id: str, kind: str = "tcp") -> dict[str, bool | None]:
    raw = _get(f"/check-result/{request_id}")
    return parse_ping(raw) if kind == "ping" else parse_tcp(raw)


@dataclass
class Verdict:
    ok: int                  # узлы целевой страны, которые подключились
    fail: int                # … и которые не смогли
    total: int
    ctl_ok: int              # контрольные узлы в других странах
    ctl_total: int
    failed: list[str] = field(default_factory=list)  # города целевой страны, откуда сервер недоступен

    @property
    def down(self) -> bool:
        """Хотя бы один узел целевой страны не подключился, а контрольные — подключились."""
        return self.ctl_ok > 0 and self.fail >= 1

    @property
    def reachable(self) -> bool:
        """Все ответившие узлы целевой страны подключились."""
        return self.ok > 0 and self.fail == 0

    @property
    def partial(self) -> bool:
        """Недоступен лишь с части узлов страны — похоже на блокировку у отдельных провайдеров."""
        return 0 < self.fail < self.total

    def to_dict(self) -> dict:
        return {"ok": self.ok, "fail": self.fail, "total": self.total,
                "ctl_ok": self.ctl_ok, "ctl_total": self.ctl_total, "failed": self.failed}


def combine(tcp: dict[str, bool | None], ping: dict[str, bool | None] | None) -> dict[str, bool | None]:
    """Итог по узлу из двух проверок: доступен, если прошла хоть одна; недоступен, если TCP не прошёл,
    а ping не помог; нет данных, если TCP не ответил."""
    out: dict[str, bool | None] = {}
    for node in set(tcp) | set(ping or {}):
        t, p = tcp.get(node), (ping or {}).get(node)
        out[node] = True if t is True or p is True else (False if t is False else None)
    return out


def verdict(res: dict[str, bool | None], target: list[str], ctl: list[str],
            info: dict[str, tuple[str, str, str]] | None = None) -> Verdict:
    failed = [n for n in target if res.get(n) is False]
    return Verdict(
        ok=sum(res.get(n) is True for n in target), fail=len(failed), total=len(target),
        ctl_ok=sum(res.get(n) is True for n in ctl), ctl_total=len(ctl),
        failed=[info[n][2] if info and n in info and info[n][2] else n.split(".")[0] for n in failed],
    )
