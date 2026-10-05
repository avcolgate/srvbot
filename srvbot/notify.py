"""Отправка сообщения владельцу из внешних хуков (PAM, fail2ban, systemd).

Только stdlib, чтобы запускаться быстро:
    srvbot-notify [--button "Текст=callback_data"]... "сообщение"   (или текст из stdin)
Нажатия на кнопки обрабатывает работающий бот.
"""
import argparse
import socket
import sys

from .config import ENV_FILE, load_env
from .tg import TGError, api_call


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--button", action="append", default=[], help="Текст=callback_data")
    ap.add_argument("text", nargs="?")
    a = ap.parse_args()
    text = a.text if a.text is not None else sys.stdin.read()
    env = load_env(ENV_FILE)
    payload = {"chat_id": env["OWNER_ID"], "text": f"{socket.gethostname().split('.')[0]}: {text}"[:4000]}
    buttons = [{"text": t, "callback_data": d} for t, _, d in (b.partition("=") for b in a.button)
               if d and len(d.encode()) <= 64]
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[b] for b in buttons]}
    try:
        api_call(env["BOT_TOKEN"], "sendMessage", payload, timeout=15)
    except TGError as e:
        print(f"srvbot-notify: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
