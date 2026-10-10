"""Клиенты туннеля в docker-контейнере, без привязки к версии.

Ничего не хардкодится: контейнеры ищутся по подстроке имени/образа, утилита
и интерфейсы определяются внутри контейнера, статистика берётся из машинного
формата `show all dump`, имена клиентов — из JSON-списка клиентов (TUNNEL_CLIENTS), если он найдётся.
"""
import ipaddress
import json
from dataclasses import dataclass, field

from . import config, docker
from .util import CmdError, run


class TunnelError(Exception):
    pass


@dataclass
class Peer:
    pubkey: str
    endpoint: str | None
    allowed_ips: str
    handshake: int          # unix time, 0 — не было
    rx: int                 # байт получено сервером (upload клиента)
    tx: int                 # байт отправлено сервером (download клиента)
    name: str = ""
    created: str = ""       # дата создания из списка клиентов, как есть

    @property
    def label(self) -> str:
        return self.name or self.pubkey[:8] + "…"

    @property
    def endpoint_ip(self) -> str | None:
        """Внешний IP, с которого клиент подключался последний раз (без порта)."""
        if not self.endpoint:
            return None
        host = self.endpoint.rsplit(":", 1)[0]
        return host.strip("[]")

    @property
    def ip(self) -> str:
        """Внутренний адрес клиента: «10.0.0.5» (маски /32 и /128 убираются)."""
        ips = [a.strip() for a in self.allowed_ips.split(",") if a.strip() not in ("", "(none)")]
        return ", ".join(a.removesuffix("/32").removesuffix("/128") for a in ips) or "—"


@dataclass
class Interface:
    name: str
    listen_port: int | None
    peers: list[Peer] = field(default_factory=list)


@dataclass
class Instance:
    container: str
    image: str = ""
    tool: str = ""
    version: str = ""
    ports: list[str] = field(default_factory=list)
    interfaces: list[Interface] = field(default_factory=list)
    names_source: str = ""  # путь к списку клиентов или "", если имён нет
    running: bool = True
    started_at: float = 0   # unix time запуска контейнера (с него считается трафик)
    error: str = ""

    @property
    def peers(self) -> list[Peer]:
        return [p for i in self.interfaces for p in i.peers]


def _int(s: str) -> int:
    try:
        return int(s)
    except ValueError:
        return 0


def parse_dump(text: str) -> list[Interface]:
    """Парсит `<утилита> show all dump`.

    Для каждого интерфейса первая строка — сам интерфейс
    (iface, private-key, public-key, listen-port, fwmark, [доп. параметры…]),
    остальные — пиры (iface, public-key, psk, endpoint, allowed-ips, handshake, rx, tx, keepalive, […]).
    Хвостовые поля, которые могут добавить будущие версии, игнорируются.
    """
    ifaces: dict[str, Interface] = {}
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 5:
            continue
        name = f[0]
        if name not in ifaces:
            ifaces[name] = Interface(name=name, listen_port=_int(f[3]) or None)
            continue
        if len(f) < 8:
            raise TunnelError(f"неожиданный формат строки пира ({len(f)} полей)")
        ifaces[name].peers.append(Peer(
            pubkey=f[1],
            endpoint=None if f[3] in ("(none)", "") else f[3],
            allowed_ips=f[4],
            handshake=_int(f[5]),
            rx=_int(f[6]),
            tx=_int(f[7]),
        ))
    return list(ifaces.values())


def _clients(text: str):
    """(pubkey, данные клиента) из JSON-списка клиентов. Терпим к разным вариантам формата."""
    data = json.loads(text)
    if isinstance(data, dict):  # вариант {pubkey: {...}}
        items = [{"clientId": k, **v} for k, v in data.items() if isinstance(v, dict)]
    else:
        items = data
    for it in items:
        if not isinstance(it, dict):
            continue
        key = it.get("clientId") or it.get("publicKey") or it.get("id")
        ud = it.get("userData") if isinstance(it.get("userData"), dict) else it
        if key:
            yield key, ud


