import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import config
from .util import run


@dataclass
class Container:
    name: str
    image: str
    status: str                  # running / exited / restarting / ...
    restart_policy: str
    started_at: str
    ports: list[str] = field(default_factory=list)   # ["8080/tcp", ...] опубликованные на хост

    @property
    def watched(self) -> bool:
        return self.restart_policy in config.WATCH_RESTART_POLICIES

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def started_ts(self) -> float:
        try:  # "2026-09-27T12:04:05.123456789Z" — наносекунды отбрасываем
            return datetime.fromisoformat(self.started_at[:19]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return 0


_last: tuple[float, list[Container]] | None = None   # последний удачный опрос: (когда, контейнеры)


def cached() -> tuple[float, list[Container]] | None:
    """Что docker отвечал в прошлый раз — чтобы показать хоть что-то, пока он не отвечает."""
    return _last


async def containers() -> list[Container]:
    global _last
    ids = (await run("docker", "ps", "-aq")).split()
    if not ids:
        _last = (time.time(), [])
        return []
    data = json.loads(await run("docker", "inspect", *ids))
    res = []
    for c in data:
        ports = [p for p, binds in (c.get("NetworkSettings", {}).get("Ports") or {}).items() if binds]
        res.append(Container(
            name=c["Name"].lstrip("/"),
            image=c.get("Config", {}).get("Image", ""),
            status=c.get("State", {}).get("Status", "?"),
            restart_policy=(c.get("HostConfig", {}).get("RestartPolicy") or {}).get("Name", ""),
            started_at=c.get("State", {}).get("StartedAt", ""),
            ports=sorted(ports),
        ))
    res.sort(key=lambda c: c.name)
    _last = (time.time(), res)
    return res


async def restart(name: str) -> None:
    await run("docker", "restart", name, timeout=120)
