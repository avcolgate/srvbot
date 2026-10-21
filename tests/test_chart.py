"""Картинка «Сервер за сутки» рисуется и без данных, и с ними (если Pillow установлен)."""
import time
import unittest

from srvbot import chart, report, trend, tunnel

try:
    import PIL  # noqa: F401
except ImportError:
    PIL = None


@unittest.skipIf(PIL is None, "нет Pillow")
class Chart(unittest.TestCase):
    def test_renders_jpeg(self):
        now = time.time()
        hist = None
        for i in range(30):
            hist = trend.add_sample(hist, now - i * 3600, 5 + i % 7, 40 + i % 5)
        i = tunnel.Instance(container="c")
        i.interfaces = [tunnel.Interface("t", 1, [tunnel.Peer("K", None, "10.0.0.2/32", 0, 5 * 2**20, 2**30)])]
        led = report.account(None, [i], now)
        for metrics, series, days in ((None, None, 1), (hist, led["series"], 7), (hist, led["series"], 30)):
            jpg = chart.render(metrics, series, now, 12, 55, days)
            self.assertEqual(jpg[:2], b"\xff\xd8")

    def test_buckets_cover_axis(self):
        now = 1_700_000_000.0
        for days, (_, line, bar, _) in chart.PERIODS.items():
            end = chart.axis_end(now, days)
            self.assertGreaterEqual(end, now)
            cells = chart.buckets([(now - 3600, 10, 1)], end - days * 86400, end, line, mean=True)
            self.assertEqual(len(cells), days * 86400 // line)
            self.assertEqual(sum(v is not None for _, v in cells), 1)
            self.assertTrue(chart.ticks(end, days))

    def test_tiny_bar_at_hour_start(self):
        now = time.time()
        h = now // 3600 * 3600
        cases = [[[h, 1, 0], [h - 3600, 10 * 2**30, 0]],                         # 1 байт против 10 ГБ
                 [[h - 3600, 221772627, 1428644970], [h, 20477055, 91583436]]]   # упало на живых данных
        for series in cases:
            for days in chart.PERIODS:
                self.assertEqual(chart.render(None, series, now, 1, 1, days)[:2], b"\xff\xd8")

    def test_scales(self):
        self.assertEqual(chart._nice(23), 25)
        self.assertEqual(chart._byte_top(1.5 * 2**30), 2 * 2**30)
