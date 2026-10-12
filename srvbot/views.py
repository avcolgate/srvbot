"""Тексты сообщений (HTML)."""
import asyncio
import re
import time

from datetime import datetime

from . import checkhost, config, docker, geoip, manage, report, system, tunnel
from .util import CmdError, btn, details, esc, fmt_ago, fmt_bytes, fmt_duration, h, mark, p, table, tg_time


def _bar(pct: float) -> str:
    return "🔴" if pct >= 90 else "🟡" if pct >= 75 else "🟢"



def unit_icon(state: str) -> str:
    return "🟢" if state == "active" else "🟡" if state in ("activating", "reloading") else "🔴"


async def status() -> str:
    m = system.metrics(await system.cpu_now())
    blocks = [h("📊 Сервер"),
              p(f"⏱ аптайм {fmt_duration(m.uptime)}, load {m.load[0]:.2f} / {m.load[1]:.2f} / {m.load[2]:.2f}",
                f"🌐 IP {esc(', '.join(system.public_ipv4()) or 'нет внешнего IPv4')}"),
              table([[f"{_bar(m.cpu_pct)} CPU", f"{m.cpu_pct:.0f}%"],
                     [f"{_bar(100 - m.mem_avail_pct)} RAM", f"доступно {fmt_bytes(m.mem_avail)} из {fmt_bytes(m.mem_total)}"
                      + (f", swap {fmt_bytes(m.swap_used)}/{fmt_bytes(m.swap_total)}" if m.swap_total else "")],
                     [f"{_bar(m.disk_pct)} диск", f"{m.disk_pct:.0f}%, свободно {fmt_bytes(m.disk_free)}"]])]
    # службы и контейнеры — с кнопками перезапуска и журнала прямо в строке;
    # каждая внешняя команда в своём try: зависшая одна не должна ронять весь раздел
    svc = []
    try:
        states = await manage.unit_states()
        for u in manage.SERVICES:
            st = states.get(u, "?")
            line = f"{unit_icon(st)} {u}"
            if u in manage.RESTARTABLE:
                line += f" {btn('🔄', f'go:unit:{u}')}"
            svc.append(mark(line, st not in ("active", "activating", "reloading")) + f" {btn('📜', f'lg:u:{u}')}")
    except CmdError as e:
        svc.append(f"🔴 службы: {esc(e)}")
    jails = await system.jails()
    svc.append("🛡 fail2ban: " + (" · ".join(f"{esc(j.name)} сейчас {len(j.banned_now)}" for j in jails)
                                  if jails else "не отвечает"))
    upd, sec = system.updates()
    if upd:
        svc.append(f"📦 обновлений: {upd}" + (f" (безопасности {sec})" if sec else ""))
    blocks += [h("🔧 Службы", 4), p(*svc)]
    ctr_lines = []
    try:
        ctrs = await docker.containers()
    except CmdError as e:  # docker завис (например, сервер перегружен) — показываем, что знали до этого
        ctr_lines.append(f"🔴 docker не отвечает: {esc(e)}")
        ctrs = []
        if last := docker.cached():
            ctr_lines.append(f"<i>по данным {tg_time(last[0])}:</i>")
            ctrs = last[1]
    for c in ctrs:
        line = f"{'🟢' if c.running else ('🔴' if c.watched else '⚪')} {esc(c.name)} — {esc(c.status)}"
        ctr_lines.append(mark(line, c.watched and not c.running)
                         + (f" {btn('🔄', f'go:restart:{c.name}')}" if c.watched else "")
                         + f" {btn('📜', f'lg:c:{c.name}')}")
    blocks += [h("🐳 Контейнеры", 4), p(*(ctr_lines or ["нет контейнеров"]))]
    ports, stale = system.listening()
    if ports:
        by_proc: dict[str, list[str]] = {}
        for port, proc in sorted(ports.items(), key=lambda kv: int(kv[0].split("/")[1])):
            by_proc.setdefault(proc, []).append(port)
        blocks.append(details("🔌 Порты", p(*[f"{esc(proc)}: {', '.join(ps)}" for proc, ps in by_proc.items()])))
    if stale:
        blocks.append(p(*[mark(f"🔴 {esc(port)} привязан к {esc(ip)} — этого адреса больше нет на сервере")
                          for port, ip in stale.items()]))
    certs = await system.certs()
    if certs:
        blocks.append(details("🔒 Сертификаты", p(*[
            mark(f"{'🟢' if c.days_left >= config.CERT_WARN_DAYS else '🔴'} {esc(c.name)}: {c.days_left} дн.",
                 c.days_left < config.CERT_WARN_DAYS) for c in certs])))
    if system.reboot_required():
        blocks.append(p(mark("⚠️ требуется перезагрузка")))
    return "".join(blocks)


