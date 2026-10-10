"""Тексты сообщений (HTML)."""
import asyncio
import re
import time

from datetime import datetime

from . import checkhost, config, docker, geoip, manage, report, system, tunnel
from .util import IND, CmdError, esc, fmt_ago, fmt_bytes, fmt_duration


def _bar(pct: float) -> str:
    return "🔴" if pct >= 90 else "🟡" if pct >= 75 else "🟢"


def unit_icon(state: str) -> str:
    return "🟢" if state == "active" else "🟡" if state in ("activating", "reloading") else "🔴"


async def status() -> str:
    m = system.metrics(await system.cpu_now())
    lines = ["<b>📊 Сервер</b>",
             f"⏱ аптайм {fmt_duration(m.uptime)}, load {m.load[0]:.2f} / {m.load[1]:.2f} / {m.load[2]:.2f}",
             f"{_bar(m.cpu_pct)} CPU {m.cpu_pct:.0f}%",
             f"{_bar(100 - m.mem_avail_pct)} RAM доступно {fmt_bytes(m.mem_avail)} из {fmt_bytes(m.mem_total)}"
             + (f", swap {fmt_bytes(m.swap_used)}/{fmt_bytes(m.swap_total)}" if m.swap_total else ""),
             f"{_bar(m.disk_pct)} диск {m.disk_pct:.0f}%, свободно {fmt_bytes(m.disk_free)}",
             f"🌐 IP {esc(', '.join(system.public_ipv4()) or 'нет внешнего IPv4')}"]
    # каждая внешняя команда в своём try: зависшая одна не должна ронять весь раздел
    try:
        states = await manage.unit_states()
        lines.append("🔧 " + " · ".join(f"{unit_icon(states.get(u, '?'))} {u}" for u in manage.SERVICES))
    except CmdError as e:
        lines.append(f"🔴 службы: {esc(e)}")
    jails = await system.jails()
    lines.append("🛡 fail2ban: " + (" · ".join(f"{esc(j.name)} сейчас {len(j.banned_now)}" for j in jails)
                                    if jails else "не отвечает"))
    upd, sec = system.updates()
    if upd:
        lines.append(f"📦 обновлений: {upd}" + (f" (безопасности {sec})" if sec else ""))
    lines.append("\n<b>🐳 Контейнеры</b>")
    try:
        ctrs = await docker.containers()
    except CmdError as e:  # docker завис (например, сервер перегружен) — показываем, что знали до этого
        lines.append(f"🔴 docker не отвечает: {esc(e)}")
        ctrs = []
        if last := docker.cached():
            lines.append(f"<i>по данным {fmt_ago(last[0])}:</i>")
            ctrs = last[1]
    for c in ctrs:
        icon = "🟢" if c.running else ("🔴" if c.watched else "⚪")
        lines.append(f"{icon} {esc(c.name)} — {esc(c.status)}")
    ports, stale = system.listening()
    if ports:
        lines.append("\n<b>🔌 Порты</b>")
        by_proc: dict[str, list[str]] = {}
        for p, proc in sorted(ports.items(), key=lambda kv: int(kv[0].split("/")[1])):
            by_proc.setdefault(proc, []).append(p)
        lines += [f"{esc(proc)}: {', '.join(ps)}" for proc, ps in by_proc.items()]
    lines += [f"🔴 {esc(p)} привязан к {esc(ip)} — этого адреса больше нет на сервере" for p, ip in stale.items()]
    certs = await system.certs()
    if certs:
        lines.append("\n<b>🔒 Сертификаты</b>")
        lines += [f"{'🟢' if c.days_left >= config.CERT_WARN_DAYS else '🔴'} {esc(c.name)}: {c.days_left} дн."
                  for c in certs]
    if system.reboot_required():
        lines.append("\n⚠️ требуется перезагрузка")
    return "\n".join(lines)


is_online = tunnel.is_online


NAME_MAX = 12   # длиннее — обрезаем, чтобы строка влезала в экран телефона
ORG_MAX = 14
IND2 = "\u00a0" * 3


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def _where(g: dict | None) -> str:
    """«🇩🇪 Example ISP» — страна и провайдер, без города (компактно для телефона)."""
    if not g:
        return ""
    if g.get("private"):
        return "локальная сеть"
    return " ".join(x for x in (checkhost.flag(g["cc"]) if g.get("cc") else "", _short(g.get("org", ""), ORG_MAX)) if x)


