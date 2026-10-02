# 来源与依赖

## 六路与视觉扩展来源

- SCD41：参考[厂商SCD4x手册](https://admin.sensirion.com/media/documents/48C4B7FB/64C134E7/Sensirion_SCD4x_Datasheet.pdf)的I2C命令和CRC规则，驱动原创；不分发厂商手册或代码。
- 图像接口：参考[Ollama视觉API](https://docs.ollama.com/capabilities/vision)，复用本机Ollama，没有升级核心依赖。
- 视觉模型：[Qwen3-VL-2B-Instruct官方模型卡](https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct)，Apache-2.0；[Ollama官方标签](https://ollama.com/library/qwen3-vl:2b-instruct)。下载到本机用于推理，源码归档不含权重。最初测试qwen3-vl:2b时出现最终回答为空，已改用instruct标签；不将模型思考文本当作识别结果。
- 没有引入第三方蘑菇图片或训练集，用户菌种与样本尚待确认，不能声明领域识别准确率。

核查日期：2026-09-26。项目业务代码、前端页面、控制逻辑和传感器驱动均为本任务独立编写；未引入第三方前端库、字体或图片。沿用工作区既有“Python 标准库本地后台 + SQLite”思路，没有修改旧项目。

| 项目 | 官方来源 | 使用与许可情况 |
|---|---|---|
| Python | [PSF 许可证](https://docs.python.org/3/license.html) | 电脑端使用现有 Python 标准库；不打包解释器 |
| MicroPython | [官方许可证](https://github.com/micropython/micropython/blob/master/LICENSE) | 主项目 MIT；端口可能另有第三方条款。这里交付原创 .py，不打包固件/编译工具链；实物刷写版本待核对 |
| ESP32 MicroPython API | [官方快速参考](https://docs.micropython.org/en/latest/esp32/quickref.html) | Pin、I²C、串口、WDT 与时间接口参考；文档不是实物验证证据 |
| AHT20 | [奥松 B2 手册](https://www.aosong.com/userfiles/files/media/AHT20%E4%BA%A7%E5%93%81%E8%A7%84%E6%A0%BC%E4%B9%A6%28%E4%B8%AD%E6%96%87%E7%89%88%29%20B2-0630.pdf) | 只参考通信参数与公式，未复制厂商示例代码/插图或分发 PDF；未取得手册再分发许可 |
| pySerial 3.5 | [官方仓库与许可证](https://github.com/pyserial/pyserial/blob/v3.5/LICENSE.txt) | BSD-3-Clause；仅实物模式需要，当前没有安装到本项目或复制其源码 |
| Ollama | [API 文档](https://docs.ollama.com/api/chat)、[许可证](https://github.com/ollama/ollama/blob/main/LICENSE) | 可选本机解读，Ollama 主项目 MIT；复用已安装 0.34.2，无二进制再分发 |
| Qwen3-4B | [官方模型卡](https://huggingface.co/Qwen/Qwen3-4B) | 可选已安装模型，Apache-2.0；没有下载/打包模型。工作区原有许可记录见下方 |

工作区已有本地模型部署记录与许可证位于 `../../industrial-diagnosis/docs/LOCAL_MODEL.md` 和 `../../industrial-diagnosis/docs/third-party/`。本项目启动不依赖旧项目业务代码；默认模式不启动模型、不访问外网。

本次没有引入 GPL/AGPL 代码。未为原创项目擅自新增或变更总许可证；若要公开发布或打包运行时，另核对最终依赖版本、许可文本与学校要求。

智能体扩展参考 Ollama 官方 [Chat API](https://docs.ollama.com/api/chat) 与 [工具调用说明](https://docs.ollama.com/capabilities/tool-calling)。本项目实际采用 format JSON Schema 生成受限计划，再由原创 Python 执行层校验并调用已有工具，未新增运行依赖、复制第三方示例或打包模型。
