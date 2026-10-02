import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from test_system import status, png, APITests as BaseAPI
from room.storage import Store
from room.runtime import Runtime
from room.report import build_report
from room.maintenance import backup, check


class DataWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "demo", "demo")
        self.runtime = Runtime("demo", self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_actual_span_gaps_and_clock_reversal(self):
        for seq, at in enumerate(("2026-09-26T00:00:00+00:00", "2026-09-26T00:00:01+00:00",
                                  "2026-09-26T00:00:20+00:00", "2026-09-26T00:00:19+00:00"), 1):
            with patch("room.storage.timestamp", return_value=at):
                self.runtime.accept(status(seq=seq))
        report = build_report(self.runtime, self.store)
        self.assertEqual(report["stats"]["span_seconds"], 19)
        self.assertEqual(report["stats"]["gap_count"], 1)
        self.assertEqual(report["stats"]["clock_reversals"], 1)
        self.assertNotIn("约一小时", report["text"])

    def test_empty_report_and_stale_fault_not_presented_as_current(self):
        report = build_report(self.runtime, self.store)
        self.assertEqual(report["stats"]["span_seconds"], 0)
        self.runtime.accept({**status(), "fault": "旧故障"})
        self.runtime.received = 0
        self.runtime.error = "当前断线"
        report = build_report(self.runtime, self.store)
        self.assertIn("当前提示：当前断线", report["text"])
        self.assertNotIn("当前提示：旧故障", report["text"])

    def test_consistent_backup_includes_photos_and_preserves_existing(self):
        self.store.sample(status())
        self.store.event("实验记录")
        self.store.add_photo("a" * 32, "image/png", png())
        first = backup(self.root, "demo")
        original_backup = Path(first["path"]).read_bytes()
        self.store.sample(status(seq=2))
        second = backup(self.root, "demo")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(first["counts"], {"samples": 1, "events": 1, "photos": 1})
        self.assertEqual(second["counts"]["samples"], 2)
        self.assertEqual(Path(first["path"]).read_bytes(), original_backup)
        restored = sqlite3.connect(first["path"])
        try:
            self.assertEqual(restored.execute("SELECT image FROM photos").fetchone()[0], png())
        finally:
            restored.close()

    def test_check_does_not_create_missing_serial_database(self):
        result = check(self.root)
        self.assertFalse(result["databases"]["serial"]["exists"])
        self.assertFalse((self.root / "serial").exists())
        with self.assertRaises(ValueError):
            backup(self.root, "serial")

    def test_csv_text_cannot_be_interpreted_as_formula(self):
        self.store.sample({**status(), "temperature": -2, "fault": " =1+1"})
        row = list(csv.reader(io.StringIO(self.store.csv().decode("utf-8-sig"))))[1]
        self.assertEqual(row[3], "-2")
        self.assertEqual(row[10], "' =1+1")


class CommandBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "serial")
        self.runtime = Runtime("serial", self.store)
        self.runtime.accept(status())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_concurrent_control_rejected_instead_of_queued(self):
        self.runtime.command_lock.acquire()
        try:
            with self.assertRaisesRegex(ValueError, "上一条"):
                self.runtime.command("start")
            self.assertIsNone(self.runtime.pending)
        finally:
            self.runtime.command_lock.release()

    def test_cancelled_ack_cannot_be_overwritten(self):
        p = {"id": "abc", "boot_id": "test", "sent": True, "result": None, "event": threading.Event()}
        self.runtime.pending = p
        self.runtime.fail_pending("开发板已重启")
        self.runtime.acknowledge({"v": 1, "boot_id": "test", "id": "abc", "ok": True})
        self.assertFalse(p["result"]["ok"])

    def test_ack_requires_sent_matching_boot_and_strict_version(self):
        p = {"id": "abc", "boot_id": "test", "sent": False, "result": None, "event": threading.Event()}
        self.runtime.pending = p
        good = {"v": 1, "boot_id": "test", "id": "abc", "ok": True}
        self.runtime.acknowledge(good)
        self.assertIsNone(p["result"])
        p["sent"] = True
        self.runtime.acknowledge({**good, "boot_id": "other"})
        self.runtime.acknowledge({**good, "v": True})
        self.assertIsNone(p["result"])
        self.runtime.acknowledge(good)
        self.assertTrue(p["result"]["ok"])

    def test_timeout_stays_unknown_and_late_ack_rejected(self):
        with patch("threading.Event.wait", return_value=False):
            with self.assertRaisesRegex(ValueError, "超时"):
                self.runtime.command("start")
        p = self.runtime.pending
        self.runtime.acknowledge({"v": 1, "boot_id": "test", "id": p["id"], "ok": True})
        self.assertFalse(p["result"]["ok"])
        self.assertFalse(self.runtime.snapshot()["connected"])
        self.assertIsNone(self.runtime.snapshot()["status"]["mist"])

    def test_backward_uptime_not_accepted(self):
        self.runtime.accept({**status(seq=2), "uptime_ms": 4000})
        self.assertEqual(self.runtime.status["seq"], 1)


# Reuse setup/request helpers, not inherited test cases, to keep counts meaningful.
class ReportDownloadTests(unittest.TestCase):
    setUp = BaseAPI.setUp
    tearDown = BaseAPI.tearDown
    request = BaseAPI.request

    def test_text_download_contains_source_and_range(self):
        code, headers, raw = self.request("GET", "/api/report.txt")
        self.assertEqual(code, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("模拟演示", raw.decode("utf-8-sig"))
        self.assertIn("时间跨度", raw.decode("utf-8-sig"))

del BaseAPI