def peer_line(p: tunnel.Peer, now: float, geo: dict | None = None, emoji: str = "") -> str:
    """Две строки: «🟢 🦊 имя · 10.0.0.2» и «↓4.8 ГБ ↑212 МБ · 🇩🇪 Example ISP»."""
    head = f"{emoji + ' ' if emoji else ''}<b>{esc(_short(p.label, NAME_MAX))}</b> · <code>{esc(p.ip)}</code>"
    if is_online(p, now):
        head = f"🟢 {head}"
    elif p.handshake:
        head = f"⚪ {head} · <i>{fmt_ago(p.handshake, now).removesuffix(' назад')}</i>"
    else:
        head = f"⚪ {head}"
    tail = [f"↓{fmt_bytes(p.tx)} ↑{fmt_bytes(p.rx)}"] if p.rx + p.tx else []
    if p.endpoint_ip and (where := _where(geo)):
        tail.append(esc(where))
    return head + (f"\n{IND2}" + " · ".join(tail) if tail else "")


async def _geo_all(pool, peers: list[tunnel.Peer]) -> dict[str, dict]:
    """Геоданные для внешних IP клиентов (параллельно, ошибки — без геоданных)."""
    loop = asyncio.get_running_loop()
    ips = sorted({p.endpoint_ip for p in peers if p.endpoint_ip})
    res = await asyncio.gather(*(loop.run_in_executor(pool, geoip.lookup, ip) for ip in ips), return_exceptions=True)
    return {ip: r for ip, r in zip(ips, res) if isinstance(r, dict)}


def ssh_target() -> str | None:
    """IP:порт для проверки извне — внешний IPv4 и порт sshd (порт открыт всегда)."""
    ips = system.public_ipv4()
    if not ips:
        return None
    ssh = [p for p, proc in system.listening()[0].items() if proc == "sshd" and p.startswith("tcp/")]
    return f"{ips[0]}:{ssh[0].split('/')[1] if ssh else 22}"


GEO_WAIT = "⏳ Проверяю доступность сервера с ~60 узлов check-host.net по всему миру, это до 40 секунд…"


def risk_card(state: dict, insts: list[tunnel.Instance] | None = None) -> str:
    """Сводка признаков риска блокировки адреса — из состояния бота, без сетевых запросов."""
    now = time.time()
    lines = ["<b>🧭 Риск блокировки</b>"]
    if since := state.get("ip_since"):
        lines.append(f"🌐 адрес не менялся {fmt_duration(now - since)} (с {datetime.fromtimestamp(since, config.TZ):%d.%m})")
    if config.GEO_COUNTRY:
        flag = checkhost.flag(config.GEO_COUNTRY)
        geo = state.get("geo_last")
        hist = [h for h in state.get("geo_hist", []) if now - h[0] < 86400]
        if geo and geo.get("cc") == config.GEO_COUNTRY:
            mark = "❌" if geo["fail"] else ("✅" if geo["ok"] else "⚪")
            line = f"{flag} последняя проверка {fmt_ago(geo['ts'], now)}: {mark} {geo['ok']}/{geo['total']} узлов"
            if geo["fail"]:
                line += f" ({esc(', '.join(geo['failed']))})"
            if hist:
                bad = sum(1 for h in hist if h[2])
                line += f" · за сутки проверок {len(hist)}, неудачных {bad}"
            lines.append(line)
        else:
            lines.append(f"{flag} доступность ещё не проверялась")
    day = state.get("traffic_day") or {}
    if insts and day.get("date"):
        _, rx, tx = report.day_traffic(day, insts, day["date"])
        now_online = sum(is_online(p, now) for i in insts if not i.error for p in i.peers)
        total = sum(len(i.peers) for i in insts if not i.error)
        lines.append(f"📈 трафик за сегодня ↓{fmt_bytes(tx)} ↑{fmt_bytes(rx)} · в сети {now_online}/{total}")
    return "\n".join(lines)