def parse_clients_table(text: str) -> dict[str, str]:
    """pubkey -> имя клиента."""
    return {k: str(n) for k, ud in _clients(text) if (n := ud.get("clientName") or ud.get("name"))}


def parse_clients_created(text: str) -> dict[str, str]:
    """pubkey -> дата создания (userData.creationDate), если она есть."""
    return {k: str(ud["creationDate"]) for k, ud in _clients(text) if ud.get("creationDate")}


_PROBE = r"""
TOOLS="{tools}"
if [ -z "$TOOLS" ]; then
    # утилита обычно называется как интерфейс без номера: xyz0 -> xyz
    for i in $(ls /sys/class/net 2>/dev/null); do TOOLS="$TOOLS ${{i%%[0-9]*}}"; done
fi
T=""
for t in $TOOLS; do
    command -v "$t" >/dev/null 2>&1 && "$t" show all dump >/dev/null 2>&1 && {{ T="$t"; break; }}
done
[ -n "$T" ] || {{ echo "@@NONE"; exit 0; }}
echo "@@TOOL $T"
echo "@@VERSION"; "$T" --version 2>&1 | head -n1
echo "@@DUMP"; "$T" show all dump 2>&1
FILES="{globs}"
if [ -z "$FILES" ]; then
    # список клиентов — файл, в котором встречается ключ первого клиента
    KEY=$("$T" show all dump 2>/dev/null | awk '$5 ~ /\// {{ print $2; exit }}')
    [ -n "$KEY" ] && FILES=$(find /opt /etc /var/lib /root -type f -size -1024k 2>/dev/null \
        | xargs -r grep -lF -- "$KEY" 2>/dev/null | head -n 5)
fi
for f in $FILES; do [ -f "$f" ] && {{ echo "@@CLIENTS $f"; cat "$f"; echo; }}; done
echo "@@END"
"""


class TunnelNotFound(TunnelError):
    """В контейнере нет утилиты туннеля."""


def parse_probe(out: str, inst: Instance) -> Instance:
    sections: list[tuple[str, list[str]]] = []
    for line in out.splitlines():
        if line.startswith("@@"):
            sections.append((line[2:], []))
        elif sections:
            sections[-1][1].append(line)
    names: dict[str, str] = {}
    created: dict[str, str] = {}
    for head, body in sections:
        kind, _, arg = head.partition(" ")
        if kind == "NONE":
            raise TunnelNotFound("утилита туннеля не найдена")
        if kind == "ERR":
            raise TunnelError(arg)
        if kind == "TOOL":
            inst.tool = arg.rsplit("/", 1)[-1]
        elif kind == "VERSION":
            inst.version = (body[0] if body else "").split(" - ")[0].strip()
        elif kind == "DUMP":
            inst.interfaces = parse_dump("\n".join(body))
        elif kind == "CLIENTS" and not names:
            try:
                names = parse_clients_table("\n".join(body))
                created = parse_clients_created("\n".join(body))
                inst.names_source = arg
            except (ValueError, AttributeError):
                pass
    if not inst.tool:
        raise TunnelError("пустой ответ от контейнера")
    for p in inst.peers:
        p.name = names.get(p.pubkey, "")
        p.created = created.get(p.pubkey, "")
    return inst


def sort_by_ip(peers: list[Peer]) -> list[Peer]:
    """По внутреннему IP: 10.0.0.1, 10.0.0.2, … 10.0.0.10 (числами, не строками)."""
    def key(p: Peer):
        first = p.allowed_ips.split(",")[0].strip()
        try:
            addr = ipaddress.ip_network(first, strict=False).network_address
            return (0, addr.version, int(addr))
        except ValueError:
            return (1, 0, 0)  # без адреса — в конец
    return sorted(peers, key=key)


