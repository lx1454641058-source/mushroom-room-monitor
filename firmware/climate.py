"""Optional CO2 ventilation and bounded low-voltage thermal control."""
DEFAULTS = {"temperature_low": 18, "temperature_high": 24, "co2_low": 800, "co2_high": 1200}


def validate_climate_config(config):
    if type(config) is not dict or set(config) != set(DEFAULTS):
        raise ValueError("温控与CO2参数须包含四个规定字段")
    if any(type(v) not in (int, float) or not -1e6 < v < 1e6 for v in config.values()):
        raise ValueError("温控与CO2参数必须是有限数字")
    if not 10 <= config["temperature_low"] <= config["temperature_high"] - 4 <= 31:
        raise ValueError("温度下限至少10℃、上限最多35℃，上下限至少相差4℃")
    if not 400 <= config["co2_low"] <= config["co2_high"] - 100 <= 4900:
        raise ValueError("CO2下限至少400ppm、上限最多5000ppm，至少相差100ppm")
    return dict(config)


def validate_climate_command(values):
    if type(values) is not dict or values.get("operation") not in ("enable", "disable", "config"):
        raise ValueError("温控与CO2指令无效")
    if values["operation"] == "config":
        if set(values) != {"operation", "config"}:
            raise ValueError("温控配置字段无效")
        validate_climate_config(values["config"])
    elif set(values) != {"operation"}:
        raise ValueError("启停不接受附加参数")


class ClimateController:
    CO2_MAX_AGE_MS = 15000
    REST_MS = 30000
    MAX_ON_MS = 120000

    def __init__(self):
        self.config = dict(DEFAULTS)
        self.enabled = self.heater = self.cooler = self.ventilation = False
        self.co2 = None
        self.sample_at = -100000
        self.off_at = self.on_at = 0
        self.fault = "温控与CO2联动未启用"

    def sample(self, co2, now):
        valid = type(co2) in (int, float) and 0 < co2 <= 40000
        self.co2 = co2 if valid else None
        self.sample_at = now if valid else -100000

    def co2_ok(self, now):
        return self.co2 is not None and 0 <= now - self.sample_at < self.CO2_MAX_AGE_MS

    def problem(self, temperature, now):
        if not self.co2_ok(now):
            return "CO2传感器无效或数据过期"
        if type(temperature) not in (int, float) or not 5 < temperature < 40:
            return "温度无效或超出5～40℃保护范围"
        return ""

    def stop(self, reason, now):
        if self.heater or self.cooler:
            self.off_at = now
        self.enabled = self.heater = self.cooler = self.ventilation = False
        self.fault = reason

    def command(self, values, temperature, now):
        validate_climate_command(values)
        op = values["operation"]
        if op == "disable":
            self.stop("温控与CO2联动已关闭", now)
        elif op == "config":
            if self.enabled:
                raise ValueError("请先关闭温控与CO2联动再修改参数")
            self.config = validate_climate_config(values["config"])
        else:
            error = self.problem(temperature, now)
            if error:
                raise ValueError(error)
            self.enabled = True
            self.fault = ""

    def step(self, temperature, now):
        if not self.enabled:
            return ""
        error = self.problem(temperature, now)
        if not error and (self.heater or self.cooler) and now - self.on_at >= self.MAX_ON_MS:
            error = "升降温连续运行达到120秒上限；检查后人工重启"
        if error:
            self.stop(error, now)
            return error
        c = self.config
        if self.co2 >= c["co2_high"]:
            self.ventilation = True
        elif self.co2 <= c["co2_low"]:
            self.ventilation = False
        # One-degree stop band inside the user range; a shared rest timer also
        # prevents immediate direction reversal from heating to cooling.
        if (self.heater and temperature >= c["temperature_low"] + 1
                or self.cooler and temperature <= c["temperature_high"] - 1):
            self.heater = self.cooler = False
            self.off_at = now
        if not self.heater and not self.cooler and now - self.off_at >= self.REST_MS:
            self.heater = temperature <= c["temperature_low"]
            self.cooler = temperature >= c["temperature_high"]
            if self.heater or self.cooler:
                self.on_at = now
        self.fault = "CO2超限，强制通风" if self.ventilation else "联动正常"
        return ""

    def status(self, now):
        valid = self.co2_ok(now)
        return {"co2": self.co2 if valid else None, "co2_ok": valid,
                "enabled": self.enabled, "heater": self.heater, "cooler": self.cooler,
                "ventilation": self.ventilation, "config": dict(self.config), "fault": self.fault}
