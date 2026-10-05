import asyncio
import logging
import os
import secrets
import shutil
import socket
import time
from datetime import datetime

from . import config, docker, manage, report, system, tunnel, views
from .alerts import Alerts
from .tg import Bot, kb, reply_kb
from .util import CmdError, State, cut_html, esc, fmt_ago, fmt_bytes

log = logging.getLogger(__name__)

BTN = {"status": "📊 Статус", "tun": "🔐 Клиенты", "geo": "🌍 Доступность",
       "bans": "🛡 Баны", "report": "📋 Отчёт", "ctl": "⚙️ Управление"}
COMMANDS = {"status": "status", "clients": "tun", "geo": "geo", "bans": "bans", "report": "report", "control": "ctl"}


def main_kb() -> dict:
    """Клавиатура внизу чата. Кнопка клиентов — только если туннель на сервере найден."""
    top = [BTN["status"]] + ([BTN["tun"]] if tunnel.found() else []) + [BTN["geo"]]
    return reply_kb(top, [BTN["bans"], BTN["report"], BTN["ctl"]])


def bot_commands() -> list[tuple[str, str]]:
    cmds = [("status", "Статус сервера"), ("clients", "Клиенты туннеля"),
            ("geo", "Доступность из разных стран"), ("bans", "fail2ban"), ("report", "Отчёт"),
            ("control", "Управление"), ("menu", "Показать кнопки")]
    return [c for c in cmds if c[0] != "clients" or tunnel.found()]


CTL_TEXT = "<b>⚙️ Управление</b>\nВыберите раздел. Любое действие выполняется только после подтверждения."
CTL_KB = kb([("📦 Обновления", "v:upd"), ("🧹 Очистка диска", "v:clean")],
            [("🔧 Службы и журналы", "v:svc")],
            [("⏻ Перезагрузить сервер", "go:reboot:")])

# Действия с подтверждением: kind -> arg -> (что сделать, предупреждение, куда вернуться)
ACTIONS = {
    "reboot": lambda a: ("перезагрузить сервер", "сервер и бот будут недоступны примерно минуту", "ctl"),
    "restart": lambda a: (f"перезапустить контейнер {a}", "его клиенты отключатся на несколько секунд", "svc"),
    "unit": lambda a: (f"перезапустить службу {a}", manage.RESTART_WARN.get(a, ""), "svc"),
    "svc": lambda a: (f"перезапустить службу {a}", manage.RESTART_WARN.get(a.removesuffix(".service"), ""), "svc"),
    "upgrade": lambda a: ("установить обновления", "", "upd"),
    "clean": lambda a: ("очистить диск", "", "clean"),
}

# Долгие разделы: сначала показываем «⏳», потом подменяем результатом
SLOW = {"geo": views.GEO_WAIT}
MAX_LEN = 4000


def refresh_kb(view: str) -> dict:
    return kb([("⟳ Обновить", f"v:{view}")])


def stamp(text: str) -> str:
    """Подпись со временем: видно, насколько свежие данные после «⟳ Обновить»."""
    return f"{text}\n\n<i>🕒 обновлено {datetime.now(config.TZ):%H:%M:%S}</i>"


def cut(text: str) -> str:
    return cut_html(text, MAX_LEN)