# Эмодзи клиентов: без вариационных селекторов и без цветных кружков (они — статусы)
EMOJI = ("🦊", "🐼", "🦉", "🐯", "🦁", "🐨", "🐰", "🐻", "🐧", "🦄", "🦋", "🐢", "🐬", "🐳", "🦒",
         "🦓", "🐘", "🦔", "🦦", "🦥", "🦩", "🦜", "🐱", "🐶", "🐹", "🐤", "🦭", "🦘", "🐝", "🐞",
         "🍎", "🍊", "🍋", "🍉", "🍇", "🍓", "🍒", "🥝", "🍍", "🥑", "🍄", "🌵", "🌻", "🌈", "⭐",
         "🍩", "🍪", "🎈", "🎲", "🚀")


def assign_emoji(mapping: dict[str, str], keys: list[str], forget_missing: bool = True) -> bool:
    """Выдаёт новым ключам первый свободный эмодзи (в порядке keys), забывает удалённые ключи.
    mapping меняется на месте; возвращает True, если что-то изменилось."""
    changed = False
    for k in [k for k, e in mapping.items() if e not in EMOJI]:  # эмодзи убран из набора — выдадим новый
        del mapping[k]
        changed = True
    if forget_missing:
        for k in [k for k in mapping if k not in keys]:
            del mapping[k]
            changed = True
    used = set(mapping.values())
    for k in keys:
        if k in mapping:
            continue
        free = [e for e in EMOJI if e not in used]
        mapping[k] = free[0] if free else EMOJI[sum(map(ord, k)) % len(EMOJI)]
        used.add(mapping[k])
        changed = True
    return changed


def new_peers(known: list[str], inst: Instance) -> list[Peer]:
    """Пиры инстанса, которых нет в known (списке ранее виденных ключей)."""
    seen = set(known)
    return [p for p in inst.peers if p.pubkey not in seen]


def is_tunnel(c: docker.Container) -> bool:
    s = f"{c.name} {c.image}".lower()
    return any(m in s for m in config.TUNNEL_MATCH)


# Автоопределение: контейнеры без туннеля не перепроверяем до их перезапуска,
# найденные запоминаем (и путь к списку клиентов — чтобы не искать его каждый раз).
_not_tunnel: dict[str, str] = {}     # контейнер -> started_at, когда туннеля там не оказалось
_known: dict[str, str] = {}          # контейнер с туннелем -> путь к списку клиентов ("" — не найден)


def found() -> bool:
    """Есть ли на сервере туннель (заданный в настройках или уже найденный)."""
    return bool(config.TUNNEL_MATCH or _known)


async def collect() -> list[Instance]:
    """Все контейнеры туннеля на сервере. Ошибки — в Instance.error, исключений нет."""
    explicit = bool(config.TUNNEL_MATCH)
    res = []
    for c in await docker.containers():
        if explicit:
            if not is_tunnel(c):
                continue
        elif c.name not in _known and (not c.running or _not_tunnel.get(c.name) == c.started_at):
            continue
        inst = Instance(container=c.name, image=c.image, ports=c.ports, running=c.running,
                        started_at=c.started_ts)
        if not c.running:
            inst.error = f"контейнер не запущен ({c.status})"
            res.append(inst)
            continue
        globs = config.TUNNEL_CLIENTS or tuple(filter(None, [_known.get(c.name, "")]))
        script = _PROBE.format(tools=" ".join(config.TUNNEL_TOOLS), globs=" ".join(globs))
        try:
            parse_probe(await run("docker", "exec", c.name, "sh", "-c", script), inst)
        except (CmdError, TunnelError) as e:
            if not explicit and c.name not in _known:
                _not_tunnel[c.name] = c.started_at  # не туннель (или нет даже sh) — до перезапуска не трогаем
                continue
            inst.error = str(e)
        if not inst.error and not explicit:
            _known[c.name] = inst.names_source
        res.append(inst)
    return res
