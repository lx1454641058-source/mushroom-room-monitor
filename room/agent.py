"""Bounded natural-language planner and tool executor. No arbitrary code or GPIO access."""
import json
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from firmware.controller import validate_config
from firmware.liquids import validate_liquid_command
from firmware.climate import validate_climate_config, DEFAULTS as CLIMATE_DEFAULTS
from .maintenance import backup
from .report import build_report, NoRedirect

TOOLS = {
    "get_status": "查询当前环境和设备状态",
    "get_report": "生成运行统计报告",
    "start_system": "启动自动加湿及周期通风",
    "stop_system": "停止全部设备",
    "set_fan": "通风模式：on=开启30秒后恢复定时，off=关闭常规通风（CO2超限请求优先），schedule=恢复定时",
    "run_water_pump": "限时抽水到雾化水箱；seconds为0～30整数，0停止；先停止环境控制",
    "run_nutrient_pump": "限时释放营养液；seconds为0～10整数，0停止；先停止环境控制",
    "update_settings": "修改湿度上下限或通风周期和时长，只使用用户明确给出的数值",
    "enable_climate": "启用温控与CO2联动，须先启动环境控制",
    "disable_climate": "关闭温控与CO2联动，雾化与周期通风仍按环境设置工作",
    "update_climate": "修改温度下上限和CO2通风下上限，先关闭联动，只用用户明确给出的值",
    "backup_data": "备份当前模式完整数据库",
    "list_photos": "查询最近已保存照片",
    "camera_start": "开启本页面摄像头，需要浏览器权限",
    "camera_stop": "关闭本页面摄像头及定时拍照",
    "camera_capture": "使用已开启的摄像头拍照保存",
    "camera_auto_on": "开启本页面每5分钟拍照，需要先开启摄像头",
    "camera_auto_off": "停止本页面定时拍照",
    "camera_analyze": "分析页面选定照片的成熟度与疑似病害，须先填写菌种；不控制设备"
}
CLIENT_TOOLS = {name for name in TOOLS if name.startswith("camera_")}
WRITE_TOOLS = {"start_system", "stop_system", "set_fan", "update_settings", "run_water_pump", "run_nutrient_pump", "enable_climate", "disable_climate", "update_climate"}
LABELS = {**TOOLS, "set_fan": "调整通风", "update_settings": "更新控制参数",
          "run_water_pump": "清水泵限时控制", "run_nutrient_pump": "营养液泵限时控制"}
HELP = ("我能查询状态、生成报告、启动自动控制、全部停止、通风30秒、关闭或恢复定时通风、"
        "修改明确的湿度/通风参数、备份数据、查看照片和操作本页面摄像头。"
        "例如：把湿度下限设为65，上限设为75，然后启动系统。"
        "支持“启用温控与二氧化碳联动”和“识别选定照片”；液体输送先全部停止，再发送“抽水5秒”或“释放营养液3秒”。")
SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "properties": {
                "tool": {"type": "string", "enum": list(TOOLS)},
                "args": {"type": "object", "properties": {
                    "mode": {"type": "string", "enum": ["on", "off", "schedule"]},
                    "seconds": {"type": "integer"},
                    "humidity_low": {"type": "number"}, "humidity_high": {"type": "number"},
                    "fan_period_s": {"type": "number"}, "fan_on_s": {"type": "number"}
                    ,"temperature_low": {"type": "number"}, "temperature_high": {"type": "number"},
                    "co2_low": {"type": "number"}, "co2_high": {"type": "number"}
                }, "additionalProperties": False}
            }, "required": ["tool", "args"], "additionalProperties": False}},
        "clarification": {"type": "string", "description": "Only a question when actions is empty. Must be the empty string when actions is nonempty. Never explain actions here."}
    }, "required": ["actions", "clarification"], "additionalProperties": False
}


def normalized(text):
    return re.sub(r"[\s，。！!？?、,；;：:]+", "", text)


