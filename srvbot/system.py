import asyncio
import calendar
import glob
import ipaddress
import os
import socket
import re
import time
from dataclasses import dataclass
from datetime import datetime

import psutil

from . import config
from .util import CmdError, run


@dataclass
class Metrics:
    uptime: float
    load: tuple[float, float, float]
    cpu_pct: float
    mem_total: int
    mem_avail: int
    swap_total: int
    swap_used: int
    mem_psi_full: float | None
    disk_total: int
    disk_free: int
    disk_pct: float

    @property
    def mem_avail_pct(self) -> float:
        return self.mem_avail / self.mem_total * 100


def mem_psi_full() -> float | None:
    try:
        with open("/proc/pressure/memory") as f:
            for line in f:
                if line.startswith("full"):
                    return float(re.search(r"avg60=([\d.]+)", line).group(1))
    except (OSError, AttributeError):
        pass
    return None


class CpuMeter:
    """Загрузка CPU между вызовами pct(); у каждого потребителя свой счётчик."""

    def __init__(self):
        self.last = psutil.cpu_times()

    def pct(self) -> float:
        cur = psutil.cpu_times()
        busy = lambda t: sum(t) - t.idle - getattr(t, "iowait", 0)
        total = sum(cur) - sum(self.last)
        res = (busy(cur) - busy(self.last)) / total * 100 if total > 0 else 0.0
        self.last = cur
        return max(0.0, min(100.0, res))


async def cpu_now(sec: float = 1.0) -> float:
    m = CpuMeter()
    await asyncio.sleep(sec)
    return m.pct()


def metrics(cpu_pct: float = 0.0) -> Metrics:
    vm, sw, du = psutil.virtual_memory(), psutil.swap_memory(), psutil.disk_usage("/")
    return Metrics(
        uptime=time.time() - psutil.boot_time(),
        load=os.getloadavg(),
        cpu_pct=cpu_pct,
        mem_total=vm.total, mem_avail=vm.available,
        swap_total=sw.total, swap_used=sw.used,
        mem_psi_full=mem_psi_full(),
        disk_total=du.total, disk_free=du.free, disk_pct=du.percent,
    )


def local_ips() -> set[str]:
    return {a.address.split("%")[0] for addrs in psutil.net_if_addrs().values() for a in addrs
            if a.family in (socket.AF_INET, socket.AF_INET6)}


def public_ipv4() -> list[str]:
    """Внешние IPv4 сервера: глобальные адреса на интерфейсах."""
    return sorted({a.address for addrs in psutil.net_if_addrs().values() for a in addrs
                   if a.family == socket.AF_INET and ipaddress.ip_address(a.address).is_global})


def listening() -> tuple[dict[str, str], dict[str, str]]:
    """Слушающие порты процессов из PORT_PROCESSES.

    Возвращает (рабочие, «мёртвые»): {"tcp/443": "nginx", ...} и {"tcp/443": "203.0.113.5", ...}.
    «Мёртвый» — порт привязан только к IP, которого больше нет на сервере (например, после смены IP).
    Loopback пропускается: там служебные порты, которые меняются при перезапусках.
    """
    ok, stale = {}, {}
    mine = local_ips()
    for c in psutil.net_connections(kind="inet"):
        is_tcp = c.type == socket.SOCK_STREAM
        if is_tcp and c.status != psutil.CONN_LISTEN:
            continue
        if not is_tcp and c.raddr:
            continue
        ip = ipaddress.ip_address(c.laddr.ip.split("%")[0])
        if not c.pid or ip.is_loopback:
            continue
        try:
            name = psutil.Process(c.pid).name()
        except psutil.Error:
            continue
        if name not in config.PORT_PROCESSES:
            continue
        key = f"{'tcp' if is_tcp else 'udp'}/{c.laddr.port}"
        if ip.is_unspecified or str(ip) in mine:
            ok[key] = name
        else:
            stale[key] = str(ip)
    return ok, {k: v for k, v in stale.items() if k not in ok}


async def failed_units() -> list[str]:
    out = await run("systemctl", "list-units", "--state=failed", "--no-legend", "--plain", check=False)
    return [line.split()[0] for line in out.splitlines() if line.strip()]


@dataclass
class Cert:
    name: str
    expires: float

    @property
    def days_left(self) -> int:
        return int((self.expires - time.time()) // 86400)


async def certs() -> list[Cert]:
    res = []
    for p in sorted(glob.glob(os.path.join(config.LETSENCRYPT_LIVE, "*", "fullchain.pem"))):
        try:
            out = await run("openssl", "x509", "-enddate", "-noout", "-in", p)
            dt = datetime.strptime(out.strip().split("=", 1)[1], "%b %d %H:%M:%S %Y %Z")
            res.append(Cert(os.path.basename(os.path.dirname(p)), calendar.timegm(dt.timetuple())))
        except (CmdError, ValueError, IndexError):
            continue
    return res


@dataclass
class Jail:
    name: str
    banned_now: list[str]
    total_banned: int
    total_failed: int


async def jails() -> list[Jail]:
    try:
        out = await run("fail2ban-client", "status")
    except CmdError:
        return []
    m = re.search(r"Jail list:\s*(.*)", out)
    names = [j.strip() for j in m.group(1).split(",") if j.strip()] if m else []
    res = []
    for n in names:
        s = await run("fail2ban-client", "status", n, check=False)
        def num(label):
            m = re.search(label + r":\s*(\d+)", s)
            return int(m.group(1)) if m else 0
        ips = re.search(r"Banned IP list:\s*(.*)", s)
        res.append(Jail(n, ips.group(1).split() if ips else [], num("Total banned"), num("Total failed")))
    return res


async def unban(jail: str, ip: str) -> None:
    await run("fail2ban-client", "set", jail, "unbanip", ip)


def updates() -> tuple[int, int]:
    """(всего, безопасности) доступных обновлений.

    Читается из файла, который apt сам обновляет после `apt update`/dpkg.
    apt-check не запускаем: он съедает ~170 МБ RAM.
    """
    try:
        with open("/var/lib/update-notifier/updates-available") as f:
            text = f.read()
    except OSError:
        return (0, 0)
    total = re.search(r"(\d+) updates? can be applied immediately", text)
    sec = re.search(r"(\d+) of these updates (?:is|are) (?:a )?standard security updates?", text)
    return (int(total.group(1)) if total else 0, int(sec.group(1)) if sec else 0)


def reboot_required() -> bool:
    return os.path.exists("/var/run/reboot-required")


async def reboot() -> None:
    await run("systemctl", "reboot")

