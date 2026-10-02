import json
import math
import threading
import time
import uuid
from firmware.controller import Controller, validate_config
from firmware.liquids import validate_liquid_command
from firmware.climate import validate_climate_config, validate_climate_command


def parse_status(value):
    if not isinstance(value, dict) or type(value.get("v")) is not int or value.get("v") != 1 or value.get("type") != "status":
        raise ValueError("不是版本 1 状态报文")
    if not isinstance(value.get("boot_id"), str) or not 1 <= len(value["boot_id"]) <= 64:
        raise ValueError("缺少启动标识")
    for field in ("seq", "uptime_ms"):
        if type(value.get(field)) is not int or value[field] < 0:
            raise ValueError("序号或时间无效")
    for field in ("enabled", "mist", "fan", "water_ok", "sensor_ok"):
        if type(value.get(field)) is not bool:
            raise ValueError("设备状态必须为布尔值")
    for field, low, high in (("temperature", -40, 85), ("humidity", 0, 100)):
        number = value.get(field)
        if value["sensor_ok"]:
            if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
                raise ValueError("传感器数据无效")
        elif number is not None:
            raise ValueError("传感器故障时不能提供有效读数")
    if value.get("fan_mode") not in ("schedule", "on", "off"):
        raise ValueError("风机模式无效")
    if not isinstance(value.get("fault"), str) or len(value["fault"]) > 300:
        raise ValueError("故障字段无效")
    if (value["mist"] or value["fan"]) and not value["enabled"]:
        raise ValueError("设备输出与停机状态冲突")
    validate_config(value.get("config"))
    keys = ("v", "type", "boot_id", "seq", "uptime_ms", "temperature", "humidity", "enabled", "mist", "fan", "water_ok", "sensor_ok", "fault", "fan_mode", "config")
    result = {key: value[key] for key in keys}
    liquid = value.get("liquids")
    if liquid is not None:
        bools = ("water_pump", "nutrient_pump", "water_source_ok", "water_room_ok", "nutrient_source_ok", "nutrient_room_ok")
        if not isinstance(liquid, dict) or set(liquid) != set(bools) | {"remaining_ms", "fault"}:
            raise ValueError("液体模块报文格式无效")
        if any(type(liquid[k]) is not bool for k in bools):
            raise ValueError("液体状态必须为布尔值")
        if type(liquid["remaining_ms"]) is not int or not 0 <= liquid["remaining_ms"] <= 30000:
            raise ValueError("液体运行时间无效")
        if not isinstance(liquid["fault"], str) or len(liquid["fault"]) > 300:
            raise ValueError("液体故障文本无效")
        running = liquid["water_pump"] or liquid["nutrient_pump"]
        if (liquid["water_pump"] and liquid["nutrient_pump"]) or (running and value["enabled"]):
            raise ValueError("液体输出互锁冲突")
        if bool(liquid["remaining_ms"]) != running:
            raise ValueError("液体剩余时间与运行状态冲突")
        for pump, limit in (("water", 30000), ("nutrient", 10000)):
            if liquid[pump + "_pump"] and (not value["sensor_ok"] or not liquid[pump + "_source_ok"]
                    or not liquid[pump + "_room_ok"] or liquid["remaining_ms"] > limit):
                raise ValueError("液体运行与保护条件冲突")
        result["liquids"] = dict(liquid)
    climate = value.get("climate")
    if climate is not None:
        bools = ("enabled", "heater", "cooler", "ventilation", "co2_ok")
        if type(climate) is not dict or set(climate) != set(bools) | {"co2", "config", "fault"}:
            raise ValueError("温控与CO2状态格式无效")
        if any(type(climate[k]) is not bool for k in bools):
            raise ValueError("温控状态必须为布尔值")
        validate_climate_config(climate["config"])
        if type(climate["fault"]) is not str or len(climate["fault"]) > 300:
            raise ValueError("温控故障文本无效")
        ppm = climate["co2"]
        if (climate["co2_ok"] and (type(ppm) not in (int, float) or not 0 < ppm <= 40000)
                or not climate["co2_ok"] and ppm is not None):
            raise ValueError("CO2读数无效")
        if climate["heater"] and climate["cooler"]:
            raise ValueError("升降温不能同时输出")
        if climate["enabled"] and (not value["enabled"] or not value["sensor_ok"] or not climate["co2_ok"]
                                   or not 5 < value["temperature"] < 40):
            raise ValueError("温控启用与保护状态冲突")
        if any(climate[k] for k in ("heater", "cooler", "ventilation")) and not climate["enabled"]:
            raise ValueError("温控输出与停机状态冲突")
        if climate["ventilation"] and not value["fan"]:
            raise ValueError("CO2通风请求与风机状态冲突")
        result["climate"] = dict(climate)
    return result