async def geo_view(pool, state: dict | None = None) -> str:
    target = ssh_target()
    if not target:
        return "<b>🌍 Доступность</b>\n🔴 у сервера нет внешнего IPv4"
    card = ""
    if state is not None:
        insts = None
        if tunnel.found():
            try:
                insts = await tunnel.collect()
            except CmdError:
                pass
        card = risk_card(state, insts) + "\n\n"
    started = time.monotonic()
    try:
        info = await asyncio.get_running_loop().run_in_executor(pool, checkhost.node_info)
        res = await checkhost.check(pool, target, sorted(info))
    except checkhost.CheckHostError as e:
        return f"{card}<b>🌍 Доступность</b>\n🔴 check-host.net не ответил: {esc(e)}"
    countries = checkhost.by_country(res, info, first=config.GEO_COUNTRY)
    n_ok = sum(v is True for v in res.values())
    lines = [f"{card}<b>🌍 Доступность {esc(target)}</b>", ""]
    for c in countries:
        if c.cc != config.GEO_COUNTRY:
            continue
        lines.append(f"{checkhost.flag(c.cc)} <b>{esc(c.name)}</b> — {len(c.ok)}/{len(c.ok) + len(c.fail) + len(c.silent)}")
        lines += [f"{IND}✅ {esc(city)}" for city in c.ok]
        lines += [f"{IND}❌ {esc(city)}" for city in c.fail]
        lines += [f"{IND}⚪ {esc(city)} — не ответил" for city in c.silent]
    bad = [c for c in countries if c.cc != config.GEO_COUNTRY and c.fail]
    if bad:
        lines.append("\n🔴 <b>Недоступен</b>")
        lines += [f"{checkhost.flag(c.cc)} {esc(c.name)}: ❌ {esc(', '.join(c.fail))}"
                  + (f" · ✅ {esc(', '.join(c.ok))}" if c.ok else "") for c in bad]
    good = [c for c in countries if c.cc != config.GEO_COUNTRY and c.ok and not c.fail]
    if good:
        lines.append(f"\n🟢 <b>Доступен</b> из {len(good)} стран:")
        lines.append("".join(checkhost.flag(c.cc) for c in good))
    silent = [c for c in countries if c.cc != config.GEO_COUNTRY and not c.ok and not c.fail]
    if silent:
        lines.append(f"\n⚪ Не ответили: {''.join(checkhost.flag(c.cc) for c in silent)}")
    n_silent = sum(v is None for v in res.values())
    lines.append(f"\n<i>TCP-подключение к порту SSH · доступен с {n_ok} из {len(res)} узлов"
                 + (f", {n_silent} не ответили" if n_silent else "") + f" · {time.monotonic() - started:.0f} с</i>")
    return "\n".join(lines)


async def tunnel_view(insts: list[tunnel.Instance] | None = None, pool=None, emojis: dict | None = None) -> str:
    if insts is None:
        try:
            insts = await tunnel.collect()
        except CmdError as e:  # docker завис — честно скажем, а не «не удалось открыть раздел»
            return f"<b>🔐 Клиенты</b>\n🔴 docker не отвечает: {esc(e)}"
    if not insts:
        return ("<b>🔐 Клиенты</b>\nтуннель на сервере не найден: нет docker-контейнера "
                "с подходящей утилитой (см. README, раздел про настройки)")
    now = time.time()
    blocks = []
    for inst in insts:
        peers = inst.peers
        n_online = sum(is_online(p, now) for p in peers)
        head = f"<b>🔐 {esc(inst.container)}</b>"
        if not inst.error:
            head += f" · в сети <b>{n_online}</b>/{len(peers)}"
        lines = [head]
        meta = []
        if inst.version:
            m = re.search(r"v\d[\w.]*", inst.version)
            meta.append(esc(m.group(0) if m else inst.version))
        meta += [p.split("/")[1].upper() + " " + p.split("/")[0] for p in inst.ports]
        if meta:
            lines.append("<i>" + " · ".join(meta) + "</i>")
        if inst.error:
            lines.append(f"\n🔴 не удалось прочитать данные: {esc(inst.error)}")
            blocks.append("\n".join(lines))
            continue
        lines.append("")
        peers = tunnel.sort_by_ip(peers)
        geo = await _geo_all(pool, peers) if pool else {}
        lines += [peer_line(p, now, geo.get(p.endpoint_ip), (emojis or {}).get(p.pubkey, "")) for p in peers]
        total = f"Σ ↓{fmt_bytes(sum(p.tx for p in peers))} ↑{fmt_bytes(sum(p.rx for p in peers))}"
        if inst.started_at:
            total += f" за {fmt_duration(now - inst.started_at)}"
        lines.append(f"\n<i>{total}</i>")
        if not inst.names_source:
            lines.append("<i>список клиентов не найден — вместо имён ключи</i>")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


async def bans() -> tuple[str, list[tuple[str, str]]]:
    """Текст и список (jail, ip) забаненных сейчас."""
    jails = await system.jails()
    if not jails:
        return "<b>🛡 fail2ban</b>\nне отвечает", []
    lines, banned = ["<b>🛡 fail2ban</b>"], []
    for j in jails:
        lines.append(f"<b>{esc(j.name)}</b>: сейчас {len(j.banned_now)}, всего {j.total_banned}, неудач {j.total_failed}")
        lines += [f"  • {esc(ip)}" for ip in j.banned_now[:20]]
        if len(j.banned_now) > 20:
            lines.append(f"  … и ещё {len(j.banned_now) - 20}")
        banned += [(j.name, ip) for ip in j.banned_now[:20]]
    lines.append("<i>счётчики «всего» — с последнего запуска fail2ban</i>")
    return "\n".join(lines), banned
