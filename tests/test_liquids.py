import copy
import tempfile
import unittest
from pathlib import Path
from firmware.controller import Controller
from firmware.liquids import LiquidController, validate_liquid_command
from room.runtime import Runtime, parse_status
from room.storage import Store
from room.agent import RoomAgent, validate_plan
from room.report import build_report


class LiquidUnitTests(unittest.TestCase):
    def ready(self, now=5000):
        c = Controller()
        c.heartbeat(now)
        c.sample(23, 60, True, now)
        c.liquids.sample(True, True, True, True, now)
        return c

    def test_defaults_and_strict_duration(self):
        c = self.ready()
        self.assertIsNone(c.liquids.active)
        for values in (None, {}, {"pump": "water", "seconds": True},
                       {"pump": "water", "seconds": 31}, {"pump": "nutrient", "seconds": 11},
                       {"pump": "water", "seconds": 1.5}, {"pump": "water", "seconds": -1}):
            with self.assertRaises((ValueError, TypeError)):
                validate_liquid_command(values)

    def test_timed_stop_and_no_auto_restart(self):
        c = self.ready()
        c.command("liquid", {"pump": "water", "seconds": 2}, 5000)
        self.assertEqual(c.liquids.active, "water")
        for now in (6000, 7000, 8000):
            c.heartbeat(now)
            c.sample(23, 60, True, now)
            c.liquids.sample(True, True, True, True, now)
            c.step(now)
        self.assertIsNone(c.liquids.active)
        self.assertIn("时间已到", c.liquids.reason)

    def test_each_level_interlock_stops_its_pump(self):
        for index, pump in ((0, "water"), (1, "water"), (2, "nutrient"), (3, "nutrient")):
            c = self.ready()
            c.command("liquid", {"pump": pump, "seconds": 5}, 5000)
            inputs = [True] * 4
            inputs[index] = False
            c.liquids.sample(*inputs, 5100)
            c.step(5100)
            self.assertIsNone(c.liquids.active)
            c.liquids.sample(True, True, True, True, 5200)
            c.step(5200)
            self.assertIsNone(c.liquids.active)

    def test_stale_levels_sensor_and_lease_stop(self):
        for failure in ("stale", "sensor", "lease"):
            c = self.ready()
            c.command("liquid", {"pump": "water", "seconds": 30}, 5000)
            now = 15001 if failure == "lease" else 6600
            if failure != "lease":
                c.heartbeat(now)
            if failure != "stale":
                c.liquids.sample(True, True, True, True, now)
            c.sample(None if failure == "sensor" else 23, None if failure == "sensor" else 60, True, now)
            c.step(now)
            self.assertIsNone(c.liquids.active, failure)

    def test_mutual_exclusion_no_extension_and_environment_interlock(self):
        c = self.ready()
        c.command("liquid", {"pump": "water", "seconds": 5}, 5000)
        deadline = c.liquids.until
        for action, values in (("liquid", {"pump": "water", "seconds": 20}),
                               ("liquid", {"pump": "nutrient", "seconds": 3}), ("start", None)):
            with self.assertRaises(ValueError):
                c.command(action, values, 5100)
            self.assertEqual(c.liquids.until, deadline)
        c.command("stop", None, 5200)
        self.assertIsNone(c.liquids.active)
        with self.assertRaises(ValueError):
            c.command("liquid", {"pump": "water", "seconds": 1}, 5300)
        c.command("start", None, 5400)
        with self.assertRaises(ValueError):
            c.command("liquid", {"pump": "nutrient", "seconds": 1}, 5500)

    def test_empty_mist_tank_can_be_refilled_with_environment_stopped(self):
        c = self.ready()
        c.sample(23, 60, False, 5000)
        c.command("liquid", {"pump": "water", "seconds": 5}, 5000)
        self.assertEqual(c.liquids.active, "water")
        self.assertFalse(c.enabled or c.mist)

    def test_stop_other_pump_does_not_stop_active_one(self):
        c = self.ready()
        c.command("liquid", {"pump": "water", "seconds": 5}, 5000)
        c.command("liquid", {"pump": "nutrient", "seconds": 0}, 5100)
        self.assertEqual(c.liquids.active, "water")
        c.command("stop", None, 5200)
        self.assertIsNone(c.liquids.active)


class LiquidIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "demo", "demo")
        self.runtime = Runtime("demo", self.store)
        self.runtime.demo_tick(0)
        self.agent = RoomAgent(self.runtime, self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_agent_run_stop_and_dry_refusal(self):
        result = self.agent.handle("liquid-test-001", "抽水5秒")
        self.assertTrue(result["ok"], result)
        self.assertTrue(self.runtime.snapshot()["status"]["liquids"]["water_pump"])
        self.assertTrue(self.agent.handle("liquid-test-002", "全部停止")["ok"])
        self.runtime.scenario("nutrient_source_empty")
        result = self.agent.handle("liquid-test-003", "释放营养液3秒")
        self.assertFalse(result["ok"])
        self.assertIsNone(self.runtime.control.liquids.active)

    def test_legacy_board_does_not_gain_liquid_capability(self):
        packet = copy.deepcopy(self.runtime.status)
        packet.pop("liquids")
        self.runtime.status = parse_status(packet)
        with self.assertRaisesRegex(ValueError, "未报告液体模块"):
            self.runtime.command("liquid", {"pump": "water", "seconds": 5})

    def test_offline_never_presents_stale_pump_output(self):
        self.runtime.command("liquid", {"pump": "water", "seconds": 5})
        self.runtime.scenario("offline")
        self.assertIsNone(self.runtime.snapshot()["status"]["liquids"])
        self.assertFalse(self.agent.handle("liquid-test-004", "抽水5秒")["ok"])

    def test_protocol_rejects_inconsistent_liquid_output(self):
        packet = copy.deepcopy(self.runtime.status)
        for change in ({"water_pump": True}, {"remaining_ms": 100},
                       {"water_pump": True, "nutrient_pump": True, "remaining_ms": 1000}):
            invalid = copy.deepcopy(packet)
            invalid["liquids"].update(change)
            with self.assertRaises(ValueError):
                parse_status(invalid)

    def test_queries_and_unsupported_volume_do_not_dose(self):
        for i, message in enumerate(("释放营养液100毫升", "不要抽水5秒", "每天释放营养液3秒", "抽水31秒")):
            result = self.agent.handle("liquid-guard-%d" % i, message)
            self.assertIsNone(self.runtime.control.liquids.active, result)
        with self.assertRaises(ValueError):
            validate_plan({"actions": [{"tool": "run_water_pump", "args": {"seconds": 5}},
                                       {"tool": "run_nutrient_pump", "args": {"seconds": 3}}], "clarification": ""},
                          "抽水5秒然后释放营养液3秒", self.runtime.control.config)

    def test_csv_and_report_include_liquid_records(self):
        self.runtime.command("liquid", {"pump": "water", "seconds": 5})
        self.assertIn("liquid_water_pump", self.store.csv().decode("utf-8-sig"))
        report = build_report(self.runtime, self.store)
        self.assertGreater(report["stats"]["water_pump_samples"], 0)
        self.assertIn("不代表运行次数", report["text"])

    def test_new_serial_session_refuses_running_pump(self):
        packet = copy.deepcopy(self.runtime.status)
        packet["liquids"].update(water_pump=True, remaining_ms=1000)
        runtime = Runtime("serial", self.store)
        with self.assertRaises(OSError):
            runtime.accept(packet)

