#!/bin/bash
# Установка бота мониторинга srvbot. Можно запускать повторно (обновление/переустановка).
#
#   sudo ./install.sh                 установить или обновить
#   sudo ./install.sh --no-ssh-alerts без уведомлений о входе по SSH (не трогать /etc/pam.d/sshd)
#   sudo ./install.sh --uninstall     удалить (настройки /etc/srvbot.env и состояние остаются)
#
# Установщик спрашивает токен, ID и страну для проверки доступности и пишет их в /etc/srvbot.env;
# уже заполненное не переспрашивает. Без вопросов — переменными окружения:
#   sudo BOT_TOKEN=... OWNER_ID=... CHECK_COUNTRY=de ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE=/etc/srvbot.env
PAM_FILE=/etc/pam.d/sshd
PAM_LINE="session optional pam_exec.so seteuid /usr/local/bin/srvbot-ssh-login"
F2B_ACTION=/etc/fail2ban/action.d/srvbot.conf

ssh_alerts=1
mode=install
for arg in "$@"; do
    case "$arg" in
        --no-ssh-alerts) ssh_alerts=0 ;;
        --uninstall) mode=uninstall ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "Неизвестный параметр: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mОшибка: %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "запустите от root: sudo $0 $*"

if [ "$mode" = uninstall ]; then
    say "Удаляю srvbot"
    systemctl disable --now srvbot.service 2>/dev/null || true
    rm -f /etc/systemd/system/srvbot.service /etc/systemd/system/srvbot-fail.service
    systemctl daemon-reload
    rm -f /usr/local/bin/srvbot-notify /usr/local/bin/srvbot-ssh-login
    if [ -f "$PAM_FILE" ] && grep -qxF "$PAM_LINE" "$PAM_FILE"; then
        tmp=$(mktemp)
        grep -vxF "$PAM_LINE" "$PAM_FILE" > "$tmp"
        cat "$tmp" > "$PAM_FILE"   # cat, а не mv — сохраняем права и владельца файла
        rm -f "$tmp"
        echo "  убрал хук входа по SSH из $PAM_FILE"
    fi
    if [ -f "$F2B_ACTION" ]; then
        if grep -rqs "^\s*srvbot\b" /etc/fail2ban/jail.local /etc/fail2ban/jail.d/ 2>/dev/null; then
            echo "  $F2B_ACTION оставил: action «srvbot» ещё используется в настройках fail2ban"
        else
            rm -f "$F2B_ACTION"
        fi
    fi
    echo "Готово. Остались: $ENV_FILE (токен) и /var/lib/srvbot (состояние) — удалите вручную, если не нужны."
    exit 0
fi

# --- проверки ---
command -v apt-get >/dev/null || die "нужен Debian/Ubuntu (apt)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null || die "нужен Python 3.10+"
if ! python3 -c 'import psutil' 2>/dev/null; then
    say "Ставлю python3-psutil"
    # на свежем сервере списки пакетов пусты, а apt может быть занят автообновлением
    apt-get -o DPkg::Lock::Timeout=300 update -qq
    apt-get -o DPkg::Lock::Timeout=300 install -y -qq python3-psutil >/dev/null
fi

# --- токен и владелец ---
if [ ! -s "$ENV_FILE" ]; then
    token="${BOT_TOKEN:-}"; owner="${OWNER_ID:-}"
    if [ -z "$token" ] || [ -z "$owner" ]; then
        if [ -t 0 ]; then
            echo "Нужны токен бота (@BotFather → /newbot) и ваш Telegram ID (@userinfobot)."
            [ -n "$token" ] || read -rp "BOT_TOKEN: " token
            [ -n "$owner" ] || read -rp "OWNER_ID:  " owner
        else
            install -m 600 "$DIR/srvbot.env.example" "$ENV_FILE"
            die "заполните $ENV_FILE (BOT_TOKEN и OWNER_ID) и запустите установку ещё раз"
        fi
    fi
    [[ "$owner" =~ ^[0-9]+$ ]] || die "OWNER_ID должен быть числом"
    umask 077
    printf 'BOT_TOKEN=%s\nOWNER_ID=%s\n' "$token" "$owner" > "$ENV_FILE"
    say "Сохранил токен и ID в $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# --- необязательные настройки: спрашиваем только то, чего ещё нет в файле ---
