"""Настройки бота.

Секреты и всё, что зависит от конкретного сервера, — в /etc/srvbot.env (см. srvbot.env.example).
Здесь — пороги и расписания.
"""
import logging
import os
from zoneinfo import ZoneInfo

ENV_FILE = "/etc/srvbot.env"
STATE_FILE = "/var/lib/srvbot/state.json"


def load_env(path: str = ENV_FILE) -> dict:
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.split(" #", 1)[0]  # комментарий в конце строки
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _list(value: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in value.split(",") if x.strip())


def _system_tz() -> str:
    """Часовой пояс сервера: /etc/timezone или ссылка /etc/localtime -> …/zoneinfo/<пояс>."""
    try:
        with open("/etc/timezone") as f:
            return f.read().strip()
    except OSError:
        pass
    link = os.path.realpath("/etc/localtime")
    return link.split("/zoneinfo/", 1)[1] if "/zoneinfo/" in link else "UTC"


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name or _system_tz())
    except (ValueError, KeyError, OSError):  # опечатка в TIMEZONE не должна ронять бота
        logging.getLogger(__name__).warning("TIMEZONE=%r не распознан, использую UTC", name)
        return ZoneInfo("UTC")


_env = load_env() if os.path.exists(ENV_FILE) else {}
BOT_TOKEN = _env.get("BOT_TOKEN", "")
OWNER_ID = int(_env.get("OWNER_ID", "0") or 0)
TZ = _tz(_env.get("TIMEZONE", ""))

# Внешние команды (docker, fail2ban-client, systemctl…)
CMD_TIMEOUT = 30             # сек для фоновых проверок
UI_CMD_TIMEOUT = 10          # сек для команд из чата: лучше быстро ответить «не отвечает», чем молчать

# Цикл алертов
CHECK_INTERVAL = 60          # сек
DISK_PCT = 90                # алерт при заполнении / >= N%
CPU_PCT = 90                 # средняя загрузка CPU за минуту
MEM_AVAIL_PCT = 8            # алерт, если доступно меньше N% RAM
MEM_PSI_FULL = 10            # /proc/pressure/memory full avg60, %
SUSTAIN_CHECKS = 5           # сколько проверок подряд должна держаться проблема CPU/RAM
PORT_MISS_CHECKS = 2         # сколько проверок подряд порт должен отсутствовать
# Процессы, чьи слушающие порты бот запоминает и контролирует (+ PORT_PROCESSES из env)
PORT_PROCESSES = {"nginx", "docker-proxy", "sshd"} | set(_list(_env.get("PORT_PROCESSES", "")))

# Недельный отчёт (время по TZ)
REPORT_HOUR = 10
WEEKLY_WEEKDAY = 0           # понедельник
CERT_WARN_DAYS = 14

# Docker: какие restart policy считаются «должен работать»
WATCH_RESTART_POLICIES = {"always", "unless-stopped", "on-failure"}

# Раздел клиентов туннеля. Контейнер, утилита и список клиентов находятся автоматически;
# значения из env — только чтобы переопределить автоопределение.
TUNNEL_MATCH = _list(_env.get("TUNNEL_MATCH", ""))      # подстроки имени/образа контейнера
TUNNEL_TOOLS = _list(_env.get("TUNNEL_TOOLS", ""))      # утилиты внутри контейнера, первая найденная
TUNNEL_CLIENTS = _list(_env.get("TUNNEL_CLIENTS", ""))  # пути/шаблоны к JSON со списком клиентов
TUNNEL_ONLINE_SEC = 180      # рукопожатие свежее N секунд — клиент в сети

# Доступность из выбранной страны через check-host.net (код страны в env, пусто — выключено)
GEO_COUNTRY = _env.get("CHECK_COUNTRY", "").lower()
GEO_CHECK_INTERVAL = 600     # сек между проверками (бесплатный сервис — не чаще)
GEO_SUSTAIN = 2              # сколько проверок подряд «недоступен» до алерта (~20 мин)

LETSENCRYPT_LIVE = "/etc/letsencrypt/live"
CONFIRM_TTL = 60             # сек на подтверждение опасного действия
