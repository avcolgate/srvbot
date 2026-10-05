"""Управление сервером из бота: обновления, службы и журналы, очистка диска."""
import glob
import json
import os
import re
import shutil

import psutil

from .util import CmdError, run

# --- обновления ---

UPGRADE_UNIT = "srvbot-upgrade"
UPGRADE_LOG = "/var/lib/srvbot/upgrade.log"  # папка только для root (в /var/log можно подменить ссылкой)
UPGRADE_RC = "/var/lib/srvbot/upgrade.rc"
APT_ENV = ("env", "DEBIAN_FRONTEND=noninteractive")
APT_OPTS = ("-o", "Dpkg::Options::=--force-confold", "-o", "Dpkg::Options::=--force-confdef")


async def apt_update() -> None:
    await run(*APT_ENV, "apt-get", "update", "-qq", timeout=180)


async def upgradable() -> list[str]:
    """Пакеты, которые поставит dist-upgrade (включая новые, например новое ядро)."""
    out = await run(*APT_ENV, "apt-get", "-s", *APT_OPTS, "dist-upgrade", timeout=120)
    return [line.split()[1] for line in out.splitlines() if line.startswith("Inst ")]


def upgrade_warnings(pkgs: list[str]) -> list[str]:
    res = []
    if any(p.startswith(("docker", "containerd")) for p in pkgs):
        res.append("перезапустится docker — контейнеры и их клиенты отключатся примерно на 20 с")
    if any(p.startswith(("linux-image", "linux-modules", "linux-generic")) for p in pkgs):
        res.append("новое ядро заработает после перезагрузки")
    if any(p.startswith(("libc6", "systemd", "libssl", "openssl")) for p in pkgs):
        res.append("перезапустятся системные службы — бот может на минуту пропасть")
    return res


def upgrade_running() -> bool:
    return os.path.exists(f"/run/systemd/transient/{UPGRADE_UNIT}.service")


async def start_upgrade() -> None:
    """Установка в отдельном юните systemd: её не прервёт перезапуск бота (needrestart и т.п.)."""
    if os.path.exists(UPGRADE_RC):
        os.remove(UPGRADE_RC)
    apt = " ".join(["apt-get", "-y", *APT_OPTS, "dist-upgrade"])
    script = f"{apt} > {UPGRADE_LOG} 2>&1; echo $? > {UPGRADE_RC}"
    await run("systemd-run", f"--unit={UPGRADE_UNIT}", "--collect", "--description=srvbot: установка обновлений",
              "--setenv=DEBIAN_FRONTEND=noninteractive", "/bin/sh", "-c", script)


def upgrade_result() -> tuple[int, str] | None:
    """(код возврата, хвост лога), если установка закончилась; иначе None."""
    try:
        with open(UPGRADE_RC) as f:
            rc = int(f.read().strip() or 1)
    except (FileNotFoundError, ValueError):
        return None
    try:
        with open(UPGRADE_LOG, errors="replace") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    os.remove(UPGRADE_RC)
    summary = [l for l in lines if re.match(r"\d+ upgraded|\d+ обновлено", l)]
    tail = summary or lines[-8:]
    return rc, "\n".join(tail)


# --- службы и журналы ---

SERVICES = ("docker", "fail2ban", "ssh", "srvbot")
RESTARTABLE = ("docker", "fail2ban", "ssh")   # srvbot сам себя не перезапускает
RESTART_WARN = {
    "docker": "контейнеры и их клиенты отключатся примерно на 20 с",
    "ssh": "текущие SSH-сессии не оборвутся",
}


async def unit_states() -> dict[str, str]:
    out = await run("systemctl", "show", "-p", "Id", "-p", "ActiveState", *[f"{u}.service" for u in SERVICES],
                    check=False)
    res, cur = {}, None
    for line in out.splitlines():
        k, _, v = line.partition("=")
        if k == "Id":
            cur = v.removesuffix(".service")
        elif k == "ActiveState" and cur:
            res[cur] = v
    return res


_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,100}$")


def unit_name(unit: str) -> str:
    """Проверяет имя службы systemd и добавляет «.service», если суффикса нет."""
    if not _UNIT_RE.match(unit) or unit.startswith("-"):
        raise CmdError(f"{unit}: недопустимое имя службы")
    return unit if "." in unit else f"{unit}.service"


