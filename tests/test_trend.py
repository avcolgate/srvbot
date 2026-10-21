"""Тесты полосок заполнения и почасовой истории CPU/RAM (trend)."""
import unittest

from srvbot import trend

H = trend.HOUR
NOW = 1_700_000_000 // H * H + 600


class Trend(unittest.TestCase):
    def test_bar_fill_and_color(self):
        self.assertEqual(trend.bar(32), "🟩🟩🟩⬜⬜⬜⬜⬜")
        self.assertEqual(trend.bar(0), "⬜" * 8)
        self.assertEqual(trend.bar(78), "🟨" * 6 + "⬜" * 2)
        self.assertEqual(trend.bar(120), "🟥" * 8)

    def test_add_sample_sums_per_hour_and_prunes(self):
        hist = trend.add_sample(None, NOW, 10, 50)
        hist = trend.add_sample(hist, NOW + 60, 30, 70)
        self.assertEqual(hist, [[NOW // H * H, 40, 120, 2]])
        hist = trend.add_sample(hist, NOW + 25 * H, 0, 0)
        self.assertEqual(len(hist), 2)  # месяц храним
        hist = trend.add_sample(hist, NOW + 60 * 24 * H, 0, 0)
        self.assertEqual(len(hist), 1)
