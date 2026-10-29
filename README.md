<div align="center">

# AI 防溢料视觉监控系统

**基于 YOLO 目标检测与 Siemens PLC 的混凝土卸料实时溢料检测及联锁控制方案**

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Ultralytics](https://img.shields.io/badge/Ultralytics-YOLO-00BFFF)](https://github.com/ultralytics/ultralytics)
[![Snap7](https://img.shields.io/badge/PLC-Snap7%20%2F%20S7--200%20SMART-009999)](https://github.com/gijzelaerr/python-snap7)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)]()

</div>

---

## 1. 项目简介

本项目面向混凝土搅拌站的卸料场景，通过工业摄像头实时采集卸料口画面，利用 YOLO 模型识别**卸料口、收集口与混凝土料流**，计算混凝土相对于收集口的面积占比（溢出率），并在溢出风险持续超限时通过 S7 协议向 PLC 下发联锁指令，实现自动关阀、防溢料处置与镜头自清洁。

系统提供完整的桌面监控界面，支持实时画面预览、设备状态监控、手动控制、事件日志与自动录像，可作为工业现场视觉防溢料方案的基础实现或二次开发参考。

> **English Abstract**: An AI-based anti-overflow monitoring system for concrete discharging. A YOLO model detects the discharge port, collection port and concrete flow from an RTSP camera stream; the overflow ratio is computed in a two-stage detection pipeline and, when it exceeds a configurable threshold, interlock commands are sent to a Siemens S7-200 SMART PLC via Snap7. The project includes a Tkinter monitoring GUI, automatic recording and a configurable detection pipeline.

## 2. 核心特性

- **两阶段检测流程**：先在卸料初期跟踪收集口位置，待料流稳定后冻结参考区域，再基于面积比计算溢出率，降低检测框抖动带来的误判。
- **多级滤波与迟滞**：EMA 中心点平滑、30 帧滚动窗口、连续移动/丢框计数与死区判定，提升工业现场的稳定性。
- **PLC 联锁控制**：通过 Snap7 与 Siemens S7-200 SMART 通信，支持开门、关门、全开、全关、先关后开（防溢料）及喷水清洗等命令序列，具备心跳保活、断线重连与命令补发能力。
- **镜头自清洁**：基于 Laplacian 方差的清晰度评估，检测到清晰度骤降时自动触发喷水 + 雨刷流程。
- **进程隔离架构**：视频捕获与界面运行于主进程，模型推理运行于独立子进程，PLC 通信与录像各自运行于后台线程，避免推理阻塞 UI。
- **自动录像**：进入二阶段后自动开始录制，退出二阶段 30 秒后自动停止；支持存储上限与旧文件自动清理。
- **可配置化**：视频源、模型路径、PLC 地址、检测阈值、推理参数等均通过 `.env` 管理，无需修改代码。

## 3. 系统架构

```mermaid
flowchart LR
    CAM[IP Camera / RTSP / Local Video] --> CAP[视频捕获线程]
    CAP -->|原始帧| INF[YOLO 推理子进程]
    INF -->|标注帧| GUI[监控 GUI<br/>Tkinter]
    INF -->|检测状态 / 溢出率| GUI
    GUI -->|控制命令| CMD[命令队列]
    CMD --> PLC[PLC 工作线程<br/>Snap7]
    PLC <-->|VB100 / VB200| S7[Siemens S7-200 SMART SR20]
    S7 --> ACT[卸料门 / 喷水 / 雨刷]
    GUI --> REC[录像线程]
    INF --> REC
    GUI --> LOG[日志与事件记录]
```

运行时由以下模块协作：

| 模块 | 载体 | 职责 |
|------|------|------|
| `MonitorGUI` | 主进程主线程 | 界面渲染、状态展示、用户操作响应 |
| 捕获线程 | 主进程后台线程 | 读取 RTSP / 本地视频帧，投递至推理进程 |
| `DetectionProcess` | 独立子进程 | YOLO 推理、检测逻辑与状态计算 |
| `PLCWorker` | 后台线程 | PLC 状态轮询、心跳、命令下发与重连 |
| 录制线程 | 后台线程 | 视频编码与存储空间管理 |

## 4. 检测与控制逻辑

### 4.1 目标类别

模型输出 3 个类别（见 [`train.yaml`](train.yaml)）：

| 类别 ID | 名称 | 说明 |
|---------|------|------|
| 0 | `dischargPort` | 卸料口 |
| 1 | `collectionPort` | 收集口 / 料斗 |
| 2 | `liquidconcrete` | 混凝土料流 |

### 4.2 两阶段状态机

| 阶段 | 触发条件 | 行为 |
|------|----------|------|
| 一阶段 | 画面中仅检测到收集口 | 以首次检测位置建立参考区域；正常执行清晰度采样 |
| 二阶段（跟踪） | 收集口连续 `STOP_THRESHOLD` 帧静止 | 持续跟踪收集口位置，位置未固定前不参与溢出判定 |
| 二阶段（冻结） | 跟踪状态下连续 `FREEZE_SECONDS` 秒无有效位移 | 冻结参考中心、面积与半径，开始计算溢出率并执行联锁判断 |
| 退出 | 连续 5 帧丢失目标 | 重置跟踪状态，回到一阶段 |

### 4.3 溢料判定与联锁

冻结参考区域后，按以下公式计算溢出率：

```
溢出率 = 混凝土检测框面积 / 收集口参考面积
```

当同时满足以下条件时，系统下发防溢料命令 `valve_off_on`（先关闭再重新开启卸料门）：

1. 溢出率超过 `DANGER_RATIO_THRESHOLD`；
2. 持续时间超过 `DANGER_THRESHOLD`；
3. PLC 的 0% 开度到位信号（`switch_0_status`）未激活；
4. 距上次阀门动作已超过 20 秒冷却时间，避免电机频繁动作。

危险状态解除后自动复位计时器，并在事件日志中记录危险告警的起止时间。安全侧自动开阀逻辑当前默认关闭，可按现场工艺在 `_draw_second_stage()` 中启用。

### 4.4 镜头自清洁

- 每 120 秒采样一次画面，计算灰度图的 Laplacian 方差作为清晰度指标。
- 当清晰度相对上次采样骤降超过 20% 时，向 PLC 投递喷水命令。
- 喷水流程：喷水 3 秒 → 雨刷 1 秒 → 停止喷水 → 保持雨刷 10 秒 → 关闭雨刷。
- 二阶段期间仅显示清晰度数值，不自动触发喷水，避免干扰检测。

### 4.5 自动录像

- 开启"自动录像"后，进入二阶段自动开始录制，退出二阶段 30 秒后自动停止。
- 文件保存至 `recordings/`，命名为 `YYYYMMDD_HHMMSS.mp4`，宽度超过 960 px 时等比缩放。
- 存储上限为 50 GB，超限时按修改时间删除最旧文件。

## 5. 硬件与接线

典型部署包含：工业相机（RTSP）、工控机、Siemens S7-200 SMART（CPU SR20）、卸料门执行机构、喷水电磁阀与雨刷电机。

![SR20 PLC 电气图](SR20-PLC电气图.png)





> 接线与供电请由具备资质的电气人员按现场规范实施；软件联锁不能替代硬件急停与安全回路。

## 6. 快速开始

### 6.1 环境要求

- Python 3.12 及以上（推荐使用虚拟环境）
- Windows 10/11 或 Linux
- 与 PLC 连通的以太网环境
- 可选：NVIDIA GPU（当前默认使用 CPU 推理，可在推理参数中改为 `device=0`）

### 6.2 安装依赖

```bash
git clone <repository-url>
cd plc-Anti-overflow-code

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
# requirements.txt 未包含 PLC 通信库，需单独安装
pip install python-snap7
```

> 说明：`requirements.txt` 中同时存在 `dotenv` 与 `python-dotenv`，二者可能产生模块遮蔽。若导入 `load_dotenv` 失败，请卸载 `dotenv` 并仅保留 `python-dotenv`。

### 6.3 配置

```bash
# Windows
copy .env.example .env
# Linux / macOS
cp .env.example .env
```

编辑 `.env`，至少确认以下配置：

```ini
# 视频源：本地文件或 RTSP 地址
VIDEO_SOURCE=rtsp://admin:password@192.168.1.100:554/Streaming/Channels/101

# 训练权重
MODEL_PATH=./best.pt

# PLC 地址（S7-200 SMART，机架 0 / 槽位 1）
PLC_IP=192.168.18.88
PLC_RACK=0
PLC_SLOT=1
PLC_CONN_TYPE=2
```

### 6.4 运行

```bash
# 图形化监控界面（推荐）
python gui_app.py

# 无界面调试模式（OpenCV 窗口预览）
python main.py
```

界面快捷键：`F11` 切换全屏，`Esc` 退出全屏。

## 7. 配置项说明

所有配置均可通过 `.env` 覆盖，未配置时使用 [`config.py`](config.py) 中的默认值。

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `VIDEO_SOURCE` | `./train-video2/h2.mp4` | 视频源，支持本地文件或 RTSP |
| `MODEL_PATH` | `best.pt` | YOLO 权重路径，支持 `.pt` 或 OpenVINO 目录 |
| `PLC_IP` | `192.168.18.88` | PLC IP 地址 |
| `PLC_RACK` / `PLC_SLOT` | `0` / `1` | PLC 机架号与槽位号 |
| `PLC_CONN_TYPE` | `2` | Snap7 连接类型 |
| `YOLO_CONF` | `0.5` | 置信度阈值 |
| `YOLO_IMGSZ` | `320` | 推理输入尺寸 |
| `VID_STRIDE` | `2` | 跳帧间隔，`2` 表示隔帧推理以降低 CPU 负载 |
| `DANGER_RATIO_THRESHOLD` | `0.7` | 触发溢料告警的面积比阈值（现场示例配置为 `0.9`） |
| `DANGER_THRESHOLD` | `2` | 危险状态需持续的秒数 |
| `STOP_THRESHOLD` | `5` | 判定目标静止所需的连续帧数 |
| `MOVE_EPSILON` | `2` | 位移死区（像素） |
| `FREEZE_SECONDS` | `30` | 二阶段进入后位置冻结所需时间（秒） |
| `CLARITY_THRESHOLD` | `10` | 清晰度阈值（用于界面提示） |
| `LOG_DIR` | `./logs` | 日志目录 |
| `LOG_INTERVAL` | `600` | 运行状态日志输出间隔（秒） |
| `FPS_UPDATE_INTERVAL` | `100` | FPS 统计窗口（帧） |

## 8. PLC 通信协议

通信基于 Snap7 的 S7 协议，通过 DB1 读写：

- **控制区**：`VB100`，1 字节，写 PLC 控制位
- **状态区**：`VB200`，1 字节，读设备状态位

### 8.1 控制字（VB100）

| 位 | 功能 | 说明 |
|----|------|------|
| bit0 | 开门 | 与 bit7 配合 |
| bit1 | 关门 | 与 bit7 配合 |
| bit2 | 喷水 | — |
| bit3 | 雨刷 | — |
| bit7 | 动作使能 | 动作时置 1，复位时清 0 |

### 8.2 状态字（VB200）

| 位 | 变量 | 说明 |
|----|------|------|
| bit0 | `concrete_ready` | 混凝土到位 |
| bit1 | `concrete_end` | 卸料完毕 |
| bit2 | `switch_100_status` | 100% 开度到位 |
| bit3 | `switch_0_status` | 0% 开度到位 |
| bit4 | `mode_status` | 模式状态 |

### 8.3 指令时序

| 命令 | 时序 |
|------|------|
| `valve_on`（半开） | bit0=1 → 2 s → 复位 |
| `valve_off`（半关） | bit1=1 → 2 s → 复位 |
| `valve_on_totally`（全开） | bit0=1 → 3.2 s → 复位 |
| `valve_off_totally`（全关） | bit1=1 → 3.2 s → 复位 |
| `valve_off_on`（防溢料联锁） | 关门 1.5 s → 停止 → 等待 6 s → 开门 1.5 s → 停止 → 等待 2 s → 复位 |
| `spray`（清洗镜头） | 喷水 3 s → 雨刷 → 1 s → 停喷水 → 10 s → 停雨刷 |

PLC 工作线程以 0.1 s 周期轮询状态、5 s 周期发送心跳；连接异常时自动重连，并补发断线期间积压的控制命令。

## 9. 模型训练

训练数据配置见 [`train.yaml`](train.yaml)，类别定义为 `dischargPort`、`collectionPort`、`liquidconcrete`。使用 Ultralytics 训练：

```bash
yolo detect train \
  data=train.yaml \
  model=yolov8n.pt \
  imgsz=640 \
  epochs=100 \
  batch=16 \
  project=runs/detect \
  name=train
```

注意事项：

- `train.yaml` 中的 `train` / `val` 为绝对路径，迁移到其他机器时需修改。
- 训练完成后将 `runs/detect/train/weights/best.pt` 复制到项目根目录，或通过 `MODEL_PATH` 指定路径。
- 项目附带的 `best.pt` 为示例权重；`train12/` 目录保留了训练曲线、混淆矩阵与评估结果，可作为数据集与训练过程参考。
- 也可导出 OpenVINO INT8 模型以提升 CPU 推理速度，并通过 `MODEL_PATH` 指向导出目录。

## 10. 项目结构

```text
plc-Anti-overflow-code/
├── gui_app.py                 # 图形化监控程序入口
├── main.py                    # 无界面检测程序入口（含检测与 PLC 核心逻辑）
├── plc.py                     # Snap7 PLC 通信封装
├── config.py                  # 环境变量与配置加载
├── best.pt                    # YOLO 示例权重
├── train.yaml                 # 数据集配置
├── train12/                   # 训练输出（曲线、评估指标；权重由 .gitignore 忽略）
├── .env.example               # 配置模板
├── requirements.txt           # Python 依赖
├── logs/                      # 运行日志（运行时生成）
├── recordings/                # 录像文件（运行时生成）
├── SR20-PLC电气图.png         # PLC 电气原理图
├── Electrical_schematic_diagram.png
└── AI防溢料plc版本安装文档.pdf
```

## 11. 日志与故障排查

- 日志写入 `logs/app.log`，按 50 MB 轮转、保留 3 个备份；界面底部同步显示分级日志。
- 关键事件（危险告警起止）通过 `EventLogger` 单独记录起止时间与持续时长。

| 现象 | 排查方向 |
|------|----------|
| PLC 连接失败 | 检查 IP、机架/槽位、网络连通性与防火墙；确认 PLC 已允许 PUT/GET 通信 |
| 界面无画面 | 检查 `VIDEO_SOURCE`、RTSP 账号密码与编码格式；确认相机网络可达 |
| 检测频繁进出二阶段 | 适当增大 `STOP_THRESHOLD` 或 `MOVE_EPSILON`，减少检测框抖动影响 |
| 误报溢料 | 提高 `DANGER_RATIO_THRESHOLD` 或 `DANGER_THRESHOLD`，并确认参考区域冻结稳定 |
| 检测进程挂起 | 程序在 30 秒无新帧时自动重启推理进程；若频繁发生，检查 GPU/CPU 负载与视频流稳定性 |
| 中文显示为方框 | 系统缺少中文字体；Windows 通常自带微软雅黑/黑体，Linux 需安装对应字体 |

## 12. 安全声明

本项目为工业视觉辅助控制系统，仅作为防溢料监测与联锁的**辅助手段**。部署与运行时应确保：

- 现场保留独立于本系统的硬件急停、限位与安全联锁回路；
- 首次调试时断开执行机构或空载验证命令时序；
- 根据实际工艺标定检测阈值，并经过充分的现场测试后再投入自动运行；
- 操作人员具备相应的设备与电气安全培训。

因现场环境、相机安装角度、光照条件与 PLC 程序差异导致的检测偏差，需由部署方自行评估与调优。

## 13. 许可证与致谢

本仓库暂未附带开源许可证，正式发布前请补充 `LICENSE` 文件以明确授权范围。

本项目使用 [Ultralytics YOLO](https://github.com/ultralytics/ultralytics)、[OpenCV](https://opencv.org/)、[python-snap7](https://github.com/gijzelaerr/python-snap7) 等开源项目，感谢其作者与社区的贡献。