async def restart_unit(unit: str, any_unit: bool = False) -> None:
    """Перезапуск службы. Из раздела «Службы» — только известные; из алерта «служба упала» — любая,
    кроме самого бота (его перезапуск оборвал бы обработку)."""
    if not any_unit and unit not in RESTARTABLE:
        raise CmdError(f"{unit}: перезапуск из бота не предусмотрен")
    name = unit_name(unit)
    if name.startswith("srvbot.") or name.startswith("srvbot-"):
        raise CmdError("бот не перезапускает сам себя")
    await run("systemctl", "restart", name, timeout=120)


async def unit_logs(unit: str, n: int = 25) -> str:
    return await run("journalctl", "-u", unit_name(unit), "-n", str(n), "--no-pager", "-o", "short", check=False)


async def container_logs(name: str, n: int = 25) -> str:
    try:
        return await run("docker", "logs", "--tail", str(n), name, merge=True)
    except CmdError as e:
        if "does not support reading" in str(e):
            return "журнал у этого контейнера отключён (log driver none в его настройках)"
        raise


# --- очистка диска ---

VSCODE = "/root/.vscode-server"


def _du(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _vscode_junk() -> list[str]:
    """Старые серверы VS Code (без процессов и не последний по lru.json) и кэш VSIX."""
    servers = glob.glob(f"{VSCODE}/cli/servers/Stable-*")
    try:
        with open(f"{VSCODE}/cli/servers/lru.json") as f:
            newest = (json.load(f) or [None])[0]
    except (OSError, ValueError):
        newest = None
    busy = set()
    for p in psutil.process_iter(["cmdline"]):
        cmd = " ".join(p.info["cmdline"] or [])
        busy.update(s for s in servers if s in cmd)
    junk = [s for s in servers if s not in busy and os.path.basename(s) != newest]
    vsix = f"{VSCODE}/data/CachedExtensionVSIXs"
    return junk + ([vsix] if os.path.isdir(vsix) else [])


async def clean_estimate() -> dict[str, int]:
    """Сколько примерно освободится, по пунктам (байты)."""
    est = {"apt": _du("/var/cache/apt/archives")}
    out = await run("journalctl", "--disk-usage", check=False)
    m = re.search(r"([\d.]+)([KMG])", out)
    if m:
        journal = float(m.group(1)) * {"K": 2**10, "M": 2**20, "G": 2**30}[m.group(2)]
        est["journal"] = max(0, int(journal) - 50 * 2**20)
    try:
        df = await run("docker", "system", "df", "--format", "{{json .}}")
        rec = 0
        for line in df.splitlines():
            r = json.loads(line).get("Reclaimable", "")
            m = re.match(r"([\d.]+)\s*([kMG]?B)", r)
            if m:
                rec += int(float(m.group(1)) * {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9}[m.group(2)])
        est["docker"] = rec
    except (CmdError, ValueError):
        pass
    est["vscode"] = sum(_du(p) for p in _vscode_junk())
    return est


async def clean() -> list[str]:
    """Чистит и возвращает журнал действий."""
    done = []
    await run("journalctl", "--vacuum-size=50M", check=False)
    done.append("журналы systemd ужаты до 50 МБ")
    await run(*APT_ENV, "apt-get", "clean", check=False)
    out = await run(*APT_ENV, "apt-get", "-y", "autoremove", "--purge", timeout=300, check=False)
    removed = len(re.findall(r"^Removing ", out, re.M))
    done.append("кэш apt очищен" + (f", удалено ненужных пакетов: {removed}" if removed else ""))
    await run("docker", "image", "prune", "-af", check=False, timeout=120)
    await run("docker", "builder", "prune", "-af", check=False, timeout=120)
    done.append("неиспользуемые образы и кэш сборки docker удалены")
    junk = _vscode_junk()
    for p in junk:
        shutil.rmtree(p, ignore_errors=True)
    if junk:
        done.append(f"старые файлы VS Code удалены: {len(junk)}")
    return done


def disk_free() -> int:
    return shutil.disk_usage("/").free
