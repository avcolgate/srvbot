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


class ContainerHistory(unittest.TestCase):
    def test_state_and_bot_journal_instead_of_disabled_logs(self):
        import asyncio
        from unittest import mock
        from srvbot.util import CmdError

        async def fake_run(*args, **kw):
            if args[:2] == ("docker", "logs"):
                raise CmdError("docker logs: configured logging driver does not support reading")
            if args[:2] == ("docker", "inspect"):
                return "exited|2026-10-21T08:08:34.98Z|3|always|true|137\n"
            return ("1792573200.1 host python3[5]: WARNING srvbot.alerts: уведомление: 🔴 контейнер box: упал\n"
                    "1792573260.1 host python3[5]: INFO srvbot.handlers: журнал box — 0.2 с\n")
        with mock.patch.object(manage, "run", fake_run):
            out = asyncio.run(manage.container_logs("box"))
        self.assertIn("остановлен, запущен", out)
        self.assertIn("перезапусков 3 (политика always), последний раз убит из-за нехватки памяти, последний код выхода 137", out)
        self.assertIn("уведомление: 🔴 контейнер box: упал", out)
        self.assertNotIn("журнал box", out)  # нажатия кнопок — не история контейнера


class CompactLine(unittest.TestCase):
    def test_logfmt_and_plain(self):
        dockerd = ('Oct 21 16:05:20 host dockerd[963087]: time="2026-10-21T16:05:20.71+02:00" level=error '
                   'msg="Handler for GET /containers/{name:.*}/logs returned error" '
                   'error-response="configured logging driver does not support reading" method=GET module=api')
        self.assertEqual(manage.compact_line(dockerd), "16:05:20 🔴 Handler for GET /containers/{name:.*}/logs "
                                                        "returned error: configured logging driver does not support reading")
        self.assertEqual(manage.compact_line("Oct 21 06:20:42 host fail2ban-server[1]: Server ready"),
                         "06:20:42 Server ready")
        self.assertEqual(manage.compact_line("Oct 21 06:20:41 host systemd[1]: Started x."), "06:20:41 Started x.")
        self.assertLessEqual(len(manage.compact_line("Oct 21 06:20:41 host x[1]: " + "a" * 1000)), 200)
        self.assertEqual(manage.compact_line("-- No entries --"), "-- No entries --")