def parameter_plan(message):
    """Parse only complete, explicit templates; never extract an instruction from a larger sentence."""
    text = re.sub(r"\s+", "", message).rstrip("。！!")
    text = re.sub(r"^(请帮我|帮我|请|麻烦)", "", text)
    number = r"(-?\d+(?:\.\d+)?)"
    set_to = r"(?:设为|设置为|改为|改成|调到)"
    join = r"[，,、]?"
    start = r"(?:[，,]?(?:然后|并)(启动系统|启动自动控制|开启自动控制))?"
    patterns = [
        (r"(?:把)?湿度下限" + set_to + number + r"[%％]?" + join + r"(?:湿度)?上限" + set_to + number + r"[%％]?", ("humidity_low", "humidity_high")),
        (r"(?:把)?湿度上限" + set_to + number + r"[%％]?" + join + r"(?:湿度)?下限" + set_to + number + r"[%％]?", ("humidity_high", "humidity_low")),
        (r"(?:把)?湿度下限" + set_to + number + r"[%％]?", ("humidity_low",)),
        (r"(?:把)?湿度上限" + set_to + number + r"[%％]?", ("humidity_high",)),
        (r"(?:把)?通风周期" + set_to + number + r"秒" + join + r"每次通风(?:时长)?(?:" + set_to + r")?" + number + r"秒", ("fan_period_s", "fan_on_s")),
        (r"(?:把)?通风周期" + set_to + number + r"秒", ("fan_period_s",)),
        (r"(?:把)?每次通风时长" + set_to + number + r"秒", ("fan_on_s",)),
    ]
    for pattern, fields in patterns:
        match = re.fullmatch(pattern + start, text)
        if match:
            args = {key: float(match.group(i + 1)) for i, key in enumerate(fields)}
            actions = [{"tool": "update_settings", "args": args}]
            if match.group(len(fields) + 1):
                actions.append({"tool": "start_system", "args": {}})
            return {"actions": actions, "clarification": ""}
    return None


def direct_plan(message):
    text = normalized(message)
    text = re.sub(r"^(请帮我|帮我|请|麻烦)", "", text)
    liquid_match = re.fullmatch(r"(抽水|补水|释放营养液)(\d+)秒", message.strip())
    if liquid_match:
        name = "run_nutrient_pump" if liquid_match.group(1) == "释放营养液" else "run_water_pump"
        return {"actions": [{"tool": name, "args": {"seconds": int(liquid_match.group(2))}}], "clarification": ""}
    actions = {
        "启用温控与二氧化碳联动": ("enable_climate", {}), "启用温控与CO2联动": ("enable_climate", {}),
        "关闭温控与二氧化碳联动": ("disable_climate", {}), "关闭温控与CO2联动": ("disable_climate", {}),
        "识别选定照片": ("camera_analyze", {}), "分析成熟度和病害": ("camera_analyze", {}),
        "启动系统": ("start_system", {}), "启动自动控制": ("start_system", {}),
        "开启自动控制": ("start_system", {}), "全部停止": ("stop_system", {}),
        "停止系统": ("stop_system", {}), "关闭所有设备": ("stop_system", {}),
        "停止全部设备": ("stop_system", {}), "查询状态": ("get_status", {}),
        "查看状态": ("get_status", {}), "现在状态怎么样": ("get_status", {}),
        "查看当前参数": ("get_status", {}), "查询参数": ("get_status", {}),
        "为什么不出雾": ("get_status", {}), "为什么没有加湿": ("get_status", {}),
        "检查系统": ("get_status", {}),
        "停止抽水": ("run_water_pump", {"seconds": 0}), "停止营养液泵": ("run_nutrient_pump", {"seconds": 0}),
        "生成报告": ("get_report", {}), "生成统计报告": ("get_report", {}),
        "通风30秒": ("set_fan", {"mode": "on"}), "开启风机30秒": ("set_fan", {"mode": "on"}),
        "关闭风机": ("set_fan", {"mode": "off"}), "停止通风": ("set_fan", {"mode": "off"}),
        "恢复定时通风": ("set_fan", {"mode": "schedule"}),
        "备份数据": ("backup_data", {}), "查看照片": ("list_photos", {}),
        "开启摄像头": ("camera_start", {}), "关闭摄像头": ("camera_stop", {}),
        "拍照": ("camera_capture", {}), "拍照保存": ("camera_capture", {}),
        "开启定时拍照": ("camera_auto_on", {}), "关闭定时拍照": ("camera_auto_off", {})
    }
    if text in actions:
        tool, args = actions[text]
        return {"actions": [{"tool": tool, "args": args}], "clarification": ""}
    climate = re.fullmatch(r"(?:把)?(温度|二氧化碳|CO2)下限设为(\d+(?:\.\d+)?)(?:℃|度|ppm)?[，,]?上限设为(\d+(?:\.\d+)?)(?:℃|度|ppm)?", message.strip())
    if climate:
        prefix = "temperature" if climate.group(1) == "温度" else "co2"
        return {"actions": [{"tool": "update_climate", "args": {prefix + "_low": float(climate.group(2)), prefix + "_high": float(climate.group(3))}}], "clarification": ""}
    return parameter_plan(message)


