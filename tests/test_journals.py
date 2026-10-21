"""Тесты сводок журналов, понятных записей в журнал службы и истории действий."""
import os
import tempfile
import unittest

from srvbot import handlers, history, manage
from srvbot.util import State

SSH = """\
Oct 21 10:00:01 host sshd[100]: Invalid user admin from 203.0.113.5 port 4000
Oct 21 10:00:02 host sshd[100]: Connection closed by invalid user admin 203.0.113.5 port 4000 [preauth]
Oct 21 10:01:00 host sshd[101]: Connection closed by authenticating user root 198.51.100.7 port 5000 [preauth]
Oct 21 10:02:00 host sshd[102]: Accepted publickey for root from 192.0.2.1 port 6000 ssh2: ED25519 SHA256:x
Oct 21 10:03:00 host sshd[103]: Accepted publickey for root from 192.0.2.1 port 6001 ssh2: ED25519 SHA256:x
Oct 21 10:04:00 host sshd[104]: Received disconnect from 192.0.2.1 port 6001:11: disconnected by user
"""

ERRORS = """\
-- No entries --
Oct 21 06:20:06 host systemd-networkd-wait-online[933383]: Timeout occurred while waiting for network connectivity.
Oct 21 07:20:06 host systemd-networkd-wait-online[939418]: Timeout occurred while waiting for network connectivity.
Oct 21 08:00:00 host sshd[5]: error: kex_exchange_identification: read: Connection reset by peer
Oct 21 09:00:00 host kernel: disk 1.2.3.4 failed after 30 tries
"""


class Journals(unittest.TestCase):
    def test_ssh_counts_connections_not_lines(self):
        s = manage.parse_ssh(SSH)
        self.assertEqual(dict(s["accepted"]), {("root", "192.0.2.1", "publickey"): 2})
        self.assertEqual((s["failed"], s["failed_ips"]), (2, 2))

    def test_errors_grouped_without_sshd(self):
        total, top = manage.group_errors(ERRORS)
        self.assertEqual(total, 3)
        self.assertEqual(top[0], ("systemd-networkd-wait-online: Timeout occurred while waiting for network "
                                  "connectivity.", 2))
        self.assertEqual(top[1][0], "kernel: disk N failed after N tries")


class Describe(unittest.TestCase):
    def test_buttons_and_text(self):
        self.assertEqual(handlers.describe_data("v:tun"), "раздел «Клиенты»")
        self.assertEqual(handlers.describe_data("go:clean:"), "запрос подтверждения: очистить диск")
        self.assertEqual(handlers.describe_data("ub:sshd:1.2.3.4"), "разбан 1.2.3.4 в sshd")
        self.assertEqual(handlers.describe_text(handlers.BTN["geo"]), "раздел «Доступность»")
        self.assertEqual(handlers.describe_text("/bans"), "команда /bans")


class History(unittest.TestCase):
    def test_record_keeps_last_and_renders(self):
        with tempfile.TemporaryDirectory() as d:
            st = State(os.path.join(d, "s", "state.json"))
            for i in range(history.KEEP + 3):
                history.record(st, f"действие {i}", took=1.0)
            history.record(st, "очистить диск", ok=False, detail="нет места <x>", took=75)
            self.assertEqual(len(st.data["history"]), history.KEEP)
            text = history.view(st)
            self.assertIn("🔴 очистить диск · нет места &lt;x&gt; · 1 мин", text)
            self.assertIn(f"последние {history.SHOW}", text)
