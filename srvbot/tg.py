"""Минимальный клиент Telegram Bot API на stdlib (aiogram слишком тяжёл: ~160 МБ при импорте).

Соединение только по IPv4: IPv6-маршрут до api.telegram.org с этого сервера иногда зависает,
и стандартный клиент ждал полный таймаут (30–40 с), прежде чем попробовать IPv4.
"""
import asyncio
import http.client
import json
import logging
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

API_HOST = "api.telegram.org"
CONNECT_TIMEOUT = 10
_ssl = ssl.create_default_context()


class TGError(Exception):
    pass


def _connect_ipv4(address, timeout=None, source_address=None, **_):
    host, port = address
    err = OSError(f"{host}: нет IPv4-адресов")
    for af, st, proto, _, sa in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM):
        s = socket.socket(af, st, proto)
        try:
            s.settimeout(CONNECT_TIMEOUT)
            s.connect(sa)
            s.settimeout(timeout)
            return s
        except OSError as e:
            err = e
            s.close()
    raise err


def api_call(token: str, method: str, params: dict, timeout: float = 30):
    """Синхронный вызов Bot API. Возвращает result или бросает TGError."""
    body = json.dumps(params).encode()
    for attempt in range(2):
        conn = http.client.HTTPSConnection(API_HOST, timeout=timeout, context=_ssl)
        conn._create_connection = _connect_ipv4
        try:
            conn.request("POST", f"/bot{token}/{method}", body, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read() or b"{}")
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise TGError(f"{method}: {type(e).__name__}: {e}") from None
        finally:
            conn.close()
        if data.get("ok"):
            return data.get("result")
        retry = (data.get("parameters") or {}).get("retry_after")
        if resp.status == 429 and retry and attempt == 0:
            time.sleep(retry)
            continue
        raise TGError(f"{method}: {data.get('description', resp.status)}")


def kb(*rows: list[tuple[str, str]]) -> dict:
    """Inline-клавиатура: kb([("Текст", "callback"), ...], ...)."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def reply_kb(*rows: list[str]) -> dict:
    """Постоянная клавиатура внизу чата."""
    return {"keyboard": [[{"text": t} for t in row] for row in rows],
            "resize_keyboard": True, "is_persistent": True}


class Bot:
    def __init__(self, token: str):
        self.token = token
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tg")
        self._tasks: set[asyncio.Task] = set()

    async def call(self, method: str, _timeout: float = 30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        return await asyncio.get_running_loop().run_in_executor(
            self.pool, api_call, self.token, method, params, _timeout)

    async def send(self, chat_id: int, text: str, markup: dict | None = None, reply_to: int | None = None):
        reply = {"message_id": reply_to, "allow_sending_without_reply": True} if reply_to else None
        return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                               reply_markup=markup, reply_parameters=reply,
                               link_preview_options={"is_disabled": True})

    async def edit(self, chat_id: int, message_id: int, text: str, markup: dict | None = None):
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                            parse_mode="HTML", reply_markup=markup, link_preview_options={"is_disabled": True})
        except TGError as e:
            if "not modified" not in str(e):
                raise

    async def drop_markup(self, chat_id: int, message_id: int):
        try:
            await self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id)
        except TGError as e:
            if "not modified" not in str(e):
                raise

    async def delete(self, chat_id: int, message_id: int):
        """Удаляет сообщение; старше 48 ч или уже удалённое — молча пропускаем."""
        try:
            await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except TGError as e:
            log.info("%s", e)

    async def answer(self, callback_id: str, text: str | None = None, alert: bool = False):
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text, show_alert=alert)
        except TGError as e:
            log.warning("%s", e)  # запрос устарел — не страшно

    async def poll(self, handler, allowed=("message", "callback_query")):
        """Long polling; каждый апдейт обрабатывается отдельной задачей."""
        offset, backoff = None, 1
        while True:
            try:
                updates = await self.call("getUpdates", _timeout=60, offset=offset, timeout=50,
                                          allowed_updates=list(allowed))
                backoff = 1
            except TGError as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                t = asyncio.create_task(self._safe(handler, u))
                self._tasks.add(t)
                t.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _safe(handler, update):
        started = time.monotonic()
        try:
            await handler(update)
        except Exception:
            log.exception("update %s failed", update.get("update_id"))
        finally:
            took = time.monotonic() - started
            if took > 5:
                log.warning("update %s обрабатывался %.1f с", update.get("update_id"), took)
