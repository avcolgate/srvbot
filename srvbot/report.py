"""Недельный отчёт: что произошло за период — трафик клиентов и баны. Приросты считаются от снимка,
сохранённого при прошлом отчёте; текущее состояние сервера — в разделе 📊 Сервер."""
import time
from datetime import datetime

from . import config, system, tunnel
from .util import CmdError, State, esc, fmt_bytes, h, p, table


def delta(cur: int, prev: int | None) -> int:
    """Прирост счётчика; если счётчик сбросился (рестарт) — считаем с нуля."""
    if prev is None:
        return cur
    return cur - prev if cur >= prev else cur


HOUR = 3600
KEEP_HOURS = 48  # почасовые суммы храним за двое суток: хватает и на «24 ч», и на «сегодня»


def account(led: dict | None, insts: list[tunnel.Instance], now: float, complete: bool = False) -> dict:
    """Накопительный учёт трафика клиентов. Счётчики туннеля обнуляются при его перезапуске, поэтому
    прирост между проверками копится в состоянии бота и переживает перезапуски туннеля и бота.
    led = {"since": начало учёта, "total": [rx, tx], "peers": {"контейнер/ключ":
           {"last": [rx, tx] — счётчики на прошлой проверке, "total": [rx, tx], "hours": [[начало часа, rx, tx], …]}}}
    complete — прочитаны все контейнеры: удалённых клиентов можно забыть (общий итог их трафик сохраняет)."""
    led = led or {"since": now, "total": [0, 0], "peers": {}}
    hour = int(now // HOUR * HOUR)
    seen = set()
    for i in insts:
        if i.error:
            continue
        for p in i.peers:
            key = f"{i.container}/{p.pubkey}"
            seen.add(key)
            rec = led["peers"].setdefault(key, {"last": None, "total": [0, 0], "hours": []})
            last = rec["last"] or [None, None]
            drx, dtx = delta(p.rx, last[0]), delta(p.tx, last[1])
            rec["last"] = [p.rx, p.tx]
            if not drx + dtx:
                continue
            rec["total"] = [rec["total"][0] + drx, rec["total"][1] + dtx]
            led["total"] = [led["total"][0] + drx, led["total"][1] + dtx]
            if rec["hours"] and rec["hours"][-1][0] == hour:
                rec["hours"][-1][1] += drx
                rec["hours"][-1][2] += dtx
            else:
                rec["hours"].append([hour, drx, dtx])
            rec["hours"] = [x for x in rec["hours"] if x[0] > hour - KEEP_HOURS * HOUR]
    if complete:
        led["peers"] = {k: v for k, v in led["peers"].items() if k in seen}
    return led


def since(rec: dict | None, ts: float) -> tuple[int, int]:
    """Трафик клиента (rx, tx) за часы, начавшиеся не раньше ts."""
    hours = [x for x in (rec or {}).get("hours", []) if x[0] >= ts]
    return sum(x[1] for x in hours), sum(x[2] for x in hours)


def day_start(now: float) -> float:
    """Начало окна «последние 24 часа»: текущий час и 23 предыдущих."""
    return now // HOUR * HOUR - 23 * HOUR


def last_day(rec: dict | None, now: float) -> tuple[int, int]:
    return since(rec, day_start(now))


def total_since(led: dict | None, ts: float, prefix: str = "") -> tuple[int, int]:
    """Сумма по всем клиентам (или по контейнеру — prefix «контейнер/»)."""
    parts = [since(r, ts) for k, r in (led or {}).get("peers", {}).items() if k.startswith(prefix)]
    return sum(x[0] for x in parts), sum(x[1] for x in parts)


def snapshot(led: dict | None, jails: list[system.Jail]) -> dict:
    return {
        "ts": time.time(),
        "v": 2,  # tunnel — накопленные итоги из учёта трафика (в v1 были сырые счётчики туннеля)
        "tunnel": {k: r["total"] for k, r in (led or {}).get("peers", {}).items()},
        "f2b": {j.name: j.total_banned for j in jails},
    }


async def build(state: State) -> tuple[str, dict]:
    """Текст отчёта и новый снимок счётчиков. Снимок сохраняет вызывающий — после успешной отправки."""
    prev = state.get("snap", {}) or {}
    since = prev.get("ts")
    now = datetime.now(config.TZ)
    period = f" (с {datetime.fromtimestamp(since, config.TZ):%d.%m %H:%M})" if since else ""
    lines = [h(f"📋 Отчёт {now:%d.%m.%Y}") + (p(f"<i>{period.strip(' ()')}</i>") if period else "")]
    try:
        insts = await tunnel.collect()
    except CmdError as e:  # docker не ответил — отчёт всё равно соберём, без раздела клиентов
        insts = []
        lines.append(f"🔴 docker: {esc(e)}")
    jails = await system.jails()

    f2b = [f"{esc(j.name)} +{d}" for j in jails if (d := delta(j.total_banned, prev.get("f2b", {}).get(j.name)))]
    if f2b:
        lines.append(p("🛡 баны fail2ban: " + ", ".join(f2b)))

    if insts:
        lines.append(h("🔐 Клиенты — трафик за период", 4))
    rows = []
    led = state.data.get("traffic") or {}
    prev_tun = prev.get("tunnel", {}) if prev.get("v") == 2 else {}
    for inst in insts:
        if inst.error:
            lines.append(p(f"🔴 {esc(inst.container)}: {esc(inst.error)}"))
        for peer in inst.peers:
            key = f"{inst.container}/{peer.pubkey}"
            cur = (led.get("peers", {}).get(key) or {}).get("total", [0, 0])
            pr = prev_tun.get(key, [None, None])
            drx, dtx = delta(cur[0], pr[0]), delta(cur[1], pr[1])
            if drx + dtx:
                em = (state.data.get("tunnel_emoji") or {}).get(peer.pubkey, "•")
                rows.append((drx + dtx, [f"{em} <b>{esc(peer.label)}</b>", f"↓ {fmt_bytes(dtx)}", f"↑ {fmt_bytes(drx)}"]))
    if rows:
        rows.sort(key=lambda r: -r[0])
        lines.append(table([r for _, r in rows]))
        lines.append(p(f"<i>Σ {fmt_bytes(sum(r[0] for r in rows))}</i>"))
    elif insts:
        lines.append(p("трафика не было"))

    return "".join(lines), snapshot(led, jails)