def is_explanation(message):
    # Do not let a model turn negations, hypothetical requests or how-to questions into writes.
    return bool(re.search(r"不要|(?<!识)别|不许|禁止|不能|不启动|不关闭|不停止|如果|假如|要是|怎么|如何|能否|能不能|可以吗|会怎样|会怎么样|解释|介绍|建议|告诉我怎么|不是|取消", message))


def model_plan(message, state, model):
    payload = {"model": model, "stream": False, "think": False, "format": SCHEMA,
               "messages": [
                   {"role": "system", "content": "你是蘑菇房指令规划器。只返回符合JSON的计划，不输出思考。工具：" +
                    json.dumps(TOOLS, ensure_ascii=False) +
                    "查询不能变成操作。所有工具均须对应用户明确要求，不主动加操作。"
                    "不根据蘑菇品种猜参数。update_settings只含用户指定的字段，不填其他值。"
                    "on风机只支持30秒，定时拍照只支持5分钟；其他手动时长须澄清。"
                    "使用秒为单位，但只能使用用户给出的原始数值，不进行单位换算；非秒请澄清。"
                    "摄像头操作一次只能一个动作。不能关闭保护、修改程序、联网或操作未实现设备。"
                    "含否定/假设/询问方法时不操作。无法确定时actions为空并询问澄清。最多4步。"
                    "clarification仅用于缺少必要信息时提问。有actions时clarification必须严格为一个空字符串，不能写解释或完成声明。"
                    "参数与当前值不同是正常修改，不需要再次确认。每次通风10秒是fan_on_s:10，不能写成1。"
                    '例：把湿度下限设为60，上限设为70，然后启动系统 => {"actions":[{"tool":"update_settings","args":{"humidity_low":60,"humidity_high":70}},{"tool":"start_system","args":{}}],"clarification":""}。'
                    '例：把通风周期设为90秒，每次通风20秒 => {"actions":[{"tool":"update_settings","args":{"fan_period_s":90,"fan_on_s":20}}],"clarification":""}。'},
                   {"role": "user", "content": json.dumps({"instruction": message, "current_config": state.get("status", {}).get("config", {})}, ensure_ascii=False) + "\n/no_think"}],
               "options": {"temperature": 0, "num_predict": 600, "num_ctx": 4096}}
    request = urllib.request.Request("http://127.0.0.1:11435/api/chat", data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=25) as response:
        raw = response.read(131073)
    if len(raw) > 131072:
        raise ValueError("模型计划超过大小限制")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("done") is not True or value.get("done_reason") == "length":
        raise ValueError("模型计划未完整生成")
    message_value = value.get("message")
    if not isinstance(message_value, dict):
        raise ValueError("模型响应格式错误")
    return json.loads(message_value.get("content", ""))


