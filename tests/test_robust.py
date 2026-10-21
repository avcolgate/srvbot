"""Устойчивость: битое состояние и зависшие внешние команды."""
import asyncio
import os
import tempfile
import time
import unittest
from unittest import mock

from srvbot import util
from srvbot.util import CmdError, State


class BrokenState(unittest.TestCase):
    def load(self, raw: bytes) -> dict:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "state.json")
            open(p, "wb").write(raw)
            return State(p).data

    def test_not_an_object(self):
        for raw in (b"[]", b"5", b'"x"', b"", b"{", b"\xff\xfe"):
            self.assertEqual(self.load(raw), {})

    def test_wrong_types_dropped_rest_kept(self):
        data = self.load(b'{"traffic": 5, "metrics": "x", "history": {}, "ip_since": 1.5, "view_msg": 7}')
        self.assertEqual(data, {"ip_since": 1.5, "view_msg": 7})


class HungCommand(unittest.TestCase):
    def test_chat_does_not_wait_for_recently_hung_tool(self):
        calls = []

        async def slow(*args, **kw):
            calls.append(args[0])
            raise CmdError(f"{args[0]}: таймаут 10с")

        async def main():
            util.INTERACTIVE.set(True)
            with mock.patch.object(util, "_run", slow):
                util._hung.clear()
                with self.assertRaises(CmdError):
                    await util.run("docker", "ps")
                t = time.monotonic()
                with self.assertRaisesRegex(CmdError, "не жду"):
                    await util.run("docker", "ps")
                self.assertLess(time.monotonic() - t, 0.1)
            self.assertEqual(calls, ["docker"])  # второй раз команду даже не запускали
            util._hung.clear()
        asyncio.run(main())
