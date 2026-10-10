"""Тесты трафика за сегодня (report.day_traffic)."""
import unittest

from srvbot import report, tunnel


def inst(name: str, peers: dict[str, tuple[int, int]], error: str = "") -> tunnel.Instance:
    i = tunnel.Instance(container=name, error=error)
    i.interfaces = [tunnel.Interface("tun0", 1, [tunnel.Peer(k, None, "10.0.0.5/32", 0, rx, tx)
                                                 for k, (rx, tx) in peers.items()])]
    return i


class DayTraffic(unittest.TestCase):
    def test_new_day_starts_from_current_counters(self):
        day, rx, tx = report.day_traffic(None, [inst("c", {"A": (100, 200)})], "2026-01-02")
        self.assertEqual((day["date"], day["base"], rx, tx), ("2026-01-02", {"c/A": [100, 200]}, 0, 0))

    def test_date_change_resets(self):
        old = {"date": "2026-01-01", "base": {"c/A": [10, 20]}}
        day, rx, tx = report.day_traffic(old, [inst("c", {"A": (100, 200)})], "2026-01-02")
        self.assertEqual((day["date"], rx, tx), ("2026-01-02", 0, 0))

    def test_growth_within_day(self):
        old = {"date": "2026-01-02", "base": {"c/A": [10, 20], "c/B": [5, 5]}}
        day, rx, tx = report.day_traffic(old, [inst("c", {"A": (100, 220), "B": (5, 6)})], "2026-01-02")
        self.assertIs(day, old)
        self.assertEqual((rx, tx), (90, 201))

    def test_counter_reset_and_new_client(self):
        old = {"date": "2026-01-02", "base": {"c/A": [1000, 1000]}}
        _, rx, tx = report.day_traffic(old, [inst("c", {"A": (30, 40), "N": (7, 8)})], "2026-01-02")
        self.assertEqual((rx, tx), (37, 48))  # рестарт счётчика — с нуля; новый клиент — целиком

    def test_unreadable_instance_is_skipped(self):
        old = {"date": "2026-01-02", "base": {"c/A": [10, 20]}}
        _, rx, tx = report.day_traffic(old, [inst("c", {"A": (50, 60)}, error="нет данных")], "2026-01-02")
        self.assertEqual((rx, tx), (0, 0))


if __name__ == "__main__":
    unittest.main()
