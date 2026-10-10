"""Тесты разбора ответов check-host.net, решения «доступность по регионам» и группировки по странам."""
import unittest

from srvbot import checkhost

TGT = ["fr1.node.check-host.net", "fr2.node.check-host.net", "fr3.node.check-host.net"]
CTL = ["de1.node.check-host.net", "nl1.node.check-host.net"]
OK = [{"address": "203.0.113.10", "time": 0.04}]
ERR = [{"error": "Connection timed out"}]


def res(tgt, ctl):
    return dict(zip(TGT, tgt)) | dict(zip(CTL, ctl))


class ParseTcp(unittest.TestCase):
    def test_formats(self):
        parsed = checkhost.parse_tcp({"a": OK, "b": ERR, "c": None, "d": [None], "e": "weird"})
        self.assertEqual(parsed, {"a": True, "b": False, "c": None, "d": None, "e": False})


PING_OK = [[["OK", 0.05, "203.0.113.10"], ["OK", 0.05], ["TIMEOUT", 3.0], ["OK", 0.05]]]
PING_BAD = [[["TIMEOUT", 3.0], ["TIMEOUT", 3.0], ["TIMEOUT", 3.0], ["TIMEOUT", 3.0]]]


class ParsePing(unittest.TestCase):
    def test_formats(self):
        parsed = checkhost.parse_ping({"a": PING_OK, "b": PING_BAD, "c": None, "d": [None], "e": [[]],
                                       "f": [{"message": "no such node"}]})
        self.assertEqual(parsed, {"a": True, "b": False, "c": None, "d": None, "e": None, "f": False})


class Combine(unittest.TestCase):
    def test_either_method_counts_as_reachable(self):
        tcp = {"a": True, "b": False, "c": False, "d": None}
        ping = {"a": False, "b": True, "c": False, "d": True}
        self.assertEqual(checkhost.combine(tcp, ping), {"a": True, "b": True, "c": False, "d": True})

    def test_without_ping(self):
        self.assertEqual(checkhost.combine({"a": True, "b": False, "c": None}, None),
                         {"a": True, "b": False, "c": None})


class Verdict(unittest.TestCase):
    def v(self, tgt, ctl):
        return checkhost.verdict(checkhost.parse_tcp(res(tgt, ctl)), TGT, CTL)

    def test_all_ok(self):
        v = self.v([OK, OK, OK], [OK, OK])
        self.assertTrue(v.reachable)
        self.assertFalse(v.down)

    def test_unreachable_from_country(self):
        self.assertTrue(self.v([ERR, ERR, ERR], [OK, OK]).down)

    def test_one_node_failed_is_alert(self):
        v = self.v([OK, ERR, OK], [OK, OK])
        self.assertTrue(v.down)
        self.assertTrue(v.partial)
        self.assertFalse(v.reachable)
        self.assertEqual(v.failed, ["fr2"])

    def test_all_failed_is_not_partial(self):
        self.assertFalse(self.v([ERR, ERR, ERR], [OK, OK]).partial)

    def test_failed_as_cities(self):
        info = {TGT[1]: ("fr", "France", "Marseille")}
        v = checkhost.verdict(checkhost.parse_tcp(res([OK, ERR, ERR], [OK, OK])), TGT, CTL, info)
        self.assertEqual(v.failed, ["Marseille", "fr3"])

    def test_down_everywhere_is_not_country_specific(self):
        self.assertFalse(self.v([ERR, ERR, ERR], [ERR, ERR]).down)

    def test_no_answers_is_inconclusive(self):
        v = self.v([None, None, None], [OK, OK])
        self.assertFalse(v.down)
        self.assertFalse(v.reachable)

    def test_partial_answers_all_ok_is_fine(self):
        v = self.v([OK, None, OK], [OK, OK])
        self.assertTrue(v.reachable)
        self.assertFalse(v.down)


class ByCountry(unittest.TestCase):
    def test_grouping_and_order(self):
        info = {TGT[0]: ("fr", "France", "Paris"), TGT[1]: ("fr", "France", "Marseille"),
                CTL[0]: ("de", "Germany", "Frankfurt"), CTL[1]: ("nl", "Netherlands", "Amsterdam"),
                "jp1.node.check-host.net": ("jp", "Japan", "Tokyo")}
        res_ = {TGT[0]: True, TGT[1]: False, CTL[0]: True, CTL[1]: True, "jp1.node.check-host.net": False}
        cs = checkhost.by_country(res_, info, first="fr")
        self.assertEqual([c.cc for c in cs], ["fr", "jp", "de", "nl"])
        self.assertEqual((cs[0].ok, cs[0].fail), (["Paris"], ["Marseille"]))

    def test_flag(self):
        self.assertEqual(checkhost.flag("fr"), "🇫🇷")
        self.assertEqual(checkhost.flag("x"), "🏳")


if __name__ == "__main__":
    unittest.main()
