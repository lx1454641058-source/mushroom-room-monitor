# 串口与本地接口

## 六路控制扩展（2026-09-26）

新固件的状态增加可选 `climate` 对象：`co2`（ppm或null）、`co2_ok`、`enabled`、`heater`、`cooler`、`ventilation`（后五项布尔值）、`config` 和 `fault`。config 包含 temperature_low、temperature_high、co2_low、co2_high；默认18、24、800、1200。加热与制冷不能同时开启；CO2通风请求有效时fan必须开启。旧报文仍可读取，但缺少此字段时不能执行climate命令；断线时该对象显示null。

新增 `action=climate`，values为 `{"operation":"enable"}`、`{"operation":"disable"}` 或 `{"operation":"config","config":{"temperature_low":18,"temperature_high":24,"co2_low":800,"co2_high":1200}}`。config必须完整：温度10至35℃、上下限相差至少4℃；CO2为400至5000ppm、相差至少100ppm。联动关闭时才可修改。启用还要求环境控制已启动且读数有效。升降温间隔30秒、单次连续输出最多120秒；CO2读数15秒过期。详细联动规则见EXTENSIONS.md。

HTTP新增 POST `/api/vision`，JSON为 `{"photo_id":"已存照片的32位编号","species":"实际菌种"}`。仅分析现存照片，模型未配置或不可用时返回明确错误；结果保存在独立JSON文件，不更改原数据库结构。GET `/api/status` 新增vision_model配置名，它不代表模型已经下载或通过效果验收。客户端识别结果仍通过原client-result接口确认。

最新固件需同时上传 main.py、controller.py、liquids.py、climate.py、aht20.py、scd4x.py 六个文件。下方原四路协议描述保留作基础说明；fan_mode=off仅关闭常规通风，不能覆盖已启用的CO2超限请求。

## 物理链路

经典 ESP32 USB 转串口，115200 波特率、8 数据位、无校验、1 停止位。每行一个 UTF-8 JSON 对象，以换行结束。电脑发送 ASCII 转义形式的 JSON。启动日志、REPL 文字、非法 JSON 不计作有效采样。电脑每秒发一次心跳；板端每两秒测量、每秒上报状态。

## 板端状态

```json
{"v":1,"type":"status","boot_id":"ab12cd34","seq":1,"uptime_ms":5000,"temperature":23.5,"humidity":61.2,"sensor_ok":true,"water_ok":true,"enabled":false,"mist":false,"fan":false,"fault":"尚未启动","fan_mode":"schedule","config":{"humidity_low":65,"humidity_high":72,"fan_period_s":300,"fan_on_s":15}}
```

- `boot_id`：每次开发板启动随机变化的标识；`seq`：递增报文序号，重复或倒序报文不刷新在线状态。
- `uptime_ms`：开机后累计毫秒，使用 ticks_diff 处理硬件时钟回绕。
- `mist/fan`：板端发出的输出指令，不能证明负载实际动作。
- 传感器失效时 `sensor_ok=false`，温度、湿度必须同时为 null。
- `fan_mode`：schedule 周期、on 手动 30 秒、off 保持关闭。

## 电脑到板端

心跳：

```json
{"v":1,"type":"heartbeat","boot_id":"ab12cd34"}
```

控制命令：

```json
{"v":1,"type":"command","boot_id":"ab12cd34","id":"unique-command-id","expires_ms":9000,"action":"start","values":null}
```

基础action为start、stop、config、fan，液体扩展见下一段。config 的 values 必须包含全部四个参数。fan 的 values 为 schedule/on/off。

液体扩展增加 action=liquid，values为{"pump":"water","seconds":5}或{"pump":"nutrient","seconds":3}。water最大30整数秒，nutrient最大10整数秒；seconds=0仅停止指定泵。环境控制与泵输送互斥，两路泵不能叠加。stop始终停止所有输出。

新固件的status另带liquids对象：water_pump、nutrient_pump、water_source_ok、water_room_ok、nutrient_source_ok、nutrient_room_ok均为布尔值，remaining_ms为剩余整数毫秒，fault为状态说明。源罐有液、接收容器未满才为true。未携带此对象的旧v1报文仍可读取，但后台禁止泵命令；断线时网页显示liquids=null，不能沿用旧输出状态。使用新固件时四个.py文件必须一起更新。

boot_id 必须匹配当前板；expires_ms 是板端开机计时到期点，过期命令拒绝。板端保留最近 16 个命令应答以避免重复执行；超过这个窗口的旧命令依靠有效期拒绝。电脑不自动重试执行器命令。

应答：

```json
{"v":1,"type":"ack","boot_id":"ab12cd34","id":"unique-command-id","ok":true}
```

失败时 ok=false 并携带 error。电脑串行发送控制请求，3 秒内未收到匹配应答便关闭通信，显示结果未知；此时板端靠心跳超时停机。重新启动电脑后台时若首条状态显示仍在运行，后台拒绝接管并断开，等待至少 10 秒后再连接。

4 秒未收到有效状态，电脑端断开并停止心跳；板端距最后心跳 10 秒关闭输出。先标记未知，不能把断线直接当作已物理关闭。

本协议用于本机 USB 原型，没有密码认证，不能直接暴露到网络或工业总线。板端最多接收 1024 字符命令行、电脑最多接收 4096 字节状态行。

## 电脑 HTTP 接口

仅监听 127.0.0.1。限制 Host、Origin 和跨站请求，写请求需页面专用标识。

| 方法 | 地址 | 用途 |
|---|---|---|
| GET | /api/status | 来源、连接状态、当前状态、事件 |
| GET | /api/history?limit=300 | 最近记录，上限 3600 条 |
| GET | /api/export.csv | 最近最多 10 万条 CSV |
| GET | /api/report | 规则统计与证据范围 |
| GET | /api/report.txt | 下载当前 TXT 统计报告 |
| GET | /api/photos | 最近 24 张照片元数据 |
| GET | /photos/{id} | 照片 |
| POST | /api/control | 控制命令 |
| POST | /api/scenario | 仅模拟模式的演示场景 |
| POST | /api/photos | PNG 照片，最大 4MB / 1280×720 |
| POST | /api/analysis | 可选本地模型只读解读 |
| POST | /api/agent | 中文指令：id 为8～64位字母/数字/连字符，message 为1～500字 |
| POST | /api/agent/client-result | 当前页面回传摄像头执行结果：id、action、ok、detail |

JSON 写请求使用 Content-Type: application/json 与 X-Room-Client: local-dashboard；照片使用 image/png。数据源由启动参数选择，网页不能在运行期间切换模拟与实物模式。

智能体响应包含 ok、text、trace、status；摄像头步骤先返回 client_action 与 pending，页面完成并回传后才记录最终结果。plan 不由客户端提交，后端仅执行经过校验的工具。单次最多4步，请求编号在最近64条内防重复执行；失败不自动重试。后台状态增加 control_revision 控制版本，智能体发命令时同时检查版本与 boot_id，人工控制或重启后取消旧计划。串口报文结构保持不变。

同一时刻只允许一条控制请求执行；并发请求直接拒绝，不排队。命令应答同时匹配发送状态、命令 ID 和原开发板启动标识；超时或重启已取消的请求不能被迟到应答改写为成功。页面等待应答期间禁用控制按钮，设备重启后同步实际参数。
