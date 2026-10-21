import asyncio
import logging
import os
import secrets
import shutil
import socket
import time
from datetime import datetime

from . import config, docker, history, manage, system, tunnel, views
from .alerts import Alerts
from .tg import Bot, kb, reply_kb
from .util import INTERACTIVE, RICH_LEN, CmdError, State, cut_html, esc, fmt_ago, fmt_bytes, p, tg_time

log = logging.getLogger(__name__)

BTN = {"status": "📊 Сервер", "tun": "🔐 Клиенты", "geo": "🌍 Доступность", "ctl": "⚙️ Управление"}
COMMANDS = {"status": "status", "clients": "tun", "geo": "geo", "bans": "bans", "control": "ctl"}


def main_kb() -> dict:
    """Клавиатура внизу чата, по две кнопки в ряд (три в ряд на телефоне обрезаются).
    Кнопка клиентов — только если туннель на сервере найден."""
    names = [BTN["status"]] + ([BTN["tun"]] if tunnel.found() else []) + [BTN["geo"], BTN["ctl"]]
    return reply_kb(*[names[i:i + 2] for i in range(0, len(names), 2)])


def bot_commands() -> list[tuple[str, str]]:
    cmds = [("status", "Сервер: нагрузка, службы, порты"), ("clients", "Клиенты туннеля"),
            ("geo", "Доступность из разных стран"), ("bans", "fail2ban"),
            ("control", "Управление"), ("menu", "Показать кнопки")]
    return [c for c in cmds if c[0] != "clients" or tunnel.found()]


CTL_TEXT = "<b>⚙️ Управление</b>\nВыберите раздел. Любое действие выполняется только после подтверждения."
def ctl_kb() -> dict:
    """Клавиатура «Управления» с индикаторами: сколько обновлений ждёт, заполнен ли диск, нужна ли
    перезагрузка. Кнопка, которая сейчас требует внимания, — синяя. Всё читается из файлов, без команд."""
    upd, _ = system.updates()
    disk = shutil.disk_usage("/")
    disk_pct = disk.used / disk.total * 100
    reboot = system.reboot_required()
    attn = lambda on: ["primary"] if on else []
    return kb([("📦 Обновления" + (f" · {upd}" if upd else ""), "v:upd", *attn(upd)),
               ("🧹 Очистка диска" + (f" · {disk_pct:.0f}%" if disk_pct >= config.DISK_PCT - 10 else ""), "v:clean",
                *attn(disk_pct >= config.DISK_PCT - 10))],
              [("🗒 История", "v:hist"), ("📜 Журналы", "v:logs")],
              [("⏻ Перезагрузить сервер" + (" · нужно" if reboot else ""), "go:reboot:", *attn(reboot))])

# Действия с подтверждением: kind -> arg -> (что сделать, предупреждение, куда вернуться)
ACTIONS = {
    "reboot": lambda a: ("перезагрузить сервер", "сервер и бот будут недоступны примерно минуту", "ctl"),
    "restart": lambda a: (f"перезапустить контейнер {a}", "его клиенты отключатся на несколько секунд", "status"),
    "unit": lambda a: (f"перезапустить службу {a}", manage.RESTART_WARN.get(a, ""), "status"),
    "svc": lambda a: (f"перезапустить службу {a}", manage.RESTART_WARN.get(a.removesuffix(".service"), ""), "status"),
    "upgrade": lambda a: ("установить обновления", "", "upd"),
    "clean": lambda a: ("очистить диск", "", "clean"),
}

# Долгие разделы: сначала показываем «⏳», потом подменяем результатом
SLOW = {"geow": views.GEO_WAIT}
# Разделы в rich-формате (заголовки, таблицы, сворачиваемые блоки); остальное — обычные сообщения
RICH = {"status", "tun", "geo", "geow", "bans", "logs"}
# Названия разделов для журнала службы
VIEW_NAMES = {"status": "Сервер", "tun": "Клиенты", "geo": "Доступность", "geow": "Доступность по миру",
              "bans": "Баны", "ctl": "Управление",
              "upd": "Обновления", "clean": "Очистка диска", "hist": "История", "logs": "Журналы"}
