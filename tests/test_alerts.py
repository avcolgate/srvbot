import time
import unittest
from unittest import mock

from srvbot import docker
from srvbot.__main__ import start_text
from srvbot.alerts import Alerts


def ctr(name: str, running: bool = True, ports: tuple[str, ...] = ()) -> docker.Container:
    return docker.Container(name=name, image="img", status="running" if running else "exited",
                            restart_policy="always", started_at="", ports=list(ports))


class FakeState:
    def __init__(self, data: dict | None = None):
        self.data = data or {}

    def get(self, key, default=None):
        return self.data.setdefault(key, default)

    def __setitem__(self, key, value):
        self.data[key] = value

    def save(self):
        pass


class UnpublishedPort(unittest.TestCase):
    """Порт docker-proxy пропал: поломка это или контейнер просто пересоздали с другим портом."""

    def alerts(self, ctrs, known=("app",)):
        a = Alerts(bot=None, state=FakeState({"containers": list(known)}))
        a.ctrs = None if ctrs is None else {c.name: c for c in ctrs}
        return a

    def test_container_republished_on_other_port(self):
        a = self.alerts([ctr("app", ports=("443/udp",))])
        self.assertTrue(a._unpublished("udp/40000"))

    def test_port_still_published(self):
        a = self.alerts([ctr("app", ports=("443/udp",))])
        self.assertFalse(a._unpublished("udp/443"))

    def test_container_down(self):
        a = self.alerts([ctr("app", running=False)])
        self.assertFalse(a._unpublished("udp/443"))

    def test_container_missing(self):
        a = self.alerts([ctr("other")])
        self.assertFalse(a._unpublished("udp/443"))

    def test_docker_not_answering(self):
        a = self.alerts(None)
        self.assertFalse(a._unpublished("udp/443"))


class StartText(unittest.TestCase):
    def test_after_reboot_with_downtime(self):
        now = time.time()
        with mock.patch("psutil.boot_time", return_value=now - 60):
            text = start_text(FakeState({"last_seen": now - 60 - 7 * 60, "clean_exit": True}))
        self.assertIn("перезагрузился", text)
        self.assertIn("7 мин", text)

    def test_after_reboot_without_mark(self):
        with mock.patch("psutil.boot_time", return_value=time.time() - 60):
            text = start_text(FakeState({}))
        self.assertIn("перезагрузился", text)
        self.assertNotIn("не работал", text)

    def test_crash(self):
        with mock.patch("psutil.boot_time", return_value=time.time() - 86400):
            self.assertIn("после сбоя", start_text(FakeState({"clean_exit": False})))

    def test_normal(self):
        with mock.patch("psutil.boot_time", return_value=time.time() - 86400):
            self.assertIn("запущен", start_text(FakeState({"clean_exit": True})))


if __name__ == "__main__":
    unittest.main()
