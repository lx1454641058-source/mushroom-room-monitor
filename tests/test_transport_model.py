import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from test_system import ready, status
from firmware.controller import Controller
from room.runtime import Runtime
from room.storage import Store
from room.report import explain_report

class SerialTransportTests(unittest.TestCase):
    def test_serial_heartbeat_command_ack_and_stop(self):
        import queue

        class FakeSerial:
            def __init__(self):
                self.control = Controller()
                self.responses = queue.Queue()
                self.origin = time.monotonic()
                self.seq = 0
                self.closed = False
                self.actions = []

            def now(self):
                return 5000 + int((time.monotonic() - self.origin) * 1000)

            def read(self, size):
                try:
                    return self.responses.get(timeout=0.02)
                except queue.Empty:
                    self.control.sample(23, 50, True, self.now())
                    self.control.step(self.now())
                    self.seq += 1
                    value = {**self.control.status(self.now()), "v": 1, "type": "status", "boot_id": "fake-board", "seq": self.seq, "uptime_ms": self.now()}
                    return (json.dumps(value) + "\n").encode()

            def write(self, raw):
                value = json.loads(raw)
                if value["type"] == "heartbeat":
                    self.control.heartbeat(self.now())
                else:
                    self.actions.append(value["action"])
                    self.control.command(value["action"], value["values"], self.now())
                    self.responses.put((json.dumps({"v": 1, "type": "ack", "boot_id": "fake-board", "id": value["id"], "ok": True}) + "\n").encode())
                return len(raw)

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "serial")
            runtime = Runtime("serial", store, "TEST-PORT")
            runtime.serial = FakeSerial()
            runtime.thread.start()
            try:
                deadline = time.monotonic() + 2
                while not runtime.snapshot()["connected"] and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(runtime.snapshot()["connected"])
                result = runtime.command("start")
                self.assertTrue(result["ok"])
                self.assertTrue(runtime.serial.control.mist)
                runtime.command("stop")
                self.assertFalse(runtime.serial.control.enabled)
                self.assertEqual(runtime.serial.actions, ["start", "stop"])
            finally:
                runtime.close()
                store.close()
            self.assertTrue(runtime.serial.closed)

    def test_new_pc_session_does_not_take_over_running_board(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "serial")
            runtime = Runtime("serial", store)
            control = ready()
            control.command("start", None, 5000)
            with self.assertRaises(OSError):
                runtime.accept(status(control))
            self.assertIsNone(runtime.status)
            store.close()


class ModelValidationTests(unittest.TestCase):
    def response(self, payload):
        import io
        class Opener:
            def open(self, request, timeout):
                return io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return Opener()

    def test_complete_structured_summary(self):
        report = {"text": "模拟数据", "source": "demo"}
        summary = "这是一份模拟演示记录，请人工核查水位开关与传感器连接，不能据此判断蘑菇长势。"
        payload = {"done": True, "done_reason": "stop", "message": {"content": json.dumps({"recommendations": ["collect_real_data"]})}}
        with patch("room.report.urllib.request.build_opener", return_value=self.response(payload)):
            self.assertEqual(explain_report(report, "test")["recommendations"], ["collect_real_data"])

    def test_truncated_missing_source_and_bad_structure_rejected(self):
        report = {"text": "模拟数据", "source": "demo"}
        payloads = [
            {"done": True, "done_reason": "length", "message": {"content": "unfinished"}},
            {"done": True, "message": {"content": "I need to think about the user"}},
            {"done": True, "message": {"content": json.dumps({"summary": "当前环境总体良好，建议继续维持设备运行，观察植物生长变化。"})}},
            {"done": True, "message": {"content": json.dumps({"summary": "这是一份模拟数据，缺水状态记录63次，建议检查水位开关和系统连接。"})}},
            {"done": True, "message": {"content": json.dumps({"recommendations": ["check_water"]})}},
            {"done": True, "message": {"content": json.dumps({"recommendations": ["verify_settings", "verify_settings"]})}},
            [],
        ]
        for payload in payloads:
            with self.subTest(payload=payload), patch("room.report.urllib.request.build_opener", return_value=self.response(payload)):
                with self.assertRaises(ValueError):
                    explain_report(report, "test")