MAX_LEN = 4000


def refresh_kb(view: str) -> dict:
    return kb([("⟳ Обновить", f"v:{view}")])


def stamp(text: str, rich: bool = False) -> str:
    """Подпись со временем: видно, насколько свежие данные после «⟳ Обновить»."""
    t = f"🕒 обновлено {tg_time(time.time(), 'T')}"
    return f"{text}<footer>{t}</footer>" if rich else f"{text}\n\n<i>{t}</i>"


def cut(text: str, rich: bool = False) -> str:
    return cut_html(text, RICH_LEN if rich else MAX_LEN, rich)


def describe_text(text: str) -> str:
    """Сообщение владельца — понятными словами для журнала службы."""
    text = text.strip()
    if text.startswith("/"):
        return f"команда {text.split()[0][:30]}"
    view = next((k for k, v in BTN.items() if v == text), None)
    return f"раздел «{VIEW_NAMES[view]}»" if view else f"сообщение {text[:30]!r}"


def describe_data(data: str) -> str:
    """Нажатие inline-кнопки — понятными словами для журнала службы."""
    kind, _, arg = data.partition(":")
    if kind in ("v", "nv"):
        return f"раздел «{VIEW_NAMES.get(arg, arg)}»"
    if kind in ("go", "ga"):
        k, _, a = arg.partition(":")
        return f"запрос подтверждения: {ACTIONS[k](a)[0]}" if k in ACTIONS else f"запрос подтверждения: {data[:40]}"
    if kind == "do":
        return "подтверждение действия"
    if kind in ("lg", "la"):
        return f"журнал {arg.partition(':')[2]}"
    if kind == "ub":
        jail, _, ip = arg.partition(":")
        return f"разбан {ip} в {jail}"
    return {"x": "отмена", "aptu": "проверка обновлений (apt update)", "chart": f"картинка «Сервер» за {arg or 1} дн.",
            "fg": f"не следить: {arg[:40]}"}.get(
        kind, f"кнопка {data[:40]!r}")


