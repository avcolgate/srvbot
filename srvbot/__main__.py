import asyncio
import logging
import os
import signal
import time

import psutil

from . import config, tunnel
from .alerts import Alerts
from .handlers import Handlers, bot_commands, main_kb
from .tg import Bot, TGError
from .util import State, fmt_duration


def start_text(state: State) -> str:
    """С чем бот запустился: после перезагрузки сервера (с длительностью простоя), после сбоя или штатно."""
    boot = psutil.boot_time()
    if time.time() - boot < 300:
        last_seen = state.data.get("last_seen") or 0
        gap = f" (не работал {fmt_duration(boot - last_seen)})" if 0 < last_seen < boot else ""
        return f"🔁 сервер перезагрузился{gap}, бот мониторинга запущен"
    if state.data.get("clean_exit") is False:
        return "⚠️ бот мониторинга перезапущен после сбоя"
    return "🤖 бот мониторинга запущен"


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not config.BOT_TOKEN or not config.OWNER_ID:
        raise SystemExit(f"BOT_TOKEN/OWNER_ID не заданы в {config.ENV_FILE}")
    bot = Bot(config.BOT_TOKEN)
    state = State()
    text = start_text(state)
    state["clean_exit"] = False
    state.save()
    alerts = Alerts(bot, state)
    try:  # сразу ищем туннель: от этого зависят кнопки и команды
        await tunnel.collect()
    except Exception:
        logging.exception("tunnel detection failed")
    # Сеть может быть ещё не готова (рестарт networkd при обновлениях) — ждём, а не падаем
    for delay in (2, 5, 10, 30, 60, 60, 60):
        try:
            await bot.call("setMyCommands", scope={"type": "chat", "chat_id": config.OWNER_ID},
                           commands=[{"command": c, "description": d} for c, d in bot_commands()])
            await bot.send(config.OWNER_ID, text, main_kb())
            break
        except TGError as e:
            logging.warning("старт: %s, повтор через %d с", e, delay)
            await asyncio.sleep(delay)

    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    tasks = [asyncio.create_task(alerts.run()), asyncio.create_task(bot.poll(Handlers(bot, state, alerts)))]
    waiter = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait([waiter, *tasks], return_when=asyncio.FIRST_COMPLETED)
    for t in tasks + [waiter]:
        t.cancel()
    for t in done:
        if t is not waiter and t.exception():
            raise t.exception()
    state["clean_exit"] = True
    state.save()


if __name__ == "__main__":
    asyncio.run(main())
    # Не ждём потоки с висящим long polling (до 60 с) — всё нужное уже сохранено
    logging.shutdown()
    os._exit(0)
