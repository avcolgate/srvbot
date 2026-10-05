"""Недельный отчёт. Приросты считаются от снимка, сохранённого при прошлом отчёте."""
import time
from datetime import datetime

from . import config, docker, system, tunnel
from .util import CmdError, State, esc, fmt_bytes


def delta(cur: int, prev: int | None) -> int:
    """Прирост счётчика; если счётчик сбросился (рестарт) — считаем с нуля."""
    if prev is None:
        return cur
    return cur - prev if cur >= prev else cur


def snapshot(insts: list[tunnel.Instance], jails: list[system.Jail]) -> dict:
    return {
        "ts": time.time(),
        "tunnel": {f"{i.container}/{p.pubkey}": [p.rx, p.tx] for i in insts for p in i.peers},
        "f2b": {j.name: j.total_banned for j in jails},
    }


async def build(state: State) -> tuple[str, dict]:
    """Текст отчёта и новый снимок счётчиков. Снимок сохраняет вызывающий — после успешной отправки."""
    prev = state.get("snap", {}) or {}
    since = prev.get("ts")
    now = datetime.now(config.TZ)
    period = f" (с {datetime.fromtimestamp(since, config.TZ):%d.%m %H:%M})" if since else ""
    lines = [f"<b>📋 Отчёт {now:%d.%m.%Y}</b>{period}"]

    m = system.metrics()
    lines.append(f"💾 диск {m.disk_pct:.0f}%, свободно {fmt_bytes(m.disk_free)}; "
                 f"RAM доступно {fmt_bytes(m.mem_avail)}")
    upd, sec = system.updates()
    if upd:
        lines.append(f"📦 обновлений: {upd}" + (f", из них безопасности {sec}" if sec else ""))
    if system.reboot_required():
        lines.append("⚠️ требуется перезагрузка")
    try:
        bad = [c for c in await docker.containers() if c.watched and not c.running]
        lines += [f"🔴 {esc(c.name)}: {esc(c.status)}" for c in bad]
    except CmdError as e:
        lines.append(f"🔴 docker: {esc(e)}")
    for c in await system.certs():
        if c.days_left < config.CERT_WARN_DAYS * 2:
            lines.append(f"🔒 сертификат {esc(c.name)} истекает через {c.days_left} дн.")

    try:
        insts = await tunnel.collect()
    except CmdError as e:  # docker не ответил — отчёт всё равно соберём, без раздела клиентов
        insts = []
        lines.append(f"🔴 docker: {esc(e)}")
    jails = await system.jails()

    f2b = [f"{esc(j.name)} +{d}" for j in jails if (d := delta(j.total_banned, prev.get("f2b", {}).get(j.name)))]
    if f2b:
        lines.append("🛡 баны fail2ban: " + ", ".join(f2b))

    if insts:
        lines.append("\n<b>🔐 Клиенты</b> — трафик за период")
    rows = []
    for inst in insts:
        if inst.error:
            lines.append(f"🔴 {esc(inst.container)}: {esc(inst.error)}")
        for p in inst.peers:
            pr = prev.get("tunnel", {}).get(f"{inst.container}/{p.pubkey}", [None, None])
            drx, dtx = delta(p.rx, pr[0]), delta(p.tx, pr[1])
            if drx + dtx:
                em = (state.data.get("tunnel_emoji") or {}).get(p.pubkey, "•")
                rows.append((drx + dtx, f"{em} <b>{esc(p.label)}</b> — ↓ {fmt_bytes(dtx)} · ↑ {fmt_bytes(drx)}"))
    if rows:
        rows.sort(key=lambda r: -r[0])
        lines += [r for _, r in rows]
        lines.append(f"<i>Σ {fmt_bytes(sum(r[0] for r in rows))}</i>")
    elif insts:
        lines.append("трафика не было")

    return "\n".join(lines), snapshot(insts, jails)

