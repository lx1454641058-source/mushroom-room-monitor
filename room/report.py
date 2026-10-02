import json
import urllib.request
from datetime import datetime
from .storage import timestamp


def build_report(runtime, store):
    rows = store.history(3600)
    valid = [r for r in rows if r.get("sensor_ok") and r.get("humidity") is not None]
    mode = "模拟演示" if runtime.mode == "demo" else "实物串口"
    current = runtime.snapshot()
    # Use observed timestamps; a row count is neither elapsed time nor fault duration.
    times = [datetime.fromisoformat(row["at"]) for row in rows]
    intervals = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    span = max(0, (times[-1] - times[0]).total_seconds()) if times else 0
    gaps = sum(delta > 4 for delta in intervals)
    clock_reversals = sum(delta < 0 for delta in intervals)
    stats = {"samples": len(rows), "valid_samples": len(valid),
             "humidity_min": min((r["humidity"] for r in valid), default=None),
             "humidity_max": max((r["humidity"] for r in valid), default=None),
             "humidity_mean": round(sum(r["humidity"] for r in valid) / len(valid), 2) if valid else None,
             "temperature_mean": round(sum(r["temperature"] for r in valid) / len(valid), 2) if valid else None,
             "dry_samples": sum(r.get("water_ok") is False for r in rows),
             "span_seconds": round(span, 3), "gap_count": gaps, "clock_reversals": clock_reversals}
    lines = ["蘑菇房运行记录摘要", "生成时间（UTC）：" + timestamp(), "数据来源：" + mode,
             "报告类型：规则统计报告，未调用大模型", "范围：最近最多 3600 条记录，以实际起止时间为准，不代表完整一天。"]
    if rows:
        lines += ["记录区间（UTC）：%s ～ %s" % (rows[0]["at"], rows[-1]["at"]),
                  "证据：样本 #%s～#%s，共 %s 条，有效 %s 条。" % (rows[0]["id"], rows[-1]["id"], len(rows), len(valid))]
        lines += ["首末记录时间跨度：%.1f 秒；相邻记录间隔超过 4 秒：%s 处；时钟倒退：%s 处。" % (span, gaps, clock_reversals),
                  "时间跨度包含采集空白，不代表设备连续运行时长；统计均值按有效记录计算。"]
    if valid:
        lines += ["湿度：最低 %.2f%%RH，最高 %.2f%%RH，平均 %.2f%%RH。" % (stats["humidity_min"], stats["humidity_max"], stats["humidity_mean"]),
                  "平均温度：%.2f℃。温控状态以扩展模块记录为准；指令不代表实测制热或制冷能力。" % stats["temperature_mean"]]
    else:
        lines.append("尚无有效温湿度数据，不能评价环境情况。")
    lines += ["缺水状态记录：%s 条（记录条数，不等于故障次数或持续秒数）。" % stats["dry_samples"],
              "当前通信：" + ("正常" if current["connected"] else "异常，当前输出状态未知"),
              "当前提示：" + ((current["status"].get("fault") or "无") if current["connected"] else (current.get("error") or "当前状态未知")),
              "本报告不判断蘑菇长势、病害或营养需求；阈值由人工结合品种与阶段设置。"]
    liquid_rows = [r["liquids"] for r in rows if isinstance(r.get("liquids"), dict)]
    stats["liquid_samples"] = len(liquid_rows)
    stats["water_pump_samples"] = sum(r["water_pump"] for r in liquid_rows)
    stats["nutrient_pump_samples"] = sum(r["nutrient_pump"] for r in liquid_rows)
    climate_rows = [r["climate"] for r in rows if isinstance(r.get("climate"), dict)]
    co2_rows = [r["co2"] for r in climate_rows if r["co2_ok"]]
    stats["co2_valid_samples"] = len(co2_rows)
    stats["co2_max_ppm"] = max(co2_rows, default=None)
    lines.append("CO2有效记录%s条，最高%s ppm；加热输出记录%s条，制冷输出记录%s条。" % (
        len(co2_rows), stats["co2_max_ppm"] if co2_rows else "未知",
        sum(r["heater"] for r in climate_rows), sum(r["cooler"] for r in climate_rows)))
    lines.append("成熟度与疑似病害识别由独立图像入口生成，未纳入本环境统计；模型结果待人工复核。")
    lines.append("液体模块记录：%s条，清水泵开启记录%s条，营养液泵开启记录%s条；记录数不代表运行次数、持续时间或输送体积。" % (stats["liquid_samples"], stats["water_pump_samples"], stats["nutrient_pump_samples"]))
    return {"generated_at": timestamp(), "source": runtime.mode, "kind": "rule_statistics",
            "stats": stats, "current": current, "text": "\n".join(lines)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("本地分析接口不允许重定向")


def explain_report(report, model):
    if not model:
        raise ValueError("未启用本地模型；规则统计报告可以直接使用")
    stats = report.get("stats", {})
    current = report.get("current", {})
    candidates = {
        "verify_settings": "人工核对控制阈值与已确认的栽培方案；演示默认值不等于实际栽培要求。"
    }
    if report["source"] == "demo":
        candidates["collect_real_data"] = "当前为模拟数据。接好实物并完成联调后，再采集用于论文的实测记录。"
    if stats.get("dry_samples", 0) > 0:
        candidates["check_water"] = "已有缺水状态记录，请人工检查储水量和浮球开关；记录条数不能直接当作缺水次数。"
    if stats.get("valid_samples", 0) < stats.get("samples", 0):
        candidates["check_sensor"] = "已有无效采样记录，请人工检查温湿度传感器连接与供电。"
    if not current.get("connected", False):
        candidates["check_link"] = "当前连接异常，请检查通信链路；恢复连接前，不能确认设备的当前输出状态。"
    # Constrained selection: no model-written facts, measurement values or control tools.
    payload = {
        "model": model, "stream": False, "think": False,
        "format": {"type": "object", "properties": {"recommendations": {
            "type": "array", "items": {"type": "string", "enum": list(candidates)},
            "minItems": 1, "maxItems": 3}}, "required": ["recommendations"], "additionalProperties": False},
        "messages": [
            {"role": "system", "content": "根据运行证据，选择1至3个最有用的人工核查建议。仅输出JSON对象recommendations，值是给定候选ID数组。不得输出说明、思考、数据或候选之外的内容。"},
            {"role": "user", "content": json.dumps({"evidence": report["text"], "candidates": candidates}, ensure_ascii=False) + "\n/no_think"}],
        "options": {"temperature": 0, "num_predict": 250, "num_ctx": 4096}}
    request = urllib.request.Request("http://127.0.0.1:11435/api/chat", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=45) as response:
        raw = response.read(131073)
    if len(raw) > 131072:
        raise ValueError("模型输出超出限制")
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("message"), dict):
        raise ValueError("模型响应结构错误；请使用规则统计报告")
    if value.get("done") is not True or value.get("done_reason") == "length":
        raise ValueError("模型回答未完成；请使用规则统计报告")
    try:
        result = json.loads(value["message"].get("content", ""))
        choices = result["recommendations"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("模型返回格式不符合要求；请使用规则统计报告") from exc
    if (not isinstance(choices, list) or not 1 <= len(choices) <= 3 or
        any(not isinstance(item, str) or item not in candidates for item in choices) or
        len(set(choices)) != len(choices)):
        raise ValueError("模型选择了无证据支持的建议；请使用规则统计报告")
    content = "本地模型推荐的人工核查事项（依据当前记录）：\n\n"
    content += "\n".join("• " + candidates[item] for item in choices)
    return {"kind": "local_model_selection", "model": model, "text": content, "recommendations": choices, "evidence": report}

