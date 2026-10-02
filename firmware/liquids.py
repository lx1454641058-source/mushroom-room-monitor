"""Portable bounded liquid transfer. No volume estimates or unattended schedules."""
LIMITS = {"water": 30, "nutrient": 10}


def validate_liquid_command(values):
    if not isinstance(values, dict) or set(values) != {"pump", "seconds"}:
        raise ValueError("泵指令需要pump与seconds")
    pump, seconds = values["pump"], values["seconds"]
    if pump not in LIMITS or type(seconds) is not int or not 0 <= seconds <= LIMITS[pump]:
        raise ValueError("清水泵为0～30整数秒，营养液泵为0～10整数秒；0表示停止")
    return pump, seconds


class LiquidController:
    INPUT_MAX_AGE_MS = 1500
    REST_MS = 5000

    def __init__(self):
        self.inputs = {name: {"source_ok": False, "room_ok": False} for name in LIMITS}
        self.sample_at = -100000
        self.active = None
        self.until = 0
        self.off_at = {name: -100000 for name in LIMITS}
        self.reason = "尚未启动液体输送"

    def sample(self, water_source, water_room, nutrient_source, nutrient_room, now):
        values = (water_source, water_room, nutrient_source, nutrient_room)
        # Only an explicitly closed, validated input is treated as permissive.
        self.inputs = {name: {"source_ok": values[i * 2] is True,
                              "room_ok": values[i * 2 + 1] is True}
                       for i, name in enumerate(LIMITS)}
        self.sample_at = now

    def problem(self, pump, now, host_ok, sensor_ok):
        if not host_ok:
            return "电脑通信中断"
        if not sensor_ok:
            return "环境传感器无效，液体输送停止"
        if not 0 <= now - self.sample_at < self.INPUT_MAX_AGE_MS:
            return "液位输入过期"
        if not self.inputs[pump]["source_ok"]:
            return "储液不足或源液位开关断线"
        if not self.inputs[pump]["room_ok"]:
            return "接收容器已满或满液保护开关断线"
        return ""

    def stop(self, reason, now):
        if self.active:
            self.off_at[self.active] = now
        self.active, self.until, self.reason = None, 0, reason

    def command(self, values, now, host_ok, sensor_ok):
        pump, seconds = validate_liquid_command(values)
        if seconds == 0:
            if self.active == pump:
                self.stop("液体输送已手动停止", now)
            return
        if self.active:
            raise ValueError("已有泵运行，不能延长或叠加；请先停止")
        problem = self.problem(pump, now, host_ok, sensor_ok)
        if problem:
            raise ValueError(problem)
        if now - self.off_at[pump] < self.REST_MS:
            raise ValueError("泵停止后至少等待5秒再启动")
        self.active = pump
        self.until = now + seconds * 1000
        self.reason = "限时输送中；不代表已送达指定体积"

    def step(self, now, host_ok, sensor_ok):
        if not self.active:
            return
        problem = self.problem(self.active, now, host_ok, sensor_ok)
        if problem:
            self.stop(problem + "；恢复后需重新下达指令", now)
        elif now >= self.until:
            self.stop("设定时间已到，泵已停止", now)

    def status(self, now):
        return {"water_pump": self.active == "water", "nutrient_pump": self.active == "nutrient",
                "water_source_ok": self.inputs["water"]["source_ok"],
                "water_room_ok": self.inputs["water"]["room_ok"],
                "nutrient_source_ok": self.inputs["nutrient"]["source_ok"],
                "nutrient_room_ok": self.inputs["nutrient"]["room_ok"],
                "remaining_ms": max(0, self.until - now) if self.active else 0,
                "fault": self.reason}

