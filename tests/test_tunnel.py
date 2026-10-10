"""Тесты разбора данных туннеля. Запуск: cd /opt/srvbot && python3 -m unittest -v

После обновления ПО туннеля можно сохранить реальный вывод
`docker exec <контейнер> <утилита> show all dump` и добавить сюда проверку.
"""
import unittest

from unittest import mock

from srvbot import config, tunnel

T = "\t"
# Строка интерфейса: 4 значимых поля + произвольное число дополнительных (парсер их пропускает)
def iface_line(name: str, port: int, extra: int) -> str:
    return T.join([name, "PRIV=", "SRVPUB=", str(port)] + [f"x{i}" for i in range(extra)])


IFACE_EXT = iface_line("tun0", 40000, 17)  # 21 поле
PEER_A = T.join(["tun0", "KEYA=", "PSK=", "1.2.3.4:1500", "10.0.0.5/32", "1700000000", "1000000", "2000000", "off"])
PEER_B = T.join(["tun0", "KEYB=", "PSK=", "(none)", "10.0.0.6/32", "0", "0", "0", "off"])
CLIENTS = '[{"clientId": "KEYA=", "userData": {"clientName": "laptop", "allowedIps": "10.0.0.5/32"}}]'


class ParseDump(unittest.TestCase):
    def test_iface_21_fields(self):
        (iface,) = tunnel.parse_dump("\n".join([IFACE_EXT, PEER_A, PEER_B]))
        self.assertEqual((iface.name, iface.listen_port, len(iface.peers)), ("tun0", 40000, 2))
        a, b = iface.peers
        self.assertEqual((a.pubkey, a.endpoint, a.handshake, a.rx, a.tx),
                         ("KEYA=", "1.2.3.4:1500", 1700000000, 1000000, 2000000))
        self.assertIsNone(b.endpoint)
        self.assertEqual(b.handshake, 0)

    def test_iface_30_fields(self):
        # Более новая версия утилиты: у интерфейса 30 полей
        (i,) = tunnel.parse_dump("\n".join([iface_line("tun0", 40001, 26), PEER_A, PEER_B]))
        self.assertEqual((i.listen_port, [p.pubkey for p in i.peers]), (40001, ["KEYA=", "KEYB="]))

    def test_iface_5_fields(self):
        (iface,) = tunnel.parse_dump(iface_line("tun9", 12345, 1) + "\n" + PEER_A.replace("tun0", "tun9"))
        self.assertEqual((iface.listen_port, len(iface.peers)), (12345, 1))

    def test_future_extra_fields_ignored(self):
        peer = PEER_A + T + "newfield" + T + "42"
        (iface,) = tunnel.parse_dump(IFACE_EXT + "\n" + peer)
        self.assertEqual(iface.peers[0].tx, 2000000)

    def test_several_interfaces(self):
        second = [IFACE_EXT.replace("tun0", "tun1"), PEER_B.replace("tun0", "tun1")]
        ifaces = tunnel.parse_dump("\n".join([IFACE_EXT, PEER_A] + second))
        self.assertEqual([(i.name, len(i.peers)) for i in ifaces], [("tun0", 1), ("tun1", 1)])

    def test_broken_peer_line(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.parse_dump(IFACE_EXT + "\n" + T.join(["tun0", "KEY", "PSK", "x", "y"]))


class ParseClients(unittest.TestCase):
    def test_list_format(self):
        self.assertEqual(tunnel.parse_clients_table(CLIENTS), {"KEYA=": "laptop"})

    def test_dict_format(self):
        self.assertEqual(tunnel.parse_clients_table('{"KEYA=": {"clientName": "phone"}}'), {"KEYA=": "phone"})

    def test_readme_formats(self):  # форматы из README и srvbot.env.example
        self.assertEqual(tunnel.parse_clients_table('[{"publicKey": "K1=", "name": "laptop"}]'), {"K1=": "laptop"})
        self.assertEqual(tunnel.parse_clients_table('{"K2=": {"name": "phone"}}'), {"K2=": "phone"})

    def test_unknown_format_gives_empty(self):
        self.assertEqual(tunnel.parse_clients_table('[{"foo": 1}, "str"]'), {})


class ParseProbe(unittest.TestCase):
    def probe(self, *, clients=CLIENTS):
        parts = ["@@TOOL /usr/bin/tool", "@@VERSION", "tool v1.2.3 - https://example.org",
                 "@@DUMP", IFACE_EXT, PEER_A, PEER_B]
        if clients is not None:
            parts += ["@@CLIENTS /opt/app/clients.json", clients]
        return "\n".join(parts + ["@@END"])

    def test_full(self):
        inst = tunnel.parse_probe(self.probe(), tunnel.Instance(container="tun-a"))
        self.assertEqual((inst.tool, inst.version), ("tool", "tool v1.2.3"))
        self.assertEqual(inst.names_source, "/opt/app/clients.json")
        self.assertEqual([p.label for p in inst.peers], ["laptop", "KEYB=…"])

    def test_no_clients_table(self):
        inst = tunnel.parse_probe(self.probe(clients=None), tunnel.Instance(container="x"))
        self.assertEqual(inst.names_source, "")
        self.assertEqual(inst.peers[0].label, "KEYA=…")

    def test_broken_clients_table(self):
        inst = tunnel.parse_probe(self.probe(clients="{not json"), tunnel.Instance(container="x"))
        self.assertEqual((inst.names_source, len(inst.peers)), ("", 2))

    def test_no_tool(self):
        with self.assertRaises(tunnel.TunnelError):
            tunnel.parse_probe("@@ERR утилита tool не найдена", tunnel.Instance(container="x"))


class PeerHelpers(unittest.TestCase):
    def test_ip(self):
        mk = lambda ips: tunnel.Peer("K", None, ips, 0, 0, 0)
        self.assertEqual(mk("10.0.0.5/32").ip, "10.0.0.5")
        self.assertEqual(mk("10.0.0.5/32, fd00::5/128").ip, "10.0.0.5, fd00::5")
        self.assertEqual(mk("10.0.2.0/24").ip, "10.0.2.0/24")
        self.assertEqual(mk("(none)").ip, "—")

    def test_new_peers(self):
        inst = tunnel.parse_probe(ParseProbe().probe(), tunnel.Instance(container="x"))
        self.assertEqual([p.pubkey for p in tunnel.new_peers(["KEYA="], inst)], ["KEYB="])
        self.assertEqual(tunnel.new_peers(["KEYA=", "KEYB="], inst), [])


class Emoji(unittest.TestCase):
    def test_assign_in_order_and_stable(self):
        m = {}
        self.assertTrue(tunnel.assign_emoji(m, ["A", "B"]))
        self.assertEqual(m, {"A": tunnel.EMOJI[0], "B": tunnel.EMOJI[1]})
        self.assertFalse(tunnel.assign_emoji(m, ["A", "B"]))  # повторно ничего не меняется

    def test_removed_frees_emoji_new_gets_free_one(self):
        m = {"A": tunnel.EMOJI[0], "B": tunnel.EMOJI[1]}
        tunnel.assign_emoji(m, ["B", "C"])
        self.assertEqual(m, {"B": tunnel.EMOJI[1], "C": tunnel.EMOJI[0]})

    def test_keep_missing_when_asked(self):
        m = {"A": tunnel.EMOJI[0]}
        tunnel.assign_emoji(m, ["B"], forget_missing=False)
        self.assertEqual(m, {"A": tunnel.EMOJI[0], "B": tunnel.EMOJI[1]})

    def test_emoji_removed_from_palette_is_replaced(self):
        m = {"A": "🐙", "B": tunnel.EMOJI[0]}
        tunnel.assign_emoji(m, ["A", "B"])
        self.assertEqual(m, {"A": tunnel.EMOJI[1], "B": tunnel.EMOJI[0]})

    def test_unique(self):
        m = {}
        tunnel.assign_emoji(m, [f"K{i}" for i in range(len(tunnel.EMOJI))])
        self.assertEqual(len(set(m.values())), len(tunnel.EMOJI))

    def test_sort_by_ip_numeric(self):
        mk = lambda ip: tunnel.Peer(ip, None, ip, 0, 0, 0)
        peers = [mk("10.0.0.10/32"), mk("10.0.0.2/32"), mk("(none)"), mk("10.0.0.9/32")]
        self.assertEqual([p.pubkey for p in tunnel.sort_by_ip(peers)],
                         ["10.0.0.2/32", "10.0.0.9/32", "10.0.0.10/32", "(none)"])


class Detect(unittest.TestCase):
    def test_container_matching(self):
        from srvbot.docker import Container
        mk = lambda n, i: Container(n, i, "running", "always", "")
        with mock.patch.object(config, "TUNNEL_MATCH", ("tun-",)):
            self.assertTrue(tunnel.is_tunnel(mk("tun-v2", "tun-v2")))
            self.assertTrue(tunnel.is_tunnel(mk("box", "example/tun-go:latest")))
            self.assertFalse(tunnel.is_tunnel(mk("web-1", "nginx:latest")))

class BlockingSigns(unittest.TestCase):
    """Признаки блокировки: пакеты без рукопожатия и массовое исчезновение клиентов."""
    NOW = 1_700_000_000.0

    def peer(self, handshake: int, rx: int) -> tunnel.Peer:
        return tunnel.Peer("K", "1.2.3.4:1500", "10.0.0.5/32", handshake, rx, 0)

    def test_packets_without_handshake(self):
        self.assertTrue(tunnel.stalled(self.peer(0, 2000), prev_rx=1000, now=self.NOW))
        old = int(self.NOW) - config.TUNNEL_ONLINE_SEC - 10
        self.assertTrue(tunnel.stalled(self.peer(old, 2000), prev_rx=1000, now=self.NOW))

    def test_fresh_handshake_is_fine(self):
        self.assertFalse(tunnel.stalled(self.peer(int(self.NOW) - 30, 2000), prev_rx=1000, now=self.NOW))

    def test_no_new_packets_is_not_stall(self):
        self.assertFalse(tunnel.stalled(self.peer(0, 1000), prev_rx=1000, now=self.NOW))

    def test_first_check_has_no_baseline(self):
        self.assertFalse(tunnel.stalled(self.peer(0, 2000), prev_rx=None, now=self.NOW))

    def test_mass_drop(self):
        self.assertTrue(tunnel.mass_drop(2, 0))
        self.assertFalse(tunnel.mass_drop(1, 0))
        self.assertFalse(tunnel.mass_drop(2, 1))
        self.assertFalse(tunnel.mass_drop(0, 0))


if __name__ == "__main__":
    unittest.main()
