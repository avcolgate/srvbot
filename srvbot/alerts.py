"""Фоновые проверки: 🔴 при появлении проблемы, 🟢 при восстановлении, без повторов."""
import asyncio
import logging
import time
from datetime import datetime

from . import checkhost, config, docker, manage, report, system, tunnel
from .tg import Bot, kb
from .util import CmdError, State, cut_html, esc, fmt_duration

log = logging.getLogger(__name__)


def buttons(*items: tuple[str, str]) -> dict | None:
    """Кнопки под алертом (в один ряд). Слишком длинные callback_data (лимит Telegram 64 байта) пропускаем."""
    row = [(t, d) for t, d in items if len(d.encode()) <= 64]
    return kb(row) if row else None


def forget_kb(kind: str, key: str, *extra: tuple[str, str]) -> dict | None:
    return buttons(("Забыть", f"fg:{kind}:{key}"), *extra)


class Alerts:
    def __init__(self, bot: Bot, state: State):
        self.bot, self.state = bot, state
        # key -> {"title", "mid" — сообщение с 🔴, "since" — когда началось}; старые записи — просто заголовок
        self.active: dict[str, dict | str] = state.get("active", {})
        self.known_ctr: list[str] = state.get("containers", [])     # контейнеры, которые должны работать
        self.known_ports: dict[str, str] = state.get("ports", {})   # "tcp/443" -> процесс
        self.streak: dict[str, int] = {}
        self.ctrs: dict[str, docker.Container] | None = None   # контейнеры по последней проверке; None — docker не ответил
        self.cpu = system.CpuMeter()
        self.name_wait: dict[str, int] = {}  # новые клиенты без имени: сколько проверок уже ждём

    async def send(self, text: str, markup=None, reply_to: int | None = None) -> dict | None:
        """Отправленное сообщение или None, если Telegram не принял."""
        try:
            return await self.bot.send(config.OWNER_ID, cut_html(text), markup, reply_to)
        except Exception:
            log.exception("send failed")
            return None

    async def set(self, key: str, bad: bool, title: str, problem: str = "", sustain: int = 1, markup=None):
        if bad:
            self.streak[key] = self.streak.get(key, 0) + 1
            # активным алерт становится только после успешной отправки — иначе повторим в следующий цикл
            if self.streak[key] >= sustain and key not in self.active:
                if msg := await self.send(f"🔴 <b>{esc(title)}</b>: {esc(problem)}", markup):
                    self.active[key] = {"title": title, "mid": msg["message_id"], "since": time.time()}
                    self.state.save()
        else:
            self.streak.pop(key, None)
            if key not in self.active:
                return
            info = self.active[key] if isinstance(self.active[key], dict) else {}
            mid, since = info.get("mid"), info.get("since")
            took = f" (было {fmt_duration(time.time() - since)})" if since else ""
            # 🟢 — ответом на свой 🔴, а кнопки с 🔴 убираем: проблема уже ушла
            if await self.send(f"🟢 <b>{esc(title)}</b>: снова в норме{took}", reply_to=mid):
                del self.active[key]
                self.state.save()
                if mid:
                    try:
                        await self.bot.drop_markup(config.OWNER_ID, mid)
                    except Exception:
                        log.info("не удалось убрать кнопки с алерта %s", mid)

    def forget(self, kind: str, key: str):
        if kind == "ctr" and key in self.known_ctr:
            self.known_ctr.remove(key)
        if kind == "port":
            self.known_ports.pop(key, None)
        self.active.pop(f"{kind}:{key}", None)
        self.streak.pop(f"{kind}:{key}", None)
        self.state.save()

    # --- проверки ---

    async def check_containers(self):
        try:
            ctrs = {c.name: c for c in await docker.containers()}
        except CmdError as e:
            self.ctrs = None
            await self.set("docker", True, "docker", str(e), sustain=2,
                           markup=buttons(("🔄 Перезапустить docker", "ga:unit:docker"), ("📜 Журнал", "la:u:docker")))
            return
        self.ctrs = ctrs
        await self.set("docker", False, "docker")
        for c in ctrs.values():
            if c.watched and c.name not in self.known_ctr:
                self.known_ctr.append(c.name)
                self.state.save()
        for name in list(self.known_ctr):
            c = ctrs.get(name)
            if c is None:
                await self.set(f"ctr:{name}", True, f"контейнер {name}",
                               "исчез (удалён?). Если так и задумано — нажмите «Забыть»",
                               markup=forget_kb("ctr", name, ("🔧 Службы", "nv:svc")))
            elif not c.watched:
                self.forget("ctr", name)  # restart policy убрали — больше не следим
            else:
                await self.set(f"ctr:{name}", not c.running, f"контейнер {name}", f"не работает ({c.status})",
                               markup=buttons(("🔄 Перезапустить", f"ga:restart:{name}"), ("📜 Журнал", f"la:c:{name}")))

    async def check_resources(self):
        m = system.metrics(self.cpu.pct())
        await self.set("disk", m.disk_pct >= config.DISK_PCT, "диск", f"заполнен на {m.disk_pct:.0f}%",
                       markup=buttons(("🧹 Очистить диск", "nv:clean")))
        await self.set("cpu", m.cpu_pct >= config.CPU_PCT, "CPU",
                       f"загрузка {m.cpu_pct:.0f}% уже {config.SUSTAIN_CHECKS} мин", sustain=config.SUSTAIN_CHECKS,
                       markup=buttons(("📊 Статус", "nv:status"), ("🔧 Службы", "nv:svc")))
        psi = m.mem_psi_full or 0
        mem_bad = m.mem_avail_pct < config.MEM_AVAIL_PCT or psi >= config.MEM_PSI_FULL
        await self.set("mem", mem_bad, "память",
                       f"доступно {m.mem_avail_pct:.0f}% RAM, давление {psi:.0f}% уже {config.SUSTAIN_CHECKS} мин",
                       sustain=config.SUSTAIN_CHECKS,
                       markup=buttons(("📊 Статус", "nv:status"), ("🔧 Службы", "nv:svc")))

    async def check_ports(self):
        cur, stale = system.listening()
        for p, proc in cur.items():
            if p not in self.known_ports:
                self.known_ports[p] = proc
                self.state.save()
        for p, proc in list(self.known_ports.items()):
            if p not in cur and proc == "docker-proxy" and self._unpublished(p):
                # контейнер пересоздали с другим портом (docker жив, все контейнеры работают) — это не поломка
                self.forget("port", p)
                await self.send(f"ℹ️ порт {esc(p)} больше не публикуется контейнерами — перестал за ним следить")
                continue
            problem = (f"привязан к адресу {stale[p]}, которого больше нет на сервере" if p in stale
                       else "больше не слушается") + ". Если так и задумано — нажмите «Забыть»"
            await self.set(f"port:{p}", p not in cur, f"порт {p} ({proc})", problem,
                           sustain=config.PORT_MISS_CHECKS, markup=forget_kb("port", p, ("🔧 Службы", "nv:svc")))

    def _unpublished(self, port: str) -> bool:
        """Порт «udp/443» снят с публикации намеренно: docker ответил, ни один отслеживаемый контейнер
        не лежит и не исчез, и никто этот порт не публикует."""
        if self.ctrs is None or any(n not in self.ctrs or not self.ctrs[n].running for n in self.known_ctr):
            return False
        proto, _, num = port.partition("/")
        return all(f"{num}/{proto}" not in c.ports for c in self.ctrs.values())

    async def check_certs(self):
        for c in await system.certs():
            await self.set(f"cert:{c.name}", c.days_left < config.CERT_WARN_DAYS, f"сертификат {c.name}",
                           f"истекает через {c.days_left} дн.")

    async def check_units(self):
        failed = set(await system.failed_units())
        for unit in failed:
            await self.set(f"unit:{unit}", True, f"служба {unit}", "упала",
                           markup=buttons(("🔄 Перезапустить", f"ga:svc:{unit}"), ("📜 Журнал", f"la:u:{unit}")))
        for key in [k for k in self.active if k.startswith("unit:")]:
            if key[5:] not in failed:
                await self.set(key, False, f"служба {key[5:]}")

    async def check_ip(self):
        ips = system.public_ipv4()
        await self.set("ip:none", not ips, "внешний IP", "на интерфейсах нет ни одного внешнего IPv4", sustain=2)
        prev = self.state.data.get("public_ips")
        if ips and prev != ips:
            self.state["public_ips"] = ips
            self.state.save()
            if prev is not None:
                await self.send(f"🌐 <b>IP сервера изменился</b>: {esc(', '.join(prev) or '—')} → "
                                f"<b>{esc(', '.join(ips))}</b>\nСлужбы, привязанные к старому адресу, "
                                f"перестанут принимать подключения — их настройки нужно обновить.")

    async def check_tunnel(self):
        known: dict[str, list[str]] = self.state.get("tunnel_peers", {})  # контейнер -> ключи виденных клиентов
        emojis: dict[str, str] = self.state.get("tunnel_emoji", {})       # ключ -> эмодзи клиента
        insts = await tunnel.collect()
        ok = [i for i in insts if not i.error]
        keys = [p.pubkey for p in tunnel.sort_by_ip([p for i in ok for p in i.peers])]
        # пока какой-то контейнер не читается (или туннель не виден вовсе), удалённых не забываем —
        # иначе эмодзи перетасуются
        if tunnel.assign_emoji(emojis, keys, forget_missing=bool(ok) and len(ok) == len(insts)):
            self.state.save()
        for inst in insts:
            await self.set(f"tunnel:{inst.container}", bool(inst.running and inst.error),
                           f"туннель {inst.container}", f"не удалось прочитать данные: {inst.error}",
                           sustain=2, markup=buttons(("🔄 Перезапустить", f"ga:restart:{inst.container}"),
                                                     ("📜 Журнал", f"la:c:{inst.container}")))
            if inst.error:
                continue
            current = {p.pubkey for p in inst.peers}
            if inst.container not in known:  # первый запуск или новый контейнер — запоминаем молча
                known[inst.container] = sorted(current)
                self.state.save()
                continue
            seen = set(known[inst.container])
            for p in tunnel.new_peers(known[inst.container], inst):
                # имя в списке клиентов может появиться чуть позже — даём ему одну проверку
                if not p.name and self.name_wait.get(p.pubkey, 0) < 1:
                    self.name_wait[p.pubkey] = self.name_wait.get(p.pubkey, 0) + 1
                    continue
                self.name_wait.pop(p.pubkey, None)
                seen.add(p.pubkey)
                em = emojis.get(p.pubkey, "")
                await self.send(f"🆕 <b>Новый клиент</b>: {em + ' ' if em else ''}"
                                f"{esc(p.label)} · <code>{esc(p.ip)}</code>\n"
                                f"<i>{esc(inst.container)}</i>")
            updated = sorted(seen & current)  # удалённые пиры забываем: если вернутся — сообщим снова
            if updated != known[inst.container]:
                known[inst.container] = updated
                self.state.save()

    async def check_upgrade(self):
        """Обновления ставятся в отдельном юните; когда он закончился — сообщаем результат."""
        if "upgrade" not in self.state.data or manage.upgrade_running():
            return
        self.state.data.pop("upgrade")
        self.state.save()
        res = manage.upgrade_result()
        if res is None:
            await self.send(f"⚠️ Установка обновлений завершилась без результата — см. {manage.UPGRADE_LOG}")
            return
        rc, tail = res
        text = ("✅ <b>Обновления установлены</b>" if rc == 0
                else f"🔴 <b>Установка обновлений завершилась с ошибкой</b> (код {rc})")
        if tail:
            text += f"\n<pre>{esc(tail[-1500:])}</pre>"
        markup = None
        if system.reboot_required():
            text += "\n⚠️ Нужна перезагрузка, чтобы обновления заработали полностью."
            markup = buttons(("⏻ Перезагрузить", "ga:reboot:"))
        await self.send(text, markup)

    async def check_geo(self, nodes: dict):
        """Одна проверка доступности из страны GEO_COUNTRY; nodes — кэш узлов {"ts", "target", "ctl"}."""
        if not nodes.get("target") or time.time() - nodes.get("ts", 0) > 86400:
            nodes["target"], nodes["ctl"] = await asyncio.get_running_loop().run_in_executor(
                self.bot.pool, checkhost.nodes, config.GEO_COUNTRY)
            nodes["ts"] = time.time()
        ips = system.public_ipv4()
        if not ips or not nodes["target"]:
            return
        ssh = [p for p, proc in system.listening()[0].items() if proc == "sshd" and p.startswith("tcp/")]
        target = f"{ips[0]}:{ssh[0].split('/')[1] if ssh else 22}"
        res = await checkhost.check(self.bot.pool, target, nodes["target"] + nodes["ctl"])
        v = checkhost.verdict(res, nodes["target"], nodes["ctl"])
        self.state["geo_last"] = {"ts": time.time(), "target": target, "cc": config.GEO_COUNTRY, **v.to_dict()}
        self.state.save()
        if v.down or v.reachable:  # иначе данных мало (узлы не ответили) — состояние не меняем
            flag = checkhost.flag(config.GEO_COUNTRY)
            await self.set("geo", v.down, f"доступ из {flag}",
                           f"{target} не отвечает с {v.fail} из {v.total} узлов check-host.net в {flag} "
                           f"({', '.join(v.failed)}), а с контрольных доступен ({v.ctl_ok}/{v.ctl_total})",
                           sustain=config.GEO_SUSTAIN, markup=buttons(("🌍 Проверить по миру", "nv:geo")))

    async def run_geo(self):
        if not config.GEO_COUNTRY:
            return
        nodes: dict = {}
        await asyncio.sleep(30)  # дать боту стартовать
        while True:
            try:
                await self.check_geo(nodes)
            except checkhost.CheckHostError as e:
                log.warning("check-host: %s", e)
            except Exception:
                log.exception("check geo failed")
            await asyncio.sleep(config.GEO_CHECK_INTERVAL)

    # --- расписание ---

    async def scheduled(self):
        now = datetime.now(config.TZ)
        week = "%d-W%02d" % now.isocalendar()[:2]
        if "last_weekly" not in self.state.data:  # первый запуск — только базовый снимок, без рассылки
            self.state["last_weekly"] = week
            self.state["snap"] = (await report.build(self.state))[1]
            self.state.save()
            return
        if now.hour < config.REPORT_HOUR:
            return
        # Отметку «отправлено» ставим только после успешной отправки: сбой — повторим через минуту
        if now.weekday() >= config.WEEKLY_WEEKDAY and self.state["last_weekly"] != week:
            text, snap = await report.build(self.state)
            if await self.send(text):
                self.state["snap"], self.state["last_weekly"] = snap, week
                self.state.save()

    async def run(self):
        geo_task = asyncio.create_task(self.run_geo())  # своим темпом: раз в 10 мин, с ожиданием результата
        while True:
            started = time.monotonic()
            for name, fn in (("containers", self.check_containers), ("resources", self.check_resources),
                             ("ip", self.check_ip), ("ports", self.check_ports), ("units", self.check_units),
                             ("certs", self.check_certs), ("upgrade", self.check_upgrade),
                             ("scheduled", self.scheduled)):
                try:
                    await fn()
                except Exception:
                    log.exception("check %s failed", name)
            self.state["last_seen"] = time.time()  # по этой отметке при старте считаем, сколько сервер лежал
            self.state.save()
            try:
                await self.check_tunnel()
            except Exception:
                log.exception("check tunnel failed")
            await asyncio.sleep(max(1.0, config.CHECK_INTERVAL - (time.monotonic() - started)))