def validate_command(action, values):
    if action not in ("start", "stop", "config", "fan", "liquid", "climate"):
        raise ValueError("未知控制命令")
    if action == "config":
        validate_config(values)
    elif action == "liquid":
        validate_liquid_command(values)
    elif action == "climate":
        validate_climate_command(values)
    elif action == "fan":
        if values not in ("schedule", "on", "off"):
            raise ValueError("未知风机模式")
    elif values is not None:
        raise ValueError("该操作不接受附加参数")


class Runtime:
    def __init__(self, mode, store, port=None):
        self.mode, self.store, self.port = mode, store, port
        self.lock = threading.RLock()
        self.command_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.status = None
        self.received = 0
        self.error = "等待首条数据"
        self.pending = None
        self.control_revision = 0
        self.serial = None
        self.control = Controller()
        self.started = time.monotonic()
        self.demo_humidity = 61.0
        self.demo_temperature = 23.5
        self.demo_co2 = 700.0
        self.demo_co2_sensor = True
        self.demo_water = self.demo_sensor = True
        self.demo_connected = True
        self.demo_liquids = [True, True, True, True]
        self.demo_boot = "demo-" + uuid.uuid4().hex[:8]
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        if self.mode == "serial":
            try:
                import serial
            except ImportError as exc:
                raise RuntimeError("实物模式需要 pyserial。按 README 安装硬件依赖；不会自动切换模拟数据。") from exc
            self.serial = serial.Serial()
            self.serial.port = self.port
            self.serial.baudrate = 115200
            self.serial.timeout = 0.1
            self.serial.write_timeout = 0.5
            self.serial.dtr = self.serial.rts = False
            self.serial.open()
        self.store.event("启动" + ("模拟演示" if self.mode == "demo" else "实物串口") + "；控制默认停止")
        self.thread.start()

    def now(self):
        return int((time.monotonic() - self.started) * 1000)

    def snapshot(self):
        with self.lock:
            age = time.monotonic() - self.received if self.received else None
            connected = self.status is not None and age is not None and age < 4 and not self.error and not self.stop_event.is_set()
            status = dict(self.status) if self.status else {}
            if not connected:
                # Do not render last reported actuator states as current after disconnection.
                for key in ("temperature", "humidity", "water_ok", "sensor_ok", "enabled", "mist", "fan"):
                    status[key] = None
                if "liquids" in status:
                    status["liquids"] = None
                if "climate" in status:
                    status["climate"] = None
            return {"mode": self.mode, "connected": connected, "control_revision": self.control_revision, "age_s": round(age, 1) if age is not None else None,
                    "error": self.error if self.error else ("数据过期，设备输出状态未知" if not connected else ""),
                    "status": status}

    def accept(self, value):
        value = parse_status(value)
        with self.lock:
            previous = self.status
            new_boot = not previous or previous["boot_id"] != value["boot_id"]
            liquid_running = any(value.get("liquids", {}).get(k, False) for k in ("water_pump", "nutrient_pump"))
            if self.mode == "serial" and new_boot and (value["enabled"] or liquid_running):
                raise OSError("开发板仍在运行；已停止通信。等待至少 10 秒后重新启动后台，再人工启用")
            if previous and previous["boot_id"] == value["boot_id"] and (value["seq"] <= previous["seq"] or value["uptime_ms"] < previous["uptime_ms"]):
                return
            if previous and previous["boot_id"] != value["boot_id"]:
                self.control_revision += 1
                self.store.event("开发板重新启动；需要重新启动控制")
                self.fail_pending("开发板已重启，命令取消")
            changed = not previous or any(previous[k] != value[k] for k in ("fault", "enabled", "mist", "fan"))
            liquid_changed = (not previous or any(previous.get("liquids", {}).get(k) != value.get("liquids", {}).get(k)
                              for k in ("water_pump", "nutrient_pump", "fault")))
            self.status, self.received, self.error = value, time.monotonic(), ""
            self.store.sample(value)
            climate = value.get("climate")
            if climate and (not previous or any((previous.get("climate") or {}).get(k) != climate[k]
                                               for k in ("heater", "cooler", "enabled", "ventilation", "fault"))):
                self.store.event("温控联动%s，加热%s，制冷%s；%s" % (
                    "启用" if climate["enabled"] else "关闭", "开启" if climate["heater"] else "关闭",
                    "开启" if climate["cooler"] else "关闭", climate["fault"]))
            if liquid_changed and "liquids" in value:
                liquid = value["liquids"]
                self.store.event("清水泵%s，营养液泵%s；%s" % (
                    "开启" if liquid["water_pump"] else "关闭", "开启" if liquid["nutrient_pump"] else "关闭", liquid["fault"]))
            if changed:
                self.store.event("自动控制%s，加湿%s，通风%s；%s" % (
                    "已启用" if value["enabled"] else "已停止", "开启" if value["mist"] else "关闭",
                    "开启" if value["fan"] else "关闭", value["fault"] or "正常"))

    def fail_pending(self, message):
        if self.pending and self.pending["result"] is None:
            self.pending["result"] = {"ok": False, "error": message}
            self.pending["event"].set()

    def acknowledge(self, value):
        with self.lock:
            p = self.pending
            if (p and p["result"] is None and p["sent"] and not self.stop_event.is_set()
                and type(value.get("v")) is int and value["v"] == 1
                and value.get("boot_id") == p["boot_id"] and value.get("id") == p["id"]
                and type(value.get("ok")) is bool):
                p["result"] = {"ok": value["ok"], "error": str(value.get("error", "开发板拒绝命令"))[:300]}
                p["event"].set()

    def write(self, value):
        encoded = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii")
        written = self.serial.write(encoded)
        if written != len(encoded):
            raise OSError("串口写入不完整")

    def command(self, action, values=None, expected_revision=None, expected_boot=None):
        validate_command(action, values)
        # Reject concurrent requests instead of executing stale clicks after a queue wait.
        if not self.command_lock.acquire(blocking=False):
            raise ValueError("上一条控制命令尚未完成，请等待应答后重试")
        try:
            with self.lock:
                if expected_revision is not None and expected_revision != self.control_revision:
                    raise ValueError("控制状态已被其他操作改变，智能体计划已取消")
                if expected_boot is not None and (not self.status or self.status["boot_id"] != expected_boot):
                    raise ValueError("开发板会话已变化，智能体计划已取消")
                self.control_revision += 1
                if not self.snapshot()["connected"]:
                    raise ValueError("设备未连接，不能发送控制命令")
                if action == "liquid" and "liquids" not in self.status:
                    raise ValueError("当前开发板未报告液体模块，请先更新配套固件")
                if action == "climate" and "climate" not in self.status:
                    raise ValueError("当前开发板未报告温控与CO2模块，请更新配套固件")
                if self.mode == "demo":
                    self.control.command(action, values, self.now())
                    self.demo_publish()
                    self.store.event("已执行模拟命令：" + action)
                    return {"ok": True, "message": "模拟控制已执行"}
                pending = {"id": uuid.uuid4().hex, "action": action, "values": values,
                           "boot_id": self.status["boot_id"],
                           "event": threading.Event(), "sent": False, "result": None}
                self.pending = pending
            if not pending["event"].wait(3):
                with self.lock:
                    self.error = "控制应答超时；结果未知。已停止通信，请检查设备并重启后台"
                    self.stop_event.set()
                    self.fail_pending("控制应答超时，结果未知")
                    self.store.event(self.error)
                raise ValueError(self.error)
            with self.lock:
                self.pending = None
                result = pending["result"]
            if not result["ok"]:
                raise ValueError(result.get("error", "开发板拒绝命令"))
            self.store.event("开发板已应答命令：" + action)
            return {"ok": True, "message": "开发板已确认；以随后上报状态为准"}
        finally:
            self.command_lock.release()

    def demo_publish(self):
        now = self.now()
        status = self.control.status(now)
        seq = self.status["seq"] + 1 if self.status else 1
        status.update({"v": 1, "type": "status", "boot_id": self.demo_boot, "seq": seq, "uptime_ms": now})
        self.accept(status)

    def scenario(self, name):
        if self.mode != "demo":
            raise ValueError("仅模拟模式提供场景按钮")
        with self.lock:
            self.control_revision += 1
            if name == "low":
                self.demo_humidity = 55
            elif name == "high":
                self.demo_humidity = 85
            elif name == "dry":
                self.demo_water = False
            elif name == "sensor":
                self.demo_sensor = False
            elif name == "temperature_low":
                self.demo_temperature = 16
            elif name == "temperature_high":
                self.demo_temperature = 28
            elif name == "co2_high":
                self.demo_co2 = 1800
            elif name == "co2_normal":
                self.demo_co2 = 600
            elif name == "co2_fault":
                self.demo_co2_sensor = False
            elif name in ("water_source_empty", "water_target_full", "nutrient_source_empty", "nutrient_target_full"):
                self.demo_liquids[("water_source_empty", "water_target_full", "nutrient_source_empty", "nutrient_target_full").index(name)] = False
            elif name == "offline":
                self.demo_connected = False
                self.error = "模拟断线；输出状态未知，板端将在最后心跳 10 秒后停机"
            elif name == "restore":
                self.demo_water = self.demo_sensor = self.demo_connected = True
                self.demo_liquids = [True, True, True, True]
                self.demo_co2_sensor = True
                self.demo_co2 = 700
                self.demo_temperature = 23.5
                self.control.stop("场景恢复，请重新启动", self.now())
            else:
                raise ValueError("未知演示场景")
            self.demo_tick(0)
            self.store.event("模拟场景：" + name)

    def demo_tick(self, elapsed):
        now = self.now()
        if self.demo_connected:
            self.control.heartbeat(now)
        change = (0.55 if self.control.mist else -0.035) - (0.10 if self.control.fan else 0)
        self.demo_humidity = max(30, min(95, self.demo_humidity + change * elapsed))
        self.demo_temperature += ((0.06 if self.control.climate.heater else 0)
                                  - (0.06 if self.control.climate.cooler else 0)) * elapsed
        self.demo_co2 = max(420, min(40000, self.demo_co2 + (-12 if self.control.fan else 0.2) * elapsed))
        self.control.climate.sample(round(self.demo_co2) if self.demo_co2_sensor else None, now)
        self.control.sample(round(self.demo_temperature, 2) if self.demo_sensor else None,
                            round(self.demo_humidity, 2) if self.demo_sensor else None, self.demo_water, now)
        self.control.liquids.sample(*self.demo_liquids, now)
        self.control.step(now)
        if self.demo_connected:
            self.demo_publish()

    def run(self):
        try:
            if self.mode == "demo":
                last = time.monotonic()
                while not self.stop_event.is_set():
                    current = time.monotonic()
                    with self.lock:
                        self.demo_tick(current - last)
                    last = current
                    self.stop_event.wait(1)
            else:
                self.serial_loop()
        except Exception as exc:
            with self.lock:
                self.error = "采集已停止：" + str(exc)
                self.fail_pending(self.error)
                self.store.event(self.error)
        finally:
            if self.serial:
                self.serial.close()

    def serial_loop(self):
        buffer = bytearray()
        discard = False
        beat_at = 0
        opened_at = time.monotonic()
        while not self.stop_event.is_set():
            data = self.serial.read(512)
            for byte in data:
                if byte == 10:
                    if not discard and buffer:
                        try:
                            value = json.loads(buffer.decode("utf-8"))
                            if isinstance(value, dict) and value.get("type") == "status":
                                self.accept(value)
                            elif isinstance(value, dict) and value.get("type") == "ack":
                                self.acknowledge(value)
                        except (ValueError, UnicodeError, TypeError):
                            pass  # Boot/REPL text and malformed frames never become telemetry.
                    buffer.clear()
                    discard = False
                elif not discard:
                    buffer.append(byte)
                    if len(buffer) > 4096:
                        buffer.clear()
                        discard = True
            with self.lock:
                if self.stop_event.is_set():
                    break
                current = time.monotonic()
                if current - (self.received or opened_at) > 4:
                    raise OSError("4 秒未收到有效状态。通信已关闭，检查后重启后台")
                if not self.status:
                    continue
                if current - beat_at >= 1:
                    self.write({"v": 1, "type": "heartbeat", "boot_id": self.status["boot_id"]})
                    beat_at = current
                p = self.pending
                if p and not p["sent"] and p["result"] is None:
                    self.write({"v": 1, "type": "command", "boot_id": self.status["boot_id"],
                                "id": p["id"], "expires_ms": self.status["uptime_ms"] + 4000,
                                "action": p["action"], "values": p["values"]})
                    p["sent"] = True

    def close(self):
        # On hardware, lease expiry is the guaranteed software stop path if delivery fails.
        if self.mode == "serial" and self.snapshot()["connected"] and not self.stop_event.is_set():
            try:
                self.command("stop")
            except (ValueError, OSError):
                pass
        self.stop_event.set()
        self.thread.join(timeout=4)