def validate_plan(plan, message, config, climate_config=None):
    if not isinstance(plan, dict) or set(plan) != {"actions", "clarification"}:
        raise ValueError("计划结构不正确")
    actions = plan["actions"]
    if not isinstance(actions, list) or len(actions) > 4 or not isinstance(plan["clarification"], str):
        raise ValueError("计划步骤无效")
    if actions and plan["clarification"].strip():
        raise ValueError("计划尚有未明确事项，未执行")
    if any(not isinstance(step, dict) or set(step) != {"tool", "args"} for step in actions):
        raise ValueError("工具步骤格式错误")
    current = dict(config)
    seen = set()
    numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", message)]
    for step in actions:
        name, args = step["tool"], step["args"]
        if not isinstance(name, str) or name not in TOOLS or not isinstance(args, dict):
            raise ValueError("工具不在允许列表内")
        if name in seen:
            raise ValueError("同一请求不重复调用相同工具，请拆成两次操作")
        seen.add(name)
        if name in WRITE_TOOLS and re.search(r"查询|查看|看看|是否|记录|报告|是多少", message):
            raise ValueError("查询与控制请分开发送，避免将查询误当作设备操作")
        if name == "update_climate":
            if not args or not set(args) <= set(CLIMATE_DEFAULTS) or not re.search(r"设|改|调", message):
                raise ValueError("温控与CO2参数无效或没有明确修改指令")
            for key, value in args.items():
                if type(value) not in (int, float) or value not in numbers:
                    raise ValueError("参数必须来自明确输入数字")
                subject = r"温度|温控" if key.startswith("temperature") else r"二氧化碳|CO2"
                if not re.search(subject, message):
                    raise ValueError("未明确对应的温度或CO2参数")
                if re.search(r"华氏|℉|百分|%", message):
                    raise ValueError("温度使用摄氏度，CO2使用ppm")
            validate_climate_config({**(climate_config or CLIMATE_DEFAULTS), **args})
        elif name == "update_settings":
            if not args or not set(args) <= set(current):
                raise ValueError("参数字段无效")
            if not re.search(r"设|改|调", message):
                raise ValueError("未明确要求修改参数")
            for key, value in args.items():
                if type(value) not in (int, float) or value not in numbers:
                    raise ValueError("参数必须来自你明确输入的数字")
                if key.startswith("humidity") and not re.search(r"湿度|加湿", message):
                    raise ValueError("没有明确指定湿度参数")
                if key.startswith("fan") and not re.search(r"通风|风机", message):
                    raise ValueError("没有明确指定通风参数")
                if key.startswith("fan") and re.search(r"分钟|小时", message):
                    raise ValueError("通风参数请直接使用秒，例如：把通风周期设为60秒，每次通风10秒")
            current.update(args)
            validate_config(current)
        elif name in ("run_water_pump", "run_nutrient_pump"):
            if set(args) != {"seconds"}:
                raise ValueError("液体指令必须给出秒数")
            pump = "water" if name == "run_water_pump" else "nutrient"
            validate_liquid_command({"pump": pump, "seconds": args["seconds"]})
            subject = r"抽水|补水|清水泵" if pump == "water" else r"营养液"
            if not re.search(subject, message):
                raise ValueError("没有明确指定对应的泵")
            if args["seconds"] == 0:
                if not re.search(r"停止|关闭", message):
                    raise ValueError("停泵请明确输入停止抽水或停止营养液泵")
            elif (not re.search(r"(?:抽水|补水|释放|启动|开启)", message)
                  or not re.search(r"(?<![\d.])" + str(args["seconds"]) + r"\s*秒", message)
                  or re.search(r"分钟|小时|毫升|ml|每天|每隔|定时|循环|自动", message, re.I)):
                raise ValueError("液体输送仅支持单次整数秒指令，不自动换算体积或循环投加")
        elif name == "set_fan":
            if set(args) != {"mode"} or args["mode"] not in ("on", "off", "schedule"):
                raise ValueError("通风模式无效")
            if not re.search(r"通风|风机", message):
                raise ValueError("未明确要求操作通风")
            mode_intents = {"on": r"开|启动|通风30秒", "off": r"关|停", "schedule": r"定时|周期"}
            if not re.search(mode_intents[args["mode"]], message):
                raise ValueError("通风模式与明确指令不匹配")
            if args["mode"] == "on" and (re.search(r"分钟|小时", message) or any(n != 30 for n in numbers)):
                raise ValueError("手动通风仅支持30秒；其他时长请使用通风周期参数")
        elif args:
            raise ValueError("该工具不接受参数")
        gates = {
            "enable_climate": r"(启用|开启).*(温控).*(二氧化碳|CO2)",
            "disable_climate": r"(关闭|停止).*(温控).*(二氧化碳|CO2)",
            "camera_analyze": r"(识别|分析).*(照片|成熟度|病害)",
            "start_system": r"(启动|开启|打开|运行).*(系统|自动控制|自动调节)",
            "stop_system": r"(全部停止|全停|停止系统|关闭系统|关闭所有设备|停止全部设备)",
            "backup_data": r"备份", "list_photos": r"照片|影像|图片",
            "camera_start": r"(开|启用).*(摄像头|相机)",
            "camera_stop": r"(关|停止).*(摄像头|相机)",
            "camera_capture": r"拍照|拍一张",
            "camera_auto_on": r"(开|启用).*(定时|自动).*(拍照|拍摄)",
            "camera_auto_off": r"(关|停止).*(定时|自动).*(拍照|拍摄)"
        }
        if name in gates and not re.search(gates[name], message):
            raise ValueError("计划中的操作没有对应的明确指令，请换一种说法")
        if name == "camera_auto_on" and (numbers or re.search(r"秒|小时", message)):
            if not (numbers == [5] and "分钟" in message):
                raise ValueError("当前定时拍照只支持每5分钟一次")
    if "start_system" in seen and "stop_system" in seen:
        raise ValueError("启动和停止请分两次发送，避免相互冲突")
    if seen & {"run_water_pump", "run_nutrient_pump"} and len(actions) != 1:
        raise ValueError("泵指令请单独发送，先全部停止环境控制，再进行液体输送")
    if seen & CLIENT_TOOLS and len(actions) != 1:
        raise ValueError("摄像头操作请单独发送，完成后再进行下一项")
    if seen & {"enable_climate", "disable_climate", "update_climate"} and len(actions) != 1:
        raise ValueError("温控与CO2指令请单独发送，逐步核对参数与状态")
    return actions


