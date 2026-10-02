"""Original, portable control logic. No PC-only dependencies; time unit: ms."""
try:
    from .liquids import LiquidController
    from .climate import ClimateController, validate_climate_command
except ImportError:
    from liquids import LiquidController
    from climate import ClimateController, validate_climate_command

DEFAULTS = {"humidity_low": 65, "humidity_high": 72,
            "fan_period_s": 300, "fan_on_s": 15}


def number(value):
    return type(value) in (int, float) and -1e12 < value < 1e12


def validate_config(value):
    if type(value) is not dict or set(value) != set(DEFAULTS):
        raise ValueError("参数必须包含四个规定字段")
    if not all(number(v) for v in value.values()):
        raise ValueError("参数必须是有限数字")
    if not 20 <= value["humidity_low"] < value["humidity_high"] <= 95:
        raise ValueError("湿度要求：20 ≤ 下限 < 上限 ≤ 95")
    if value["humidity_high"] - value["humidity_low"] < 2:
        raise ValueError("上下限至少相差 2，避免频繁启停")
    if not 30 <= value["fan_period_s"] <= 3600:
        raise ValueError("通风周期范围为 30～3600 秒")
    if not 1 <= value["fan_on_s"] <= min(120, value["fan_period_s"] - 5):
        raise ValueError("通风时长为 1～120 秒，且至少比周期短 5 秒")
    return dict(value)


class Controller:
    LEASE_MS = 10000
    SENSOR_MS = 6000
    MIN_OFF_MS = 5000
    MAX_MIST_MS = 120000

    def __init__(self):
        self.liquids = LiquidController()
        self.climate = ClimateController()
        self.config = dict(DEFAULTS)
        self.enabled = False
        self.mist = False
        self.fan = False
        self.water_ok = False
        self.temperature = None
        self.humidity = None
        self.sensor_at = -100000
        self.heartbeat_at = -100000
        self.mist_at = 0
        self.off_at = 0
        self.cycle_at = 0
        self.manual_until = 0
        self.fan_mode = "schedule"
        self.fault = "尚未启动"

    def heartbeat(self, now):
        self.heartbeat_at = now

    def sample(self, temperature, humidity, water_ok, now):
        self.water_ok = water_ok is True
        valid = (number(temperature) and -40 <= temperature <= 85
                 and number(humidity) and 0 <= humidity <= 100)
        self.temperature = temperature if valid else None
        self.humidity = humidity if valid else None
        self.sensor_at = now if valid else -100000

    def stop(self, reason, now):
        self.liquids.stop(reason, now)
        self.climate.stop(reason, now)
        self.enabled = False
        if self.mist:
            self.off_at = now
        self.mist = False
        self.fan = False
        self.manual_until = 0
        self.fault = reason

    def problem(self, now):
        if now - self.heartbeat_at >= self.LEASE_MS:
            return "电脑通信中断"
        if not self.water_ok:
            return "缺水或水位开关断线"
        if self.humidity is None or now - self.sensor_at >= self.SENSOR_MS:
            return "温湿度传感器异常或数据过期"
        return ""

    def command(self, action, values, now):
        if action == "stop":
            self.stop("已停止", now)
        elif action == "start":
            if self.liquids.active:
                raise ValueError("请等待液体输送结束或先全部停止，再启动环境控制")
            error = self.problem(now)
            if error:
                raise ValueError(error)
            if not self.enabled:
                self.enabled = True
                self.cycle_at = now
                self.fan_mode = "schedule"
                self.fault = ""
        elif action == "liquid":
            if not isinstance(values, dict):
                raise ValueError("液体指令参数无效")
            if self.enabled and values.get("seconds") != 0:
                raise ValueError("请先全部停止环境控制，再进行限时输送")
            self.liquids.command(values, now, now - self.heartbeat_at < self.LEASE_MS,
                                 self.humidity is not None and now - self.sensor_at < self.SENSOR_MS)
        elif action == "climate":
            validate_climate_command(values)
            if values["operation"] == "enable":
                if not self.enabled:
                    raise ValueError("请先启动环境控制，再启用温控与CO2联动")
                error = self.problem(now)
                if error:
                    raise ValueError(error)
            self.climate.command(values, self.temperature, now)
        elif action == "config":
            self.config = validate_config(values)
            self.cycle_at = now
        elif action == "fan":
            if not self.enabled:
                raise ValueError("请先启动系统")
            if values not in ("schedule", "on", "off"):
                raise ValueError("未知通风模式")
            self.fan_mode = values
            self.manual_until = now + 30000 if values == "on" else 0
            if values == "schedule":
                self.cycle_at = now
        else:
            raise ValueError("未知控制命令")
        self.step(now)

    def step(self, now):
        self.liquids.step(now, now - self.heartbeat_at < self.LEASE_MS,
                          self.humidity is not None and now - self.sensor_at < self.SENSOR_MS)
        if not self.enabled:
            self.mist = self.fan = False
            return
        error = self.problem(now)
        if error:
            self.stop(error + "；恢复后需重新启动", now)
            return
        if self.mist and now - self.mist_at >= self.MAX_MIST_MS:
            self.stop("连续加湿超过 120 秒；检查后重新启动", now)
            return
        if self.mist and self.humidity >= self.config["humidity_high"]:
            self.mist = False
            self.off_at = now
        elif (not self.mist and self.humidity <= self.config["humidity_low"]
              and now - self.off_at >= self.MIN_OFF_MS):
            self.mist = True
            self.mist_at = now
        if self.fan_mode == "on" and now >= self.manual_until:
            self.fan_mode = "schedule"
            self.cycle_at = now
        if self.fan_mode == "schedule":
            phase = (now - self.cycle_at) % (self.config["fan_period_s"] * 1000)
            self.fan = phase < self.config["fan_on_s"] * 1000
        else:
            self.fan = self.fan_mode == "on"
        error = self.climate.step(self.temperature, now)
        if error:
            self.stop(error + "；恢复后需重新启动", now)
            return
        self.fan = self.fan or self.climate.ventilation

    def status(self, now):
        valid = self.humidity is not None and now - self.sensor_at < self.SENSOR_MS
        return {"enabled": self.enabled, "mist": self.mist, "fan": self.fan,
                "water_ok": self.water_ok, "sensor_ok": valid,
                "temperature": self.temperature if valid else None,
                "humidity": self.humidity if valid else None,
                "fault": self.fault, "fan_mode": self.fan_mode,
                "config": dict(self.config), "liquids": self.liquids.status(now),
                "climate": self.climate.status(now)}
