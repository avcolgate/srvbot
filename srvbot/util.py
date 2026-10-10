import asyncio
import contextvars
import html
import json
import os
import re
import time

from . import config


class CmdError(Exception):
    pass


# Не больше двух фоновых внешних команд одновременно: docker/fail2ban-client занимают по 20–30 МБ,
# и параллельные вызовы складываются в пики памяти. У команд из чата свой слот и короткий таймаут,
# чтобы ответ не ждал зависшие фоновые проверки, когда сервер перегружен.
_cmd_slots = asyncio.Semaphore(2)
_ui_slot = asyncio.Semaphore(1)
INTERACTIVE = contextvars.ContextVar("interactive", default=False)  # True внутри обработки апдейта


async def run(*args: str, **kw) -> str:
    async with (_ui_slot if INTERACTIVE.get() else _cmd_slots):
        return await _run(*args, **kw)


async def _run(*args: str, timeout: float | None = None, check: bool = True, merge: bool = False) -> str:
    """Запускает команду без shell, возвращает stdout (merge=True — вместе с stderr)."""
    if timeout is None:
        timeout = config.UI_CMD_TIMEOUT if INTERACTIVE.get() else config.CMD_TIMEOUT
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge else asyncio.subprocess.PIPE)
    except FileNotFoundError as e:
        raise CmdError(f"{args[0]}: не найден") from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CmdError(f"{args[0]}: таймаут {timeout}с")
    if check and proc.returncode != 0:
        msg = (err or out).decode(errors="replace").strip().splitlines()
        raise CmdError(f"{' '.join(args[:3])}: {msg[-1] if msg else 'код ' + str(proc.returncode)}")
    return out.decode(errors="replace")


def fmt_bytes(b: float) -> str:
    for unit, size in (("ТБ", 2**40), ("ГБ", 2**30), ("МБ", 2**20), ("КБ", 2**10)):
        if b >= size:
            v = b / size
            return f"{v:.1f} {unit}" if unit in ("ТБ", "ГБ") else f"{v:.0f} {unit}"
    return f"{int(b)} Б"


def fmt_ago(ts: float, now: float | None = None) -> str:
    if not ts:
        return "никогда"
    d = int((now or time.time()) - ts)
    if d < 60:
        return "только что"
    if d < 3600:
        return f"{d // 60} мин назад"
    if d < 86400:
        return f"{d // 3600} ч назад"
    return f"{d // 86400} дн назад"


IND = "\u00a0" * 6  # отступ второй строки карточки (обычные пробелы Telegram может съесть)


def fmt_duration(sec: float) -> str:
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    return f"{d} дн {h} ч" if d else f"{h} ч {rem // 60} мин" if h else f"{rem // 60} мин"


def esc(s) -> str:
    return html.escape(str(s), quote=False)


class State:
    """Персистентное состояние бота (JSON)."""

    def __init__(self, path: str = config.STATE_FILE):
        self.path = path
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (FileNotFoundError, ValueError):  # ValueError — в т.ч. битый JSON и битый UTF-8
            self.data = {}

    def get(self, key, default=None):
        return self.data.setdefault(key, default)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        # Папка только для root: в состоянии IP, ключи клиентов и трафик
        d = os.path.dirname(self.path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
        tmp = self.path + ".tmp"
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)



_TAG = re.compile(r"<(/?)(b|i|u|s|code|pre|a)(?:\s[^>]*)?>")


def cut_html(text: str, limit: int = 4000) -> str:
    """Обрезает HTML-сообщение для Telegram (лимит 4096): по границе строки и с закрытием
    незакрытых тегов — иначе Telegram отвергает сообщение («can't parse entities»)."""
    if len(text) <= limit:
        return text
    head = text[:limit - 40]
    nl = head.rfind("\n")
    if nl > limit // 2:
        head = head[:nl]
    else:  # длинная строка без переносов — режем по пробелу, не внутри тега и не внутри &…;
        head = head[:max(head.rfind(" "), head.rfind(">") + 1)]
        if head.rfind("<") > head.rfind(">"):
            head = head[:head.rfind("<")]
        if head.rfind("&") > head.rfind(";"):
            head = head[:head.rfind("&")]
    stack = []
    for m in _TAG.finditer(head):
        if m.group(1):
            if stack and stack[-1] == m.group(2):
                stack.pop()
        else:
            stack.append(m.group(2))
    return head + "".join(f"</{t}>" for t in reversed(stack)) + "\n…"