def config_text(config):
    return "湿度下限 %g%%RH，上限 %g%%RH；通风周期 %g 秒，每次 %g 秒" % (
        config["humidity_low"], config["humidity_high"], config["fan_period_s"], config["fan_on_s"])


def status_text(snapshot):
    if not snapshot["connected"]:
        return "当前通信异常，温湿度及设备输出状态未知。" + snapshot.get("error", "")
    s = snapshot["status"]
    reading = ("温度 %.1f℃，湿度 %.1f%%RH。" % (s["temperature"], s["humidity"])
               if s["sensor_ok"] else "温湿度传感器异常，当前读数无效。")
    overview = reading + "自动控制%s；加湿指令%s；通风指令%s；%s。%s" % (
        "已启用" if s["enabled"] else "已停止", "开启" if s["mist"] else "关闭",
        "开启" if s["fan"] else "关闭", "水位正常" if s["water_ok"] else "缺水或水位断线", s["fault"])
    config = s["config"]
    if not s["water_ok"]:
        reason = "缺水或水位信号异常，先检查储水量和水位开关；恢复后需重新启动。"
    elif not s["sensor_ok"]:
        reason = "温湿度数据无效，先检查传感器；恢复后需重新启动。"
    elif not s["enabled"]:
        reason = "系统处于停止状态，加湿与通风均不输出。检查当前故障提示后，可发送“启动系统”。"
    elif s["mist"]:
        reason = "已发出加湿指令；达到湿度上限或触发保护时停止。指令开启不能证明实物已经出雾。"
    elif s["humidity"] >= config["humidity_high"]:
        reason = "湿度已达到停止上限，当前无需加湿；降到启动下限后才重新启动。"
    elif s["humidity"] > config["humidity_low"]:
        reason = "湿度处于上下限之间，保持加湿关闭，避免频繁启停。"
    else:
        reason = "湿度已满足加湿条件；可能仍在最短停机5秒的等待期。若持续未启动，请核对下一次状态和故障记录。"
    liquid = s.get("liquids")
    liquid_text = ("\n液体输送：清水泵%s，营养液泵%s；剩余%.1f秒；%s。" % (
        "运行" if liquid["water_pump"] else "停止", "运行" if liquid["nutrient_pump"] else "停止",
        liquid["remaining_ms"] / 1000, liquid["fault"])) if liquid else "\n当前设备未报告液体模块。"
    climate = s.get("climate")
    climate_text = "\n温控与CO2模块未报告。"
    if climate:
        climate_text = "\nCO2：%s ppm；联动%s，加热%s，制冷%s；%s；参数%s" % (
            climate["co2"] if climate["co2_ok"] else "未知", climate["enabled"], climate["heater"], climate["cooler"],
            climate["fault"], json.dumps(climate["config"], ensure_ascii=False))
    return overview + "\n当前参数：" + config_text(config) + "。\n状态说明：" + reason + liquid_text + climate_text


