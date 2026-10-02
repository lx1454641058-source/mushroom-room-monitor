import tempfile
import threading
import unittest
from pathlib import Path
from test_system import APITests as BaseAPI
from room.agent import RoomAgent, validate_plan, direct_plan, status_text
from room.runtime import Runtime
from room.storage import Store
from firmware.controller import DEFAULTS


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "demo", "demo")
        self.runtime = Runtime("demo", self.store)
        self.runtime.demo_tick(0)
        self.agent = RoomAgent(self.runtime, self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def ask(self, message, id="request-0001"):
        return self.agent.handle(id, message)

    def test_start_stop_and_status(self):
        self.assertTrue(self.ask("启动系统")["ok"])
        self.assertTrue(self.runtime.control.enabled)
        state = self.ask("查询状态", "request-0002")
        self.assertIn("已启用", state["text"])
        self.assertTrue(self.ask("全部停止", "request-0003")["ok"])
        self.assertFalse(self.runtime.control.enabled)

    def test_negations_questions_and_unsupported_do_not_write(self):
        for i, text in enumerate(("不要启动系统", "怎么启动系统", "如果湿度低就启动系统",
                                  "关闭缺水保护然后启动系统", "启动水泵", "把温度降到20")):
            result = self.ask(text, "request-%04d" % i)
            self.assertFalse(self.runtime.control.enabled, result)
            self.assertEqual(result["trace"], [])

    def test_dry_protection_applies_to_agent(self):
        self.runtime.scenario("dry")
        result = self.ask("启动系统")
        self.assertFalse(result["ok"])
        self.assertIn("缺水", result["text"])
        self.assertFalse(self.runtime.control.enabled)

    def test_status_question_shortcut_is_read_only(self):
        result = self.ask("现在状态怎么样")
        self.assertEqual(result["trace"][0]["tool"], "get_status")
        self.assertFalse(self.runtime.control.enabled)

    def test_unsupported_camera_interval_and_fan_mode_rejected(self):
        for tool, args, message in (("camera_auto_on", {}, "开启每10分钟定时拍照"),
                                    ("set_fan", {"mode": "on"}, "请把风机关闭")):
            with self.assertRaises(ValueError):
                validate_plan({"actions": [{"tool": tool, "args": args}], "clarification": ""}, message, DEFAULTS)

    def test_offline_does_not_claim_stop_success(self):
        self.runtime.scenario("offline")
        result = self.ask("全部停止")
        self.assertFalse(result["ok"])
        self.assertIn("未连接", result["text"])

    def test_same_id_does_not_repeat_write(self):
        first = self.ask("启动系统")
        revision = self.runtime.control_revision
        self.assertEqual(self.ask("启动系统"), first)
        self.assertEqual(self.runtime.control_revision, revision)
        with self.assertRaises(ValueError):
            self.ask("全部停止")

    def test_model_compound_plan_uses_real_commands(self):
        called = []
        def planner(*_):
            called.append(True)
            return {"actions": [{"tool": "update_settings", "args": {"humidity_low": 65, "humidity_high": 75}},
                                {"tool": "start_system", "args": {}}], "clarification": ""}
        self.agent = RoomAgent(self.runtime, self.store, "fake-model", planner)
        result = self.ask("请将湿度下限调整到65以及上限调整到75，再启动自动控制系统")
        self.assertEqual(called, [True])
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.runtime.control.config["humidity_high"], 75)
        self.assertTrue(self.runtime.control.enabled)
        self.assertEqual(len(result["trace"]), 2)

    def test_common_parameters_work_without_model(self):
        result = self.ask("把湿度下限设为65，上限设为75，然后启动系统")
        self.assertTrue(result["ok"], result)
        self.assertIn("快捷执行", result["planner"])
        self.assertTrue(self.runtime.control.enabled)
        self.assertEqual(self.runtime.control.config["humidity_high"], 75)
        result = self.ask("把通风周期设为60秒，每次通风10秒", "request-fan-2")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.runtime.control.config["fan_period_s"], 60)
        self.assertEqual(self.runtime.control.config["fan_on_s"], 10)
        self.assertIn("每次 10 秒", result["text"])

    def test_shortcut_does_not_call_configured_model(self):
        def unavailable(*_):
            raise AssertionError("Common template must not need the model")
        self.agent = RoomAgent(self.runtime, self.store, "offline-model", unavailable)
        result = self.ask("请把湿度上限改为75.5%")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.runtime.control.config["humidity_high"], 75.5)

    def test_parameter_templates_do_not_extract_partial_or_ambiguous_instructions(self):
        for message in ("不要把湿度上限设为75", "把湿度上限设为75然后删除记录",
                        "把湿度上限设为7,5", "把湿度上限设为75吗", "把通风周期设为2分钟",
                        "如果缺水就把湿度下限设为60", "把湿度设为75"):
            self.assertIsNone(direct_plan(message), message)

    def test_invalid_offline_config_never_starts(self):
        for i, message in enumerate(("把湿度下限设为80，上限设为70，然后启动系统",
                                     "把通风周期设为30秒，每次通风30秒，然后启动系统")):
            before = dict(self.runtime.control.config)
            result = self.ask(message, "request-invalid-%d" % i)
            self.assertFalse(result["ok"], result)
            self.assertEqual(self.runtime.control.config, before)
            self.assertFalse(self.runtime.control.enabled)

    def test_model_cannot_treat_minutes_as_seconds(self):
        with self.assertRaises(ValueError):
            validate_plan({"actions": [{"tool": "update_settings", "args": {"fan_on_s": 2}}], "clarification": ""},
                          "把风机每次通风时长设为2分钟", DEFAULTS)

    def test_model_connection_failure_gives_actionable_message(self):
        def unavailable(*_):
            raise OSError("connection refused")
        self.agent = RoomAgent(self.runtime, self.store, "offline-model", unavailable)
        result = self.ask("请将自动控制系统立即启动")
        self.assertFalse(result["ok"])
        self.assertIn("本地模型暂时无法连接", result["text"])
        self.assertEqual(result["trace"], [])
        self.assertFalse(self.runtime.control.enabled)

    def test_status_explains_stopped_and_dry_without_writes(self):
        revision = self.runtime.control_revision
        result = self.ask("为什么不出雾")
        self.assertIn("停止状态", result["text"])
        self.assertIn("当前参数", result["text"])
        self.assertEqual(self.runtime.control_revision, revision)
        self.runtime.scenario("dry")
        result = self.ask("为什么没有加湿", "request-reason-2")
        self.assertIn("先检查储水量", result["text"])
        self.assertFalse(self.runtime.control.enabled)

    def test_status_explanations_follow_thresholds_and_disconnect(self):
        state = self.runtime.snapshot()
        state["status"] = {**state["status"], "enabled": True, "mist": False, "humidity": 80}
        self.assertIn("达到停止上限", status_text(state))
        state["status"]["humidity"] = 68
        self.assertIn("上下限之间", status_text(state))
        state["status"]["humidity"] = 60
        self.assertIn("可能", status_text(state))
        state["connected"] = False
        self.assertIn("未知", status_text(state))
        self.assertNotIn("当前参数", status_text(state))

    def test_plan_validated_in_full_before_any_writes(self):
        def planner(*_):
            return {"actions": [{"tool": "start_system", "args": {}},
                                {"tool": "update_settings", "args": {"humidity_high": 150}}], "clarification": ""}
        self.agent = RoomAgent(self.runtime, self.store, "fake-model", planner)
        result = self.ask("启动系统并把湿度上限改成150")
        self.assertFalse(result["ok"])
        self.assertFalse(self.runtime.control.enabled)

    def test_manual_stop_during_planning_cancels_start(self):
        def planner(*_):
            self.runtime.command("stop")
            return {"actions": [{"tool": "start_system", "args": {}}], "clarification": ""}
        self.agent = RoomAgent(self.runtime, self.store, "fake-model", planner)
        result = self.ask("请立即启动自动控制系统")
        self.assertFalse(result["ok"], result)
        self.assertIn("取消", result["text"])
        self.assertFalse(self.runtime.control.enabled)

    def test_fast_stop_bypasses_busy_model(self):
        self.runtime.command("start")
        self.agent.busy.acquire()
        try:
            result = self.ask("全部停止")
            self.assertTrue(result["ok"])
            self.assertFalse(self.runtime.control.enabled)
        finally:
            self.agent.busy.release()

    def test_model_may_not_invent_numbers_or_tool(self):
        for actions in ([{"tool": "run_python", "args": {}}],
                        [{"tool": "update_settings", "args": {"humidity_low": 55}}],
                        [{"tool": "start_system", "args": {}}]):
            with self.assertRaises(ValueError):
                validate_plan({"actions": actions, "clarification": ""}, "查看湿度65的记录", DEFAULTS)

    def test_camera_waits_for_client_result_and_no_fake_success(self):
        result = self.ask("拍照保存")
        self.assertEqual(result["client_action"], "camera_capture")
        self.assertTrue(result["trace"][0]["pending"])
        result = self.agent.complete_client("request-0001", "camera_capture", False, "摄像头未开启")
        self.assertFalse(result["ok"])
        self.assertNotIn("client_action", result)
        self.assertFalse(result["trace"][0]["pending"])
        with self.assertRaises(ValueError):
            self.agent.complete_client("request-0001", "camera_capture", True, "重复请求")

    def test_partial_execution_is_reported_truthfully(self):
        self.runtime.scenario("dry")
        def planner(*_):
            return {"actions": [{"tool": "update_settings", "args": {"humidity_high": 75}},
                                {"tool": "start_system", "args": {}}], "clarification": ""}
        self.agent = RoomAgent(self.runtime, self.store, "fake-model", planner)
        result = self.ask("把湿度上限改为75，然后启动系统")
        self.assertFalse(result["ok"])
        self.assertEqual(len(result["trace"]), 1)
        self.assertIn("已完成 1 步", result["text"])
        self.assertEqual(self.runtime.control.config["humidity_high"], 75)
        self.assertFalse(self.runtime.control.enabled)


class AgentAPITests(unittest.TestCase):
    setUp = BaseAPI.setUp
    tearDown = BaseAPI.tearDown
    request = BaseAPI.request

    def test_chat_api_writes_through_runtime(self):
        code, _, data = self.request("POST", "/api/agent", {"id": "request-http-1", "message": "启动系统"})
        self.assertEqual(code, 200)
        self.assertTrue(__import__("json").loads(data)["ok"])
        self.assertTrue(self.runtime.control.enabled)

    def test_missing_request_id_rejected(self):
        code, _, _ = self.request("POST", "/api/agent", {"message": "启动系统"})
        self.assertEqual(code, 400)
        self.assertFalse(self.runtime.control.enabled)

del BaseAPI
