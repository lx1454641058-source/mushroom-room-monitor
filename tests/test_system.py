import copy
import http.client
import json
import math
import struct
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch
from firmware.controller import Controller, DEFAULTS, validate_config
from firmware.aht20 import crc8, decode
from room.images import validate_png, SIGNATURE
from room.runtime import Runtime, parse_status
from room.storage import Store
from room.report import build_report, explain_report
from room.server import make_server


def ready(now=5000, humidity=50):
    c = Controller()
    c.heartbeat(now)
    c.sample(23, humidity, True, now)
    return c


def status(c=None, seq=1, boot="test"):
    c = c or ready()
    return {**c.status(5000), "v": 1, "type": "status", "seq": seq, "boot_id": boot, "uptime_ms": 5000}


def png(width=2, height=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress((b"\x00" + b"\x00\x80\x00" * width) * height)) + chunk(b"IEND", b"")


class ControlTests(unittest.TestCase):
    def test_boot_outputs_off(self):
        c = Controller()
        c.step(5000)
        self.assertFalse(c.enabled or c.mist or c.fan)
        with self.assertRaises(ValueError):
            c.command("start", None, 5000)

    def test_hysteresis_and_minimum_off(self):
        c = ready()
        c.command("start", None, 5000)
        self.assertTrue(c.mist)
        c.sample(23, 68, True, 5100)
        c.step(5100)
        self.assertTrue(c.mist)
        c.sample(23, 72, True, 5200)
        c.step(5200)
        self.assertFalse(c.mist)
        c.sample(23, 50, True, 5300)
        c.step(5300)
        self.assertFalse(c.mist)
        c.heartbeat(10200)
        c.sample(23, 50, True, 10200)
        c.step(10200)
        self.assertTrue(c.mist)

    def test_dry_latches_off_until_start(self):
        c = ready()
        c.command("start", None, 5000)
        c.sample(23, 50, False, 5100)
        c.step(5100)
        self.assertFalse(c.enabled or c.mist or c.fan)
        c.sample(23, 50, True, 5200)
        c.step(5200)
        self.assertFalse(c.enabled)
        c.command("start", None, 5300)
        self.assertTrue(c.enabled)

    def test_sensor_invalid_and_stale_stop(self):
        for bad in (None, float("nan"), float("inf"), True, -1, 101):
            with self.subTest(bad=bad):
                c = ready()
                c.command("start", None, 5000)
                c.sample(23, bad, True, 5100)
                c.step(5100)
                self.assertFalse(c.enabled or c.mist or c.fan)
                self.assertIsNone(c.status(5100)["humidity"])
        c = ready()
        c.command("start", None, 5000)
        c.heartbeat(11000)
        c.step(11000)
        self.assertFalse(c.enabled)
        self.assertIsNone(c.status(11000)["humidity"])

    def test_lease_stops_and_requires_restart(self):
        c = ready()
        c.command("start", None, 5000)
        c.sample(23, 50, True, 15000)
        c.step(15000)
        self.assertFalse(c.enabled or c.mist or c.fan)
        c.heartbeat(15001)
        c.step(15001)
        self.assertFalse(c.enabled)

    def test_continuous_mist_limit(self):
        c = ready()
        c.command("start", None, 5000)
        for now in range(6000, 126000, 1000):
            c.heartbeat(now)
            c.sample(23, 50, True, now)
            c.step(now)
        self.assertFalse(c.enabled or c.mist or c.fan)
        self.assertIn("120", c.fault)

    def test_schedule_manual_and_stop(self):
        c = ready(humidity=80)
        c.command("start", None, 5000)
        self.assertTrue(c.fan)
        c.heartbeat(20000)
        c.sample(23, 80, True, 20000)
        c.step(20000)
        self.assertFalse(c.fan)
        c.command("fan", "on", 20000)
        self.assertTrue(c.fan)
        c.heartbeat(50000)
        c.sample(23, 80, True, 50000)
        c.step(50000)
        self.assertEqual(c.fan_mode, "schedule")
        c.command("fan", "off", 50000)
        self.assertFalse(c.fan)
        c.command("stop", None, 50001)
        self.assertFalse(c.enabled or c.mist or c.fan)

    def test_configuration_atomic_validation(self):
        for patch_values in ({"humidity_low": 80}, {"humidity_high": float("nan")}, {"humidity_high": True},
                             {"fan_on_s": 300}, {"fan_period_s": 1}, {"extra": 1}):
            c = ready()
            values = {**DEFAULTS, **patch_values}
            with self.subTest(values=values), self.assertRaises(ValueError):
                c.command("config", values, 5000)
            self.assertEqual(c.config, DEFAULTS)
        self.assertEqual(validate_config(DEFAULTS), DEFAULTS)

    def test_aht20_known_frame_and_crc(self):
        # 50%RH -> 0x80000; 25C -> 0x60000, status calibrated.
        data = bytes([0x18, 0x80, 0x00, 0x06, 0x00, 0x00])
        self.assertEqual(crc8(b"123456789"), 0xF7)
        self.assertEqual(decode(data + bytes([crc8(data)])), (25.0, 50.0))
        with self.assertRaises(ValueError):
            decode(data + b"\x00")
        busy = bytes([0x98]) + data[1:]
        with self.assertRaises(ValueError):
            decode(busy + bytes([crc8(busy)]))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "demo")
        self.runtime = Runtime("demo", self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_strict_protocol(self):
        parse_status(status())
        for field, value in (("humidity", math.nan), ("water_ok", 1), ("seq", True), ("boot_id", ""), ("fan_mode", "bad")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_status({**status(), field: value})
        with self.assertRaises(ValueError):
            parse_status({**status(), "sensor_ok": False})

    def test_duplicates_do_not_refresh_connection(self):
        self.runtime.accept(status())
        self.runtime.received = time.monotonic() - 5
        self.runtime.accept(status())
        self.assertFalse(self.runtime.snapshot()["connected"])
        self.assertIsNone(self.runtime.snapshot()["status"]["mist"])
        self.assertEqual(len(self.store.history()), 1)
        self.runtime.accept(status(seq=2))
        self.assertTrue(self.runtime.snapshot()["connected"])

    def test_boot_change_cancels_pending(self):
        self.runtime.accept(status())
        pending = {"event": threading.Event(), "result": None}
        self.runtime.pending = pending
        self.runtime.accept(status(boot="new-boot"))
        self.assertTrue(pending["event"].is_set())
        self.assertFalse(pending["result"]["ok"])

    def test_demo_fault_no_fake_recovery(self):
        self.runtime.demo_tick(0)
        self.runtime.command("start")
        self.runtime.scenario("dry")
        self.assertFalse(self.runtime.snapshot()["status"]["enabled"])
        with self.assertRaises(ValueError):
            self.runtime.command("start")
        self.runtime.scenario("restore")
        self.assertFalse(self.runtime.snapshot()["status"]["enabled"])
        self.runtime.scenario("offline")
        self.assertFalse(self.runtime.snapshot()["connected"])
        self.assertIsNone(self.runtime.snapshot()["status"]["humidity"])
        with self.assertRaises(ValueError):
            self.runtime.command("start")

    def test_report_statistics_and_source(self):
        self.runtime.accept(status())
        self.runtime.accept(status(seq=2))
        report = build_report(self.runtime, self.store)
        self.assertEqual(report["source"], "demo")
        self.assertEqual(report["stats"]["valid_samples"], 2)
        self.assertEqual(report["stats"]["humidity_mean"], 50)
        self.assertIn("不代表完整一天", report["text"])
        with self.assertRaises(ValueError):
            explain_report(report, None)

    def test_sqlite_persistence(self):
        self.runtime.accept(status())
        second = Store(Path(self.temp.name), "demo")
        try:
            self.assertEqual(second.history()[0]["humidity"], 50)
        finally:
            second.close()


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "demo")
        self.runtime = Runtime("demo", self.store)
        self.runtime.demo_tick(0)
        self.server = make_server(self.runtime, self.store, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        self.temp.cleanup()

    def request(self, method, path, value=None, headers=None, raw=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        hs = {"Content-Type": "application/json", "X-Room-Client": "local-dashboard"}
        hs.update(headers or {})
        body = raw if raw is not None else (json.dumps(value).encode() if value is not None else None)
        client.request(method, path, body, hs)
        response = client.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        client.close()
        return result

    def test_roundtrip_control_and_dry_shutdown(self):
        code, _, data = self.request("POST", "/api/control", {"action": "start"})
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(data)["ok"])
        self.request("POST", "/api/scenario", {"name": "dry"})
        _, _, data = self.request("GET", "/api/status")
        self.assertFalse(json.loads(data)["status"]["enabled"])

    def test_origin_host_and_client_guards(self):
        for headers in ({"Origin": "https://example.org"}, {"Host": "evil.example"}, {"X-Room-Client": ""}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                code, _, _ = self.request("POST", "/api/control", {"action": "start"}, headers)
                self.assertEqual(code, 403)
        self.assertFalse(self.runtime.control.enabled)

    def test_bad_payload_does_not_change_config(self):
        for payload in ({"action": "config", "values": {"humidity_low": 80}}, {"action": "delete"}, ["start"], {"action": "fan", "values": {}}):
            code, _, _ = self.request("POST", "/api/control", payload)
            self.assertEqual(code, 400)
        self.assertEqual(self.runtime.control.config, DEFAULTS)

    def test_photo_capture_store_read_and_invalid_rejection(self):
        original = png()
        code, _, data = self.request("POST", "/api/photos", headers={"Content-Type": "image/png"}, raw=original)
        self.assertEqual(code, 200, data)
        identifier = json.loads(data)["id"]
        code, headers, data = self.request("GET", "/photos/" + identifier)
        self.assertEqual((code, headers["Content-Type"], data), (200, "image/png", original))
        code, _, _ = self.request("POST", "/api/photos", headers={"Content-Type": "image/png"}, raw=b"<script>bad</script>")
        self.assertEqual(code, 400)
        self.assertEqual(len(self.store.photos()), 1)

    def test_exports_and_unknown_paths(self):
        code, headers, data = self.request("GET", "/api/export.csv")
        self.assertEqual(code, 200)
        self.assertIn("demo", data.decode("utf-8-sig"))
        self.assertIn("sample_id", data.decode("utf-8-sig"))
        for path in ("/../../run.ps1", "/firmware/main.py", "/api/history?limit=nan"):
            code, _, _ = self.request("GET", path)
            self.assertIn(code, (400, 404))

    def test_png_bounds_and_trailing_data(self):
        for data in (png() + b"extra", png()[:-1], png(1281, 1)):
            with self.assertRaises(ValueError):
                validate_png(data)


if __name__ == "__main__":
    unittest.main()