class Handlers:
    def __init__(self, bot: Bot, state: State, alerts: Alerts):
        self.bot, self.state, self.alerts = bot, state, alerts
        # Подтверждения опасных действий: token -> [описание, kind, arg, срок (0 — ещё не спрашивали)]
        self.pending: dict[str, list] = {}

    async def __call__(self, update: dict):
        # Только владелец и только в личном чате: в группе ответы увидели бы все участники
        if msg := update.get("message"):
            if msg.get("from", {}).get("id") == config.OWNER_ID and msg.get("chat", {}).get("id") == config.OWNER_ID \
                    and "text" in msg:
                log.info("update %s: %r", update.get("update_id"), msg["text"][:50])
                await self.on_message(msg)
        elif cq := update.get("callback_query"):
            if cq.get("from", {}).get("id") == config.OWNER_ID \
                    and cq.get("message", {}).get("chat", {}).get("id") == config.OWNER_ID:
                log.info("update %s: %r", update.get("update_id"), cq.get("data"))
                await self.on_callback(cq)

    # --- представления ---

    async def render(self, view: str) -> tuple[str, dict | None]:
        try:
            text, markup = await self._render(view)
        except Exception as e:  # раздел не открылся — показываем ошибку, а не вечное «⏳»
            log.exception("render %s failed", view)
            return (f"🔴 Не удалось открыть раздел: {esc(type(e).__name__)}: {esc(str(e)[:300])}",
                    kb([("⟳ Повторить", f"v:{view}"), ("◀ Управление", "v:ctl")]))
        return (text if view in ("ctl",) else stamp(text)), markup

    async def _render(self, view: str) -> tuple[str, dict | None]:
        if view == "status":
            return await views.status(self.state.data.get("geo_last")), refresh_kb(view)
        if view == "tun":
            text = await views.tunnel_view(pool=self.bot.pool, emojis=self.state.data.get("tunnel_emoji"))
            return text, refresh_kb(view)
        if view == "geo":
            return await views.geo_view(self.bot.pool), refresh_kb(view)
        if view == "bans":
            text, banned = await views.bans()
            rows = [[(f"Разбанить {ip}", d)] for jail, ip in banned if len((d := f"ub:{jail}:{ip}").encode()) <= 64]
            return text, kb(*rows, [("⟳ Обновить", "v:bans")])
        if view == "report":
            return (await report.build(self.state))[0], refresh_kb(view)
        if view == "ctl":
            return CTL_TEXT, CTL_KB
        if view == "upd":
            return await self.updates_view()
        if view == "clean":
            return await self.clean_view()
        if view == "svc":
            return await self.services_view()
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
        row = [("🔍 Проверить заново", "aptu:")] + ([("✅ Установить", "go:upgrade:")] if pkgs else [])
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
        return "\n".join(lines), kb([("🧹 Очистить", "go:clean:")], [("◀ Управление", "v:ctl")])

    async def services_view(self) -> tuple[str, dict]:
        icon = lambda st: "🟢" if st == "active" else "🟡" if st in ("activating", "reloading") else "🔴"
        states = await manage.unit_states()
        lines = ["<b>🔧 Службы и журналы</b>",
                 " · ".join(f"{icon(states.get(u, '?'))} {u}" for u in manage.SERVICES)]
        rows = []
        for u in manage.SERVICES:
            row = [(f"🔄 {u}", f"go:unit:{u}")] if u in manage.RESTARTABLE else []
            rows.append(row + [(f"📜 {u}", f"lg:u:{u}")])
        try:
            ctrs = [c for c in await docker.containers() if c.watched]
            if ctrs:
                lines.append("🐳 " + " · ".join(f"{icon('active' if c.running else c.status)} {esc(c.name)}" for c in ctrs))
            for c in ctrs:
                row = [(f"🔄 {c.name}", f"go:restart:{c.name}"), (f"📜 {c.name}", f"lg:c:{c.name}")]
                row = [(t, d) for t, d in row if len(d.encode()) <= 64]  # лимит callback_data в Telegram
                if row:
                    rows.append(row)
        except CmdError as e:
            lines.append(f"🔴 docker: {esc(e)}")
        lines.append("<i>🔄 — перезапуск (с подтверждением), 📜 — последние строки журнала</i>")
        return "\n".join(lines), kb(*rows, [("◀ Управление", "v:ctl")])

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
        return stamp(text), kb([("⟳ Обновить", f"lg:{what}:{name}"), ("◀ Службы", "v:svc")])

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
        if view in SLOW:
            sent = await self.bot.send(chat, SLOW[view])
            await self.replace_view(chat, sent["message_id"])
            text, markup = await self.render(view)
            await self.bot.edit(chat, sent["message_id"], cut(text), markup)
            return
        text, markup = await self.render(view)
        sent = await self.bot.send(chat, cut(text), markup)
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
            await self.bot.edit(chat, mid, cut(text), markup)
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
        elif kind == "ub":
            jail, _, ip = arg.partition(":")
            try:
                await system.unban(jail, ip)
                await self.bot.answer(cq["id"], f"{ip} разбанен", alert=True)
                await self.bot.drop_markup(chat, mid)
                await self.bot.send(chat, f"✅ {esc(ip)} разбанен в {esc(jail)}")
            except CmdError as e:
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
        markup = kb([("✅ Да, выполнить", f"do:{tok}"), ("✖ Отмена", cancel)])
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
        try:
            if kind == "reboot":
                await self.bot.edit(chat, mid, "⏻ Перезагружаю сервер… Бот напишет, когда сервер поднимется.")
                self.state.save()
                await asyncio.sleep(1)
                await system.reboot()
                return
            if kind == "upgrade":
                await manage.start_upgrade()
                self.state["upgrade"] = {"started": time.time()}
                self.state.save()
                await self.bot.edit(chat, mid, "⏳ Устанавливаю обновления в фоне. Результат придёт отдельным "
                                               "сообщением (обычно 1–5 минут).", back_kb)
                return
        except CmdError as e:
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
                await self.bot.edit(chat, mid, f"✅ <b>Очистка завершена</b> — освобождено {fmt_bytes(freed)}\n"
                                    + "\n".join(f"• {esc(d)}" for d in done), back_kb)
                return
            await self.bot.edit(chat, mid, f"✅ Выполнено: {esc(desc)}", back_kb)
        except CmdError as e:
            await self.bot.edit(chat, mid, f"🔴 Ошибка ({esc(desc)}): {esc(e)}", back_kb)