class RoomAgent:
    def __init__(self, runtime, store, model=None, planner=None):
        self.runtime, self.store, self.model = runtime, store, model
        self.planner = planner or model_plan
        self.busy = threading.Lock()
        self.cache_lock = threading.Lock()
        self.cache = OrderedDict()
        self.inflight = set()

    def handle(self, identifier, message):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,64}", identifier):
            raise ValueError("请求编号无效")
        if not isinstance(message, str) or not 1 <= len(message.strip()) <= 500:
            raise ValueError("请输入1～500字的指令")
        message = message.strip()
        with self.cache_lock:
            if identifier in self.cache:
                previous, result = self.cache[identifier]
                if previous != message:
                    raise ValueError("同一请求编号不能用于不同指令")
                return result
            if identifier in self.inflight:
                raise ValueError("相同请求正在执行，不能重复提交")
            self.inflight.add(identifier)
        fast = direct_plan(message)
        stopping = bool(fast and fast["actions"][0]["tool"] == "stop_system")
        acquired = self.busy.acquire(blocking=False)
        if not acquired and not stopping:
            with self.cache_lock:
                self.inflight.discard(identifier)
            raise ValueError("智能体正在处理上一条指令，请稍后再试；全部停止可立即发送")
        trace, response = [], {"id": identifier, "source": self.runtime.mode, "trace": []}
        before = self.runtime.snapshot()
        revision = before["control_revision"]
        boot = before["status"].get("boot_id")
        deadline = time.monotonic() + 35
        try:
            self.store.event("智能体收到指令：" + message)
            if re.search(r"绕过|(?:关闭|禁用|忽略).{0,10}保护|删除|执行脚本|修改代码", message):
                response.update(ok=False, text="未操作设备。这条指令包含未开放或未实现的功能。\n" + HELP, planner="guard")
            elif is_explanation(message) and not (fast and fast["actions"][0]["tool"] == "get_status"):
                response.update(ok=True, text="这条消息包含询问、条件或否定表达，我没有操作设备。\n" + HELP, planner="guard")
            else:
                if fast:
                    plan, origin = fast, "明确指令快捷执行"
                elif self.model:
                    try:
                        plan = self.planner(message, before, self.model)
                    except OSError as exc:
                        raise ValueError("本地模型暂时无法连接或响应超时，本次未执行。可以先用“查询状态”“全部停止”，或按示例完整输入参数指令") from exc
                    origin = "本地模型 " + self.model
                else:
                    plan, origin = {"actions": [], "clarification": "未启用本地模型，复杂语句暂不能解析。\n" + HELP}, "规则指令"
                config = before["status"].get("config", self.runtime.control.config)
                actions = validate_plan(plan, message, config, (before["status"].get("climate") or {}).get("config"))
                response["planner"] = origin
                if not actions:
                    hint = "未启用本地模型，请按示例完整输入。" if not self.model else "请把操作对象、数值和单位写清楚。"
                    response.update(ok=True, text="未执行任何工具。" + hint + "\n" + HELP)
                else:
                    texts = []
                    for step in actions:
                        if time.monotonic() > deadline:
                            raise ValueError("计划已超时，剩余操作取消")
                        name, args = step["tool"], step["args"]
                        fresh = self.runtime.snapshot()
                        if name in WRITE_TOOLS:
                            if fresh["control_revision"] != revision or fresh["status"].get("boot_id") != boot:
                                raise ValueError("期间发生了其他控制操作或设备重启，剩余计划已取消")
                        if name in CLIENT_TOOLS:
                            response["client_action"] = name
                            text = "已交给当前页面处理，尚未确认摄像头操作结果。"
                        elif name == "get_status":
                            text = status_text(fresh)
                        elif name == "get_report":
                            text = build_report(self.runtime, self.store)["text"]
                        elif name == "backup_data":
                            result = backup(self.store.directory.parent, self.runtime.mode)
                            text = "备份完成：" + result["path"] + "\n记录：" + json.dumps(result["counts"], ensure_ascii=False)
                            if result.get("vision_path"):
                                text += "\n视觉记录：" + result["vision_path"]
                        elif name == "list_photos":
                            photos = self.store.photos()
                            response["photos"] = photos
                            text = "查询到最近 %s 张照片。可在生长影像区查看。" % len(photos)
                        else:
                            action = {"start_system": "start", "stop_system": "stop", "set_fan": "fan", "update_settings": "config", "run_water_pump": "liquid", "run_nutrient_pump": "liquid", "enable_climate": "climate", "disable_climate": "climate", "update_climate": "climate"}[name]
                            values = None
                            if name == "set_fan":
                                values = args["mode"]
                            elif name == "update_settings":
                                values = {**fresh["status"]["config"], **args}
                            elif action == "liquid":
                                values = {"pump": "water" if name == "run_water_pump" else "nutrient", "seconds": args["seconds"]}
                            elif action == "climate":
                                values = {"operation": {"enable_climate": "enable", "disable_climate": "disable", "update_climate": "config"}[name]}
                                if name == "update_climate":
                                    values["config"] = {**fresh["status"]["climate"]["config"], **args}
                            result = self.runtime.command(action, values, expected_revision=revision, expected_boot=boot)
                            revision += 1
                            text = result["message"]
                            if name == "update_settings":
                                text += "；" + config_text(values)
                            if name == "set_fan" and args["mode"] == "on":
                                text += "；手动通风30秒后恢复定时"
                            if name == "start_system":
                                text += "；加湿由湿度阈值决定，不代表立即出雾"
                            if action == "liquid":
                                text += "；" + ("已请求停泵" if args["seconds"] == 0 else "限时%s秒，到时自动停止；未测量实际输送体积" % args["seconds"])
                            if action == "climate":
                                text += "；" + json.dumps(values, ensure_ascii=False) + "；CO2联动优先于风机手动关闭"
                        entry = {"tool": name, "label": LABELS[name], "args": args, "ok": True, "result": text}
                        if name in CLIENT_TOOLS:
                            entry["pending"] = True
                        trace.append(entry)
                        texts.append(LABELS[name] + "：\n" + text)
                        self.store.event("智能体步骤：" + name + "；" + text[:500])
                    response.update(ok=True, text="\n\n".join(texts))
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
            response.update(ok=False, text=("已完成 %s 步；" % len(trace) if trace else "未完成操作；") + str(exc) + "。未执行的步骤不会自动重试。")
            self.store.event("智能体未完成：" + str(exc))
        finally:
            if acquired:
                self.busy.release()
        response["trace"] = trace
        response["status"] = self.runtime.snapshot()
        with self.cache_lock:
            self.cache[identifier] = (message, response)
            self.inflight.discard(identifier)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
        return response

    def complete_client(self, identifier, action, ok, detail):
        if type(ok) is not bool or not isinstance(detail, str) or len(detail) > 500:
            raise ValueError("页面执行结果格式错误")
        with self.cache_lock:
            cached = self.cache.get(identifier)
            if not cached:
                raise ValueError("请求已过期")
            result = cached[1]
            if result.get("client_action") != action:
                raise ValueError("没有匹配的待处理页面操作")
            result.pop("client_action")
            result["ok"] = ok
            result["text"] = detail
            result["trace"][-1].update(ok=ok, pending=False, result=detail)
            self.store.event("智能体页面操作：%s；%s；%s" % (action, "成功" if ok else "失败", detail))
            return result
