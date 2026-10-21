"""Тесты накопительного учёта трафика (report.account)."""
import unittest

from srvbot import report, tunnel


def inst(name: str, peers: dict[str, tuple[int, int]], error: str = "") -> tunnel.Instance:
    i = tunnel.Instance(container=name, error=error)
    i.interfaces = [tunnel.Interface("tun0", 1, [tunnel.Peer(k, None, "10.0.0.5/32", 0, rx, tx)
                                                 for k, (rx, tx) in peers.items()])]
    return i


class Ledger(unittest.TestCase):
    T = 1_700_000_000 // 3600 * 3600  # начало часа

    def test_first_sight_counts_current_counters(self):
        led = report.account(None, [inst("c", {"A": (100, 200)})], self.T)
        self.assertEqual(led["total"], [100, 200])
        self.assertEqual(led["peers"]["c/A"]["hours"], [[self.T, 100, 200]])

    def test_tunnel_restart_keeps_total(self):
        led = report.account(None, [inst("c", {"A": (100, 200)})], self.T)
        led = report.account(led, [inst("c", {"A": (150, 300)})], self.T + 60)
        led = report.account(led, [inst("c", {"A": (10, 20)})], self.T + 120)  # счётчики обнулились
        self.assertEqual(led["peers"]["c/A"]["total"], [160, 320])
        self.assertEqual(led["total"], [160, 320])

    def test_last_day_window(self):
        led = report.account(None, [inst("c", {"A": (100, 0)})], self.T)
        led = report.account(led, [inst("c", {"A": (130, 0)})], self.T + 3600)
        rec = led["peers"]["c/A"]
        self.assertEqual(report.last_day(rec, self.T + 3600 + 10), (130, 0))
        self.assertEqual(report.last_day(rec, self.T + 24 * 3600), (30, 0))  # первый час уже вне окна
        self.assertEqual(rec["total"], [130, 0])

    def test_old_hours_pruned(self):
        led = report.account(None, [inst("c", {"A": (100, 0)})], self.T)
        led = report.account(led, [inst("c", {"A": (110, 0)})], self.T + report.KEEP_HOURS * 3600)
        self.assertEqual([h[0] for h in led["peers"]["c/A"]["hours"]], [self.T + report.KEEP_HOURS * 3600])

    def test_removed_client_forgotten_only_when_complete(self):
        led = report.account(None, [inst("c", {"A": (100, 0), "B": (5, 0)})], self.T)
        led = report.account(led, [inst("c", {"A": (100, 0)})], self.T + 60)
        self.assertIn("c/B", led["peers"])
        led = report.account(led, [inst("c", {"A": (100, 0)})], self.T + 120, complete=True)
        self.assertNotIn("c/B", led["peers"])
        self.assertEqual(led["total"], [105, 0])  # общий итог помнит и удалённых

    def test_unreadable_instance_is_skipped(self):
        led = report.account(None, [inst("c", {"A": (100, 0)})], self.T)
        led = report.account(led, [inst("c", {"A": (500, 0)}, error="нет данных")], self.T + 60)
        self.assertEqual(led["total"], [100, 0])

    def test_total_since_by_container(self):
        led = report.account(None, [inst("c", {"A": (1, 2)}), inst("d", {"A": (10, 20)})], self.T)
        self.assertEqual(report.total_since(led, self.T), (11, 22))
        self.assertEqual(report.total_since(led, self.T, "d/"), (10, 20))


if __name__ == "__main__":
    unittest.main()