is_online = tunnel.is_online


NAME_MAX = 12   # длиннее — обрезаем, чтобы строка влезала в экран телефона
ORG_MAX = 14


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def _where(g: dict | None) -> str:
    """«🇩🇪 Example ISP» — страна и провайдер, без города (компактно для телефона)."""
    if not g:
        return ""
    if g.get("private"):
        return "локальная сеть"
    return " ".join(x for x in (checkhost.flag(g["cc"]) if g.get("cc") else "", _short(g.get("org", ""), ORG_MAX)) if x)


def peer_cells(p: tunnel.Peer, now: float, geo: dict | None = None, emoji: str = "") -> list[str]:
    """Строка таблицы клиентов: кто · трафик · откуда и когда."""
    icon = "🟢" if is_online(p, now) else "⚪"
    who = f"{icon} {emoji + ' ' if emoji else ''}<b>{esc(_short(p.label, NAME_MAX))}</b><br/><code>{esc(p.ip)}</code>"
    traffic = f"↓{fmt_bytes(p.tx)}<br/>↑{fmt_bytes(p.rx)}" if p.rx + p.tx else "—"
    where = esc(_where(geo)) if p.endpoint_ip else ""
    when = "" if is_online(p, now) else (f"<i>{tg_time(p.handshake)}</i>" if p.handshake else "<i>не подключался</i>")
    return [who, traffic, "<br/>".join(x for x in (where, when) if x) or "—"]


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
        hist = [x for x in state.get("geo_hist", []) if now - x[0] < 86400]
        if geo and geo.get("cc") == config.GEO_COUNTRY:
            icon = "❌" if geo["fail"] else ("✅" if geo["ok"] else "⚪")
            line = f"{flag} последняя проверка {tg_time(geo['ts'])}: {icon} {geo['ok']}/{geo['total']} узлов"
            if geo["fail"]:
                line += f" ({esc(', '.join(geo['failed']))})"
            if hist:
                line += f" · за сутки проверок {len(hist)}, неудачных {sum(1 for x in hist if x[2])}"
            lines.append(mark(line, bool(geo["fail"])))
        else:
            lines.append(f"{flag} доступность ещё не проверялась")
    day = state.get("traffic_day") or {}
    if insts and day.get("date"):
        _, rx, tx = report.day_traffic(day, insts, day["date"])
        now_online = sum(is_online(x, now) for i in insts if not i.error for x in i.peers)
        total = sum(len(i.peers) for i in insts if not i.error)
        lines.append(f"📈 трафик за сегодня ↓{fmt_bytes(tx)} ↑{fmt_bytes(rx)} · в сети {now_online}/{total}")
    return "<blockquote>" + "<br/>".join(lines) + "</blockquote>"


async def geo_view(pool, state: dict | None = None) -> str:
    target = ssh_target()
    if not target:
        return h("🌍 Доступность") + p("🔴 у сервера нет внешнего IPv4")
    blocks = [h("🌍 Доступность")]
    if state is not None:
        insts = None
        if tunnel.found():
            try:
                insts = await tunnel.collect()
            except CmdError:
                pass
        blocks.append(risk_card(state, insts))
    started = time.monotonic()
    try:
        info = await asyncio.get_running_loop().run_in_executor(pool, checkhost.node_info)
        res = await checkhost.check(pool, target, sorted(info))
    except checkhost.CheckHostError as e:
        return "".join(blocks) + p(f"🔴 check-host.net не ответил: {esc(e)}")
    countries = checkhost.by_country(res, info, first=config.GEO_COUNTRY)
    n_ok = sum(v is True for v in res.values())
    for c in countries:
        if c.cc != config.GEO_COUNTRY:
            continue
        blocks.append(h(f"{checkhost.flag(c.cc)} {esc(c.name)} — {len(c.ok)}/{len(c.ok) + len(c.fail) + len(c.silent)}", 4))
        blocks.append(table([[esc(city), "✅"] for city in c.ok] + [[mark(esc(city)), "❌"] for city in c.fail]
                            + [[esc(city), "⚪ не ответил"] for city in c.silent]))
    bad = [c for c in countries if c.cc != config.GEO_COUNTRY and c.fail]
    if bad:
        blocks.append(h("🔴 Недоступен", 4))
        blocks.append(p(*[mark(f"{checkhost.flag(c.cc)} {esc(c.name)}: ❌ {esc(', '.join(c.fail))}")
                          + (f" · ✅ {esc(', '.join(c.ok))}" if c.ok else "") for c in bad]))
    good = [c for c in countries if c.cc != config.GEO_COUNTRY and c.ok and not c.fail]
    if good:
        blocks.append(details(f"🟢 Доступен из {len(good)} стран", p(" ".join(
            f"{checkhost.flag(c.cc)} {esc(c.name)}" for c in good))))
    silent = [c for c in countries if c.cc != config.GEO_COUNTRY and not c.ok and not c.fail]
    if silent:
        blocks.append(details(f"⚪ Не ответили: {len(silent)}", p(" ".join(
            f"{checkhost.flag(c.cc)} {esc(c.name)}" for c in silent))))
    n_silent = sum(v is None for v in res.values())
    blocks.append(p(f"<i>TCP-подключение к порту SSH {esc(target)} · доступен с {n_ok} из {len(res)} узлов"
                    + (f", {n_silent} не ответили" if n_silent else "") + f" · {time.monotonic() - started:.0f} с</i>"))
    return "".join(blocks)


