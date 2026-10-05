import json
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


async def containers() -> list[Container]:
    ids = (await run("docker", "ps", "-aq")).split()
    if not ids:
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
    return sorted(res, key=lambda c: c.name)


async def restart(name: str) -> None:
    await run("docker", "restart", name, timeout=120)