has_key() { grep -q "^$1=" "$ENV_FILE"; }
set_key() { has_key "$1" || printf '%s=%s\n' "$1" "$2" >> "$ENV_FILE"; }
ask() {  # ask КЛЮЧ "вопрос" [значение по умолчанию] — ответ из переменной окружения, иначе с клавиатуры
    local key=$1 prompt=$2 def=${3:-} val
    has_key "$key" && return 0
    val=${!key:-}
    if [ -z "$val" ] && [ -t 0 ]; then
        read -rp "$prompt${def:+ [$def]}: " val
    fi
    set_key "$key" "${val:-$def}"
}

ask CHECK_COUNTRY "Код страны для проверки доступности сервера (de, fr…; Enter — не проверять)"
cc=$(sed -n 's/^CHECK_COUNTRY=//p' "$ENV_FILE")
if [ -n "$cc" ] && ! [[ "$cc" =~ ^[A-Za-z]{2}$ ]]; then
    echo "  «$cc» не похоже на двухбуквенный код страны — проверьте CHECK_COUNTRY в $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

say "Проверяю токен"
bot_name=$(cd "$DIR" && python3 -c '
from srvbot import config
from srvbot.tg import TGError, api_call
try:
    print(api_call(config.BOT_TOKEN, "getMe", {})["username"])
except TGError as e:
    raise SystemExit(str(e))
') || die "Telegram не принял токен из $ENV_FILE"
echo "  бот: @$bot_name"

# --- файлы ---
say "Ставлю скрипты и службы"
dir_sed=$(printf '%s' "$DIR" | sed 's/[#&\\]/\\&/g')   # экранируем для sed
sed "s#@DIR@#$dir_sed#g" "$DIR/deploy/srvbot-notify.in" > /usr/local/bin/srvbot-notify
install -m 755 "$DIR/deploy/srvbot-ssh-login" /usr/local/bin/srvbot-ssh-login
chmod 755 /usr/local/bin/srvbot-notify
sed "s#@DIR@#$dir_sed#g" "$DIR/deploy/srvbot.service.in" > /etc/systemd/system/srvbot.service
install -m 644 "$DIR/deploy/srvbot-fail.service" /etc/systemd/system/srvbot-fail.service

if [ -d /etc/fail2ban/action.d ]; then
    install -m 644 "$DIR/deploy/fail2ban-srvbot.conf" "$F2B_ACTION"
    echo "  action для fail2ban: $F2B_ACTION (подключение к jail — см. README)"
fi

if [ "$ssh_alerts" = 1 ] && [ -f "$PAM_FILE" ]; then
    # Хук ставим ДО pam_env: с user_readenv=1 тот подгружает ~/.pam_environment пользователя,
    # и переменные вроде BASH_ENV/LD_PRELOAD попали бы в хук, который работает от root.
    tmp=$(mktemp)
    grep -vxF "$PAM_LINE" "$PAM_FILE" | awk -v line="$PAM_LINE" '
        !done && /^[[:space:]]*session[[:space:]].*pam_env\.so/ { print line; done = 1 }
        { print }
        END { if (!done) print line }' > "$tmp"
    if ! cmp -s "$tmp" "$PAM_FILE"; then
        cat "$tmp" > "$PAM_FILE"   # cat, а не mv — сохраняем права и владельца файла
        echo "  уведомления о входе по SSH включены ($PAM_FILE)"
    fi
    rm -f "$tmp"
fi

# --- запуск ---
systemctl daemon-reload
systemctl enable srvbot.service >/dev/null 2>&1
systemctl restart srvbot.service
sleep 3
if systemctl is-active -q srvbot.service; then
    say "Готово: бот @$bot_name запущен — он пришлёт сообщение в Telegram"
    echo "  логи: journalctl -u srvbot -f · настройки порогов: $DIR/srvbot/config.py"
else
    die "служба не запустилась, смотрите: journalctl -u srvbot -n 50"
fi