class Handlers:
    def __init__(self, bot: Bot, state: State, alerts: Alerts):
        self.bot, self.state, self.alerts = bot, state, alerts
        # Подтверждения опасных действий: token -> [описание, kind, arg, срок (0 — ещё не спрашивали)]
        self.pending: dict[str, list] = {}

    async def __call__(self, update: dict):
        INTERACTIVE.set(True)  # свой слот и короткий таймаут для внешних команд (см. util.run)
        # Только владелец и только в личном чате: в группе ответы увидели бы все участники
        if msg := update.get("message"):
            if msg.get("from", {}).get("id") == config.OWNER_ID and msg.get("chat", {}).get("id") == config.OWNER_ID \
                    and "text" in msg:
                await self.logged(describe_text(msg["text"]), self.on_message(msg))
        elif cq := update.get("callback_query"):
            if cq.get("from", {}).get("id") == config.OWNER_ID \
                    and cq.get("message", {}).get("chat", {}).get("id") == config.OWNER_ID:
                await self.logged(describe_data(cq.get("data", "")), self.on_callback(cq))

    @staticmethod
    async def logged(what: str, coro):
        """В журнал службы — что нажато, понятными словами, и сколько заняло."""
        started = time.monotonic()
        try:
            await coro
        finally:
            log.info("%s — %.1f с", what, time.monotonic() - started)

    # --- представления ---

    async def render(self, view: str) -> tuple[str, dict | None]:
        try:
            text, markup = await self._render(view)
        except Exception as e:  # раздел не открылся — показываем ошибку, а не вечное «⏳»
            log.exception("render %s failed", view)
            err = f"🔴 Не удалось открыть раздел: {esc(type(e).__name__)}: {esc(str(e)[:300])}"
            return (p(err) if view in RICH else err), kb([("⟳ Повторить", f"v:{view}"), ("◀ Сервер", "v:status")])
        return (text if view in ("ctl",) else stamp(text, view in RICH)), markup

    async def _render(self, view: str) -> tuple[str, dict | None]:
        if view == "status":
            text, banned = await views.status()
            return text, kb([("⟳ Обновить", "v:status"), ("📈 За сутки", "chart:")],
                            [("🛡 Баны" + (f" · {banned}" if banned else ""), "v:bans")])
        if view == "tun":
            text = await views.tunnel_view(emojis=self.state.data.get("tunnel_emoji"),
                                          traffic=self.state.data.get("traffic"))
            return text, refresh_kb(view)
        if view == "geo":  # мгновенно — из состояния; проверка по миру долгая, только по кнопке
            return (await views.geo_view(self.bot.pool, self.state.data, world=False),
                    kb([("⟳ Обновить", "v:geo"), ("🌍 Проверить по миру", "v:geow")]))
        if view == "geow":
            return (await views.geo_view(self.bot.pool, self.state.data),
                    kb([("⟳ Проверить заново", "v:geow"), ("◀ Кратко", "v:geo")]))
        if view == "bans":
            return await views.bans(), kb([("⟳ Обновить", "v:bans"), ("◀ Сервер", "v:status")])
        if view == "ctl":
            return CTL_TEXT, ctl_kb()
        if view == "upd":
            return await self.updates_view()
        if view == "clean":
            return await self.clean_view()
        if view == "hist":
            return history.view(self.state), kb([("⟳ Обновить", "v:hist")], [("◀ Управление", "v:ctl")])
        if view == "logs":
            return await views.journals(), kb([("⟳ Обновить", "v:logs"), ("🤖 Журнал бота", "lg:u:srvbot")],
                                              [("◀ Управление", "v:ctl")])
        raise ValueError(view)

    async def updates_view(self) -> tuple[str, dict]:
        lines = ["<b>📦 Обновления</b>"]
        back = [("◀ Управление", "v:ctl")]
        if manage.upgrade_running():
            lines.append("⏳ Идёт установка обновлений — результат придёт отдельным сообщением.")
            return "\n".join(lines), kb([("⟳ Обновить", "v:upd")], back)
        pkgs = await manage.upgradable()
        if pkgs:
            lines.append(f"Доступно <b>{len(pkgs)}</b>: " + esc(", ".join(pkgs)))
            lines += [f"⚠️ {w}" for w in manage.upgrade_warnings(pkgs)]
        else:
            lines.append("✅ Всё обновлено")
        if system.reboot_required():
            lines.append("⚠️ Ранее установленным обновлениям нужна перезагрузка")
        try:
            ts = os.path.getmtime("/var/lib/apt/periodic/update-success-stamp")
            lines.append(f"<i>списки пакетов проверялись {fmt_ago(ts)}</i>")
        except OSError:
            pass
        row = [("🔍 Проверить заново", "aptu:")] + ([("✅ Установить", "go:upgrade:", "success")] if pkgs else [])
        return "\n".join(lines), kb(row, back)

    async def clean_view(self) -> tuple[str, dict]:
        du = shutil.disk_usage("/")
        est = await manage.clean_estimate()
        names = {"journal": "журналы systemd", "apt": "кэш apt", "docker": "неиспользуемое в docker",
                 "vscode": "старые файлы VS Code"}
        lines = ["<b>🧹 Очистка диска</b>",
                 f"💾 свободно {fmt_bytes(du.free)} из {fmt_bytes(du.total)}", "", "Можно освободить примерно:"]
        lines += [f"• {names[k]} — {fmt_bytes(v)}" for k, v in est.items()]
        lines.append("• ненужные пакеты (apt autoremove) — сколько найдётся")
        lines.append(f"\n<i>всего ~{fmt_bytes(sum(est.values()))}; данные контейнеров и настройки не трогаются</i>")
        return "\n".join(lines), kb([("🧹 Очистить", "go:clean:", "success")], [("◀ Управление", "v:ctl")])

    async def logs_view(self, what: str, name: str) -> tuple[str, dict]:
        try:
            out = await (manage.unit_logs(name) if what == "u" else manage.container_logs(name))
        except CmdError as e:
            out = f"ошибка: {e}"
        host = socket.gethostname()
        lines = [esc(line.replace(f" {host} ", " ")) for line in out.strip().splitlines()] or ["журнал пуст"]
        kept, size = [], 0  # с конца, уже экранированные строки — чтобы влезть в лимит Telegram
        for line in reversed(lines):
            line = line[:300]
            if size + len(line) > 3300:
                kept.append("…")
                break
            kept.append(line)
            size += len(line) + 1
        text = f"<b>📜 {esc(name)}</b> — последние строки\n<pre>" + "\n".join(reversed(kept)) + "</pre>"
        return stamp(text), kb([("⟳ Обновить", f"lg:{what}:{name}"), ("◀ Сервер", "v:status")])

    async def send_chart(self, chat: int, days: int, mid: int | None = None):
        """Картинка «Сервер» за days дней: новым сообщением или (mid) заменой картинки на месте.
        Под ней — переключатель периода, текущий подсвечен."""
        from . import chart  # Pillow грузится только при рисовании
        if days not in chart.PERIODS:
            return
        cpu = await system.cpu_now(0.2)  # обычно — готовое значение фонового замера
        m = system.metrics(cpu)
        try:
            jpeg = await asyncio.get_running_loop().run_in_executor(
                self.bot.pool, chart.render, self.state.data.get("metrics"),
                (self.state.data.get("traffic") or {}).get("series"), time.time(), cpu, 100 - m.mem_avail_pct, days)
        except chart.ChartError as e:
            await self.bot.send(chat, f"🔴 График не нарисовался: {esc(e)}")
            return
        markup = kb([(chart.PERIODS[d][0].capitalize(), f"chart:{d}", *(["primary"] if d == days else []))
                     for d in chart.PERIODS], [("✖ Закрыть", "x:")])
        if mid:
            await self.bot.edit_photo(chat, mid, jpeg, markup)
        else:
            await self.bot.send_photo(chat, jpeg, markup=markup)

    def ask(self, kind: str, arg: str) -> str:
        """Регистрирует действие, ждущее подтверждения; токен живёт CONFIRM_TTL секунд."""
        now = time.time()
        for t in [t for t, it in self.pending.items() if it["deadline"] < now]:
            del self.pending[t]
        tok = secrets.token_hex(4)
        self.pending[tok] = {"kind": kind, "arg": arg, "deadline": now + config.CONFIRM_TTL}
        return tok

    # --- сообщения ---

    async def on_message(self, msg: dict):
        chat, text = msg["chat"]["id"], msg["text"].strip()
        if text.startswith("/"):
            cmd = text.split()[0][1:].split("@")[0]
            if cmd in ("start", "menu"):
                await self.bot.send(chat, "🤖 Мониторинг сервера. Кнопки внизу — основные разделы.", main_kb())
            elif cmd in COMMANDS:
                await self.show(chat, COMMANDS[cmd], msg["message_id"])
        elif view := next((k for k, v in BTN.items() if v == text), None):
            await self.show(chat, view, msg["message_id"])

    async def show(self, chat: int, view: str, request_id: int | None = None):
        """Новая сводка. «Чистый чат»: нажатие пользователя и предыдущая сводка удаляются,
        алерты и уведомления остаются."""
        if request_id:
            await self.bot.delete(chat, request_id)
        rich = view in RICH
        if view in SLOW:
            sent = await self.bot.send(chat, SLOW[view])
            await self.replace_view(chat, sent["message_id"])
            text, markup = await self.render(view)
            await self.bot.edit(chat, sent["message_id"], cut(text, rich), markup, rich=rich)
            return
        text, markup = await self.render(view)
        sent = await self.bot.send(chat, cut(text, rich), markup, rich=rich)
        await self.replace_view(chat, sent["message_id"])

    async def replace_view(self, chat: int, new_id: int):
        prev = self.state.data.get("view_msg")
        self.state["view_msg"] = new_id
        self.state.save()
        if prev and prev != new_id:
            await self.bot.delete(chat, prev)

    # --- inline-кнопки ---

    async def on_callback(self, cq: dict):
        data, chat, mid = cq.get("data", ""), cq["message"]["chat"]["id"], cq["message"]["message_id"]
        kind, _, arg = data.partition(":")
        if kind == "v":
            await self.bot.answer(cq["id"], "обновляю…")
            if arg in SLOW:
                await self.bot.edit(chat, mid, SLOW[arg])
            text, markup = await self.render(arg)
            rich = arg in RICH
            await self.bot.edit(chat, mid, cut(text, rich), markup, rich=rich)
        elif kind == "go":
            await self.on_ask(cq, chat, mid, *arg.partition(":")[::2])
        # Кнопки под алертами: результат — новым сообщением, сам алерт остаётся в чате
        elif kind == "nv":
            await self.bot.answer(cq["id"])
            await self.show(chat, arg)
        elif kind == "ga":
            await self.on_ask(cq, chat, mid, *arg.partition(":")[::2], new_msg=True)
        elif kind == "la":
            await self.bot.answer(cq["id"])
            what, _, name = arg.partition(":")
            text, markup = await self.logs_view(what, name)
            sent = await self.bot.send(chat, cut(text), markup)
            await self.replace_view(chat, sent["message_id"])
        elif kind == "x":
            await self.bot.answer(cq["id"], "отменено")
            await self.bot.delete(chat, mid)
        elif kind == "lg":
            await self.bot.answer(cq["id"])
            what, _, name = arg.partition(":")
            text, markup = await self.logs_view(what, name)
            await self.bot.edit(chat, mid, cut(text), markup)
        elif kind == "aptu":
            await self.bot.answer(cq["id"], "проверяю…")
            await self.bot.edit(chat, mid, "⏳ Обновляю списки пакетов (apt update), до минуты…")
            try:
                await manage.apt_update()
            except CmdError as e:
                await self.bot.edit(chat, mid, f"🔴 apt update: {esc(e)}", kb([("◀ Управление", "v:ctl")]))
                return
            text, markup = await self.render("upd")
            await self.bot.edit(chat, mid, cut(text), markup)
        elif kind == "do":
            await self.on_do(cq, chat, mid, arg)
        elif kind == "chart":
            await self.bot.answer(cq["id"], "рисую…")
            # из раздела «Сервер» — новой картинкой за сутки; с кнопок под картинкой — перерисовка на месте
            await self.send_chart(chat, int(arg) if arg else 1, mid if arg else None)
        elif kind == "ub":
            jail, _, ip = arg.partition(":")
            try:
                await system.unban(jail, ip)
                history.record(self.state, f"разбан {ip}", detail=jail)
                await self.bot.answer(cq["id"], f"{ip} разбанен", alert=True)
                text, markup = await self.render("bans")  # кнопка «Разбанить» у адреса должна исчезнуть
                await self.bot.edit(chat, mid, cut(text, True), markup, rich=True)
                await self.bot.send(chat, f"✅ {esc(ip)} разбанен в {esc(jail)}")
            except CmdError as e:
                history.record(self.state, f"разбан {ip}", ok=False, detail=str(e))
                await self.bot.answer(cq["id"], f"Ошибка: {e}"[:200], alert=True)
        elif kind == "fg":
            what, _, key = arg.partition(":")
            self.alerts.forget(what, key)
            await self.bot.answer(cq["id"], "Забыл")
            await self.bot.drop_markup(chat, mid)
            await self.bot.send(chat, f"👌 Больше не слежу: {esc(key)}")
        else:
            await self.bot.answer(cq["id"])

    async def on_ask(self, cq: dict, chat: int, mid: int, kind: str, arg: str, new_msg: bool = False):
        """Запрос подтверждения: в том же сообщении (из меню) или новым (из алерта — чтобы алерт остался)."""
        if kind not in ACTIONS:
            await self.bot.answer(cq["id"], "Неизвестное действие", alert=True)
            return
        desc, warn, back = ACTIONS[kind](arg)
        tok = self.ask(kind, arg)
        await self.bot.answer(cq["id"])
        text = f"❓ Точно {esc(desc)}?" + (f"\n⚠️ {esc(warn)}" if warn else "")
        text += f"\n<i>подтверждение действует {config.CONFIRM_TTL} с</i>"
        cancel = "x:" if new_msg else f"v:{back}"
        style = "danger" if kind in ("reboot", "restart", "unit", "svc") else "success"  # красная — для обрывов связи
        markup = kb([("✅ Да, выполнить", f"do:{tok}", style), ("✖ Отмена", cancel)])
        if new_msg:
            await self.bot.send(chat, text, markup)
        else:
            await self.bot.edit(chat, mid, text, markup)

    async def on_do(self, cq: dict, chat: int, mid: int, tok: str):
        item = self.pending.pop(tok, None)
        if not item or time.time() > item["deadline"]:
            await self.bot.answer(cq["id"], "Подтверждение истекло", alert=True)
            await self.bot.edit(chat, mid, "⌛ Подтверждение истекло, действие не выполнено.",
                                kb([("◀ Управление", "v:ctl")]))
            return
        kind, arg = item["kind"], item["arg"]
        desc, _, back = ACTIONS[kind](arg)
        back_kb = kb([("◀ Назад", f"v:{back}")])
        await self.bot.answer(cq["id"])
        started = time.monotonic()
        try:
            if kind == "reboot":
                await self.bot.edit(chat, mid, "⏻ Перезагружаю сервер… Бот напишет, когда сервер поднимется.")
                history.record(self.state, desc, detail="запущена")
                await asyncio.sleep(1)
                await system.reboot()
                return
            if kind == "upgrade":
                await manage.start_upgrade()
                self.state["upgrade"] = {"started": time.time()}
                history.record(self.state, desc, detail="запущена в фоне, результат — отдельной записью")
                await self.bot.edit(chat, mid, "⏳ Устанавливаю обновления в фоне. Результат придёт отдельным "
                                               "сообщением (обычно 1–5 минут).", back_kb)
                return
        except CmdError as e:
            history.record(self.state, desc, ok=False, detail=str(e))
            await self.bot.edit(chat, mid, f"🔴 Ошибка ({esc(desc)}): {esc(e)}", back_kb)
            return
        await self.bot.edit(chat, mid, f"⏳ {esc(desc)}…")
        try:
            if kind == "restart":
                await docker.restart(arg)
            elif kind == "unit":
                await manage.restart_unit(arg)
            elif kind == "svc":
                await manage.restart_unit(arg, any_unit=True)
            elif kind == "clean":
                before = manage.disk_free()
                done = await manage.clean()
                freed = max(0, manage.disk_free() - before)
                history.record(self.state, desc, detail=f"освобождено {fmt_bytes(freed)}",
                               took=time.monotonic() - started)
                await self.bot.edit(chat, mid, f"✅ <b>Очистка завершена</b> — освобождено {fmt_bytes(freed)}\n"
                                    + "\n".join(f"• {esc(d)}" for d in done), back_kb)
                return
            history.record(self.state, desc, took=time.monotonic() - started)
            await self.bot.edit(chat, mid, f"✅ Выполнено: {esc(desc)}", back_kb)
        except CmdError as e:
            history.record(self.state, desc, ok=False, detail=str(e), took=time.monotonic() - started)
            await self.bot.edit(chat, mid, f"🔴 Ошибка ({esc(desc)}): {esc(e)}", back_kb)