async def tunnel_view(insts: list[tunnel.Instance] | None = None, pool=None, emojis: dict | None = None) -> str:
    if insts is None:
        try:
            insts = await tunnel.collect()
        except CmdError as e:  # docker завис — честно скажем, а не «не удалось открыть раздел»
            return h("🔐 Клиенты") + p(f"🔴 docker не отвечает: {esc(e)}")
    if not insts:
        return h("🔐 Клиенты") + p("туннель на сервере не найден: нет docker-контейнера "
                                    "с подходящей утилитой (см. README, раздел про настройки)")
    now = time.time()
    blocks = [h("🔐 Клиенты")]
    for inst in insts:
        peers = inst.peers
        head = esc(inst.container)
        if not inst.error:
            head += f" · в сети {sum(is_online(x, now) for x in peers)}/{len(peers)}"
        blocks.append(h(head, 4))
        meta = []
        if inst.version:
            m = re.search(r"v\d[\w.]*", inst.version)
            meta.append(esc(m.group(0) if m else inst.version))
        meta += [x.split("/")[1].upper() + " " + x.split("/")[0] for x in inst.ports]
        if inst.error:
            blocks.append(p(f"<i>{' · '.join(meta)}</i>" if meta else "", f"🔴 не удалось прочитать данные: {esc(inst.error)}"))
            continue
        peers = tunnel.sort_by_ip(peers)
        geo = await _geo_all(pool, peers) if pool else {}
        blocks.append(table([peer_cells(x, now, geo.get(x.endpoint_ip), (emojis or {}).get(x.pubkey, "")) for x in peers]))
        total = f"Σ ↓{fmt_bytes(sum(x.tx for x in peers))} ↑{fmt_bytes(sum(x.rx for x in peers))}"
        if inst.started_at:
            total += f" за {fmt_duration(now - inst.started_at)}"
        tail = [f"<i>{total}</i>"] + ([f"<i>{' · '.join(meta)}</i>"] if meta else [])
        if not inst.names_source:
            tail.append("<i>список клиентов не найден — вместо имён ключи</i>")
        blocks.append(p(*tail))
    return "".join(blocks)


async def bans() -> str:
    """Забаненные сейчас по jail'ам; кнопка «Разбанить» — в строке с адресом."""
    jails = await system.jails()
    if not jails:
        return h("🛡 fail2ban") + p("не отвечает")
    blocks = [h("🛡 fail2ban"),
              table([[f"<b>{esc(j.name)}</b>", str(len(j.banned_now)), str(j.total_banned), str(j.total_failed)]
                     for j in jails], header=["jail", "сейчас", "всего", "неудач"])]
    for j in jails:
        if not j.banned_now:
            continue
        rows = [[f"<code>{esc(ip)}</code>", btn("Разбанить", f"ub:{j.name}:{ip}")] for ip in j.banned_now[:20]]
        if len(j.banned_now) > 20:
            rows.append([f"… и ещё {len(j.banned_now) - 20}", ""])
        blocks.append(details(f"{esc(j.name)}: {len(j.banned_now)} адресов", table(rows)))
    blocks.append(p("<i>счётчики «всего» — с последнего запуска fail2ban</i>"))
    return "".join(blocks)
