<div align="center">

# AI Anti-Overflow Vision Monitoring System

**Real-time overflow detection and PLC interlock control for concrete discharging, powered by YOLO and Siemens PLC**

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Ultralytics](https://img.shields.io/badge/Ultralytics-YOLO-00BFFF)](https://github.com/ultralytics/ultralytics)
[![Snap7](https://img.shields.io/badge/PLC-Snap7%20%2F%20S7--200%20SMART-009999)](https://github.com/gijzelaerr/python-snap7)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey)]()

**English** | [简体中文](README.md)

</div>

---

## 1. Overview

This project targets the concrete discharging process at batching plants. An industrial camera captures the discharge area in real time, and a YOLO model detects the **discharge port, collection port and concrete flow**. The system computes the area ratio of concrete to the collection port (the overflow ratio) and, when the overflow risk stays above a configurable threshold, sends interlock commands to a PLC over the S7 protocol to close the valve, mitigate overflow and clean the lens automatically.

The project includes a complete desktop monitoring interface with live video preview, equipment status monitoring, manual control, event logging and automatic recording. It can serve as a reference implementation or a starting point for further development of vision-based anti-overflow systems in industrial environments.

## 2. Key Features

- **Two-stage detection pipeline**: The collection port is tracked during the early discharging phase; once the flow stabilizes, the reference region is frozen and the overflow ratio is computed from the area ratio, reducing false positives caused by bounding-box jitter.
- **Multi-level filtering and hysteresis**: EMA center smoothing, a 30-frame rolling window, consecutive-motion/lost-frame counters and a dead zone improve stability on site.
- **PLC interlock control**: Communicates with a Siemens S7-200 SMART PLC via Snap7. Supports open, close, fully open, fully closed, close-then-open (anti-overflow) and spray-cleaning command sequences, with heartbeat keep-alive, automatic reconnection and pending-command replay.
- **Automatic lens cleaning**: Sharpness is assessed from the Laplacian variance; a sudden drop triggers the spray + wiper sequence automatically.
- **Process-isolated architecture**: Video capture and the UI run in the main process, inference runs in a dedicated child process, and PLC communication and recording each run in background threads, preventing inference from blocking the UI.
- **Automatic recording**: Recording starts automatically when the system enters stage 2 and stops 30 seconds after leaving it. A storage cap with automatic cleanup of the oldest files is supported.
- **Configurable**: Video source, model path, PLC address, detection thresholds and inference parameters are all managed through `.env`, with no code changes required.

## 3. Architecture

```mermaid
flowchart LR
    CAM[IP Camera / RTSP / Local Video] --> CAP[Capture Thread]
    CAP -->|Raw frames| INF[YOLO Inference Process]
    INF -->|Annotated frames| GUI[Monitoring GUI<br/>Tkinter]
    INF -->|Detection state / overflow ratio| GUI
    GUI -->|Control commands| CMD[Command Queue]
    CMD --> PLC[PLC Worker Thread<br/>Snap7]
    PLC <-->|VB100 / VB200| S7[Siemens S7-200 SMART SR20]
    S7 --> ACT[Discharge Door / Spray / Wiper]
    GUI --> REC[Recording Thread]
    INF --> REC
    GUI --> LOG[Logging & Event Records]
```

The runtime consists of the following cooperating modules:

| Module | Host | Responsibility |
|--------|------|----------------|
| `MonitorGUI` | Main process, main thread | UI rendering, status display, user input handling |
| Capture thread | Main process, background thread | Reads RTSP / local video frames and forwards them to the inference process |
| `DetectionProcess` | Dedicated child process | YOLO inference, detection logic and state computation |
| `PLCWorker` | Background thread | PLC status polling, heartbeat, command dispatch and reconnection |
| Recording thread | Background thread | Video encoding and storage management |

## 4. Detection and Control Logic

### 4.1 Target Classes

The model outputs 3 classes (see [`train.yaml`](train.yaml)):

| Class ID | Name | Description |
|----------|------|-------------|
| 0 | `dischargPort` | Discharge port |
| 1 | `collectionPort` | Collection port / hopper |
| 2 | `liquidconcrete` | Concrete flow |

### 4.2 Two-Stage State Machine

| Stage | Trigger | Behavior |
|-------|---------|----------|
| Stage 1 | Only the collection port is detected | Establishes the reference region from the first detection; performs sharpness sampling |
| Stage 2 (tracking) | The collection port is stationary for `STOP_THRESHOLD` consecutive frames | Continuously tracks the collection port; overflow is not evaluated until the position is frozen |
| Stage 2 (frozen) | No valid displacement for `FREEZE_SECONDS` seconds while tracking | Freezes the reference center, area and radii, then computes the overflow ratio and evaluates the interlock |
| Exit | Target lost for 5 consecutive frames | Resets tracking state and returns to stage 1 |

### 4.3 Overflow Decision and Interlock

Once the reference region is frozen, the overflow ratio is computed as:

```
overflow_ratio = concrete_bbox_area / collection_port_reference_area
```

The system issues the anti-overflow command `valve_off_on` (close the discharge door, then reopen it) when all of the following conditions hold:

1. The overflow ratio exceeds `DANGER_RATIO_THRESHOLD`;
2. The condition persists longer than `DANGER_THRESHOLD`;
3. The PLC's 0% position signal (`switch_0_status`) is not active;
4. At least 20 seconds have elapsed since the last valve action, preventing frequent motor operations.

When the danger condition clears, the timer resets automatically and the start and end of each danger alert are written to the event log. The safety-side automatic valve-opening logic is disabled by default and can be enabled in `_draw_second_stage()` according to the site process.

### 4.4 Automatic Lens Cleaning

- The image is sampled every 120 seconds, and the Laplacian variance of the grayscale frame is used as the sharpness metric.
- When sharpness drops by more than 20% relative to the previous sample, a spray command is queued to the PLC.
- Spray sequence: spray 3 s → wiper on 1 s → stop spraying → keep the wiper running 10 s → wiper off.
- During stage 2, sharpness is only displayed and never triggers spraying automatically, avoiding interference with detection.

### 4.5 Automatic Recording

- When "auto recording" is enabled, recording starts upon entering stage 2 and stops 30 seconds after leaving it.
- Files are saved to `recordings/` as `YYYYMMDD_HHMMSS.mp4`; frames wider than 960 px are scaled proportionally.
- The storage cap is 50 GB; when exceeded, the oldest files are deleted by modification time.

## 5. Hardware and Wiring

A typical deployment consists of an industrial camera (RTSP), an industrial PC, a Siemens S7-200 SMART (CPU SR20), a discharge-door actuator, a spray solenoid valve and a wiper motor.

![SR20 PLC Wiring Diagram](SR20-PLC电气图.png)

> Wiring and power supply must be carried out by qualified electricians in accordance with local regulations. Software interlocks do not replace hardware emergency stops and safety circuits.

## 6. Quick Start

### 6.1 Requirements

- Python 3.12 or later (a virtual environment is recommended)
- Windows 10/11 or Linux
- Ethernet connectivity to the PLC
- Optional: an NVIDIA GPU (CPU inference is the default; set `device=0` in the inference arguments to use a GPU)

### 6.2 Install Dependencies

```bash
git clone <repository-url>
cd plc-Anti-overflow-code

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
# The PLC communication library is not included in requirements.txt
pip install python-snap7
```

> Note: `requirements.txt` lists both `dotenv` and `python-dotenv`, which may shadow each other. If importing `load_dotenv` fails, uninstall `dotenv` and keep only `python-dotenv`.

### 6.3 Configuration

```bash
# Windows
copy .env.example .env
# Linux / macOS
cp .env.example .env
```

Edit `.env` and confirm at least the following settings:

```ini
# Video source: local file or RTSP URL
VIDEO_SOURCE=rtsp://admin:password@192.168.1.100:554/Streaming/Channels/101

# Trained weights
MODEL_PATH=./best.pt

# PLC address (S7-200 SMART, rack 0 / slot 1)
PLC_IP=192.168.18.88
PLC_RACK=0
PLC_SLOT=1
PLC_CONN_TYPE=2
```

### 6.4 Running

```bash
# Graphical monitoring interface (recommended)
python gui_app.py

# Headless debug mode (OpenCV preview window)
python main.py
```

Keyboard shortcuts: `F11` toggles fullscreen, `Esc` exits fullscreen.

## 7. Configuration Reference

All settings can be overridden through `.env`; when unset, the defaults in [`config.py`](config.py) apply.

| Option | Default | Description |
|--------|---------|-------------|
| `VIDEO_SOURCE` | `./train-video2/h2.mp4` | Video source; local file or RTSP |
| `MODEL_PATH` | `best.pt` | YOLO weights path; `.pt` file or OpenVINO directory |
| `PLC_IP` | `192.168.18.88` | PLC IP address |
| `PLC_RACK` / `PLC_SLOT` | `0` / `1` | PLC rack and slot number |
| `PLC_CONN_TYPE` | `2` | Snap7 connection type |
| `YOLO_CONF` | `0.5` | Confidence threshold |
| `YOLO_IMGSZ` | `320` | Inference input size |
| `VID_STRIDE` | `2` | Frame stride; `2` runs inference every other frame to reduce CPU load |
| `DANGER_RATIO_THRESHOLD` | `0.7` | Area-ratio threshold for the overflow alert (the site example uses `0.9`) |
| `DANGER_THRESHOLD` | `2` | Seconds the danger condition must persist |
| `STOP_THRESHOLD` | `5` | Consecutive frames required to consider the target stationary |
| `MOVE_EPSILON` | `2` | Motion dead zone in pixels |
| `FREEZE_SECONDS` | `30` | Seconds after entering stage 2 before the position is frozen |
| `CLARITY_THRESHOLD` | `10` | Sharpness threshold (used for UI indication) |
| `LOG_DIR` | `./logs` | Log directory |
| `LOG_INTERVAL` | `600` | Interval in seconds between runtime status logs |
| `FPS_UPDATE_INTERVAL` | `100` | FPS measurement window in frames |

## 8. PLC Communication Protocol

Communication uses the S7 protocol through Snap7 and reads/writes DB1:

- **Control area**: `VB100`, 1 byte, writes PLC control bits
- **Status area**: `VB200`, 1 byte, reads equipment status bits

### 8.1 Control Word (VB100)

| Bit | Function | Description |
|-----|----------|-------------|
| bit0 | Open door | Used together with bit7 |
| bit1 | Close door | Used together with bit7 |
| bit2 | Spray | — |
| bit3 | Wiper | — |
| bit7 | Action enable | Set to 1 during an action, cleared on reset |

### 8.2 Status Word (VB200)

| Bit | Variable | Description |
|-----|----------|-------------|
| bit0 | `concrete_ready` | Concrete present |
| bit1 | `concrete_end` | Discharging complete |
| bit2 | `switch_100_status` | 100% position reached |
| bit3 | `switch_0_status` | 0% position reached |
| bit4 | `mode_status` | Mode status |

### 8.3 Command Sequences

| Command | Sequence |
|---------|----------|
| `valve_on` (half open) | bit0=1 → 2 s → reset |
| `valve_off` (half close) | bit1=1 → 2 s → reset |
| `valve_on_totally` (fully open) | bit0=1 → 3.2 s → reset |
| `valve_off_totally` (fully close) | bit1=1 → 3.2 s → reset |
| `valve_off_on` (anti-overflow interlock) | Close 1.5 s → stop → wait 6 s → open 1.5 s → stop → wait 2 s → reset |
| `spray` (lens cleaning) | Spray 3 s → wiper → 1 s → stop spray → 10 s → wiper off |

The PLC worker thread polls status every 0.1 s and sends a heartbeat every 5 s. On connection failure it reconnects automatically and replays control commands queued while offline.

## 9. Model Training

Dataset configuration is provided in [`train.yaml`](train.yaml) with the classes `dischargPort`, `collectionPort` and `liquidconcrete`. Train with Ultralytics:

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

Notes:

- The `train` / `val` entries in `train.yaml` are absolute paths and must be updated when moving to another machine.
- After training, copy `runs/detect/train/weights/best.pt` to the project root, or point `MODEL_PATH` to its location.
- The bundled `best.pt` is a sample weight file. `train12/` contains training curves, confusion matrices and evaluation results as a reference for the dataset and training process.
- The model can also be exported to OpenVINO INT8 to improve CPU inference speed; point `MODEL_PATH` to the exported directory.

## 10. Project Structure

```text
plc-Anti-overflow-code/
├── gui_app.py                 # Graphical monitoring entry point
├── main.py                    # Headless detection entry point (core detection and PLC logic)
├── plc.py                     # Snap7 PLC communication wrapper
├── config.py                  # Environment-variable configuration loader
├── best.pt                    # Sample YOLO weights
├── train.yaml                 # Dataset configuration
├── train12/                   # Training outputs (curves, metrics; weights ignored by .gitignore)
├── .env.example               # Configuration template
├── requirements.txt           # Python dependencies
├── logs/                      # Runtime logs (generated at runtime)
├── recordings/                # Recorded videos (generated at runtime)
├── SR20-PLC电气图.png         # PLC wiring diagram
├── Electrical_schematic_diagram.png
└── AI防溢料plc版本安装文档.pdf
```

## 11. Logging and Troubleshooting

- Logs are written to `logs/app.log`, rotated at 50 MB with 3 backups. The severity-colored log is also shown at the bottom of the GUI.
- Key events (start and end of danger alerts) are recorded separately by `EventLogger` with their duration.

| Symptom | Troubleshooting |
|---------|-----------------|
| PLC connection failure | Check the IP address, rack/slot, network connectivity and firewall; make sure PUT/GET communication is permitted on the PLC |
| No video in the UI | Check `VIDEO_SOURCE`, RTSP credentials and codec; verify camera reachability |
| Frequent stage 1/stage 2 switching | Increase `STOP_THRESHOLD` or `MOVE_EPSILON` to reduce the impact of bounding-box jitter |
| False overflow alerts | Raise `DANGER_RATIO_THRESHOLD` or `DANGER_THRESHOLD`, and verify the reference region freezes stably |
| Detection process hangs | The inference process restarts automatically after 30 s without new frames; if this happens often, check GPU/CPU load and stream stability |
| Chinese text shown as boxes | The system lacks Chinese fonts; Windows usually ships with Microsoft YaHei/SimHei, while Linux requires installing the fonts |

## 12. Safety Notice

This project is an industrial vision assistance system and serves only as an **auxiliary means** of overflow monitoring and interlocking. When deploying and operating it, ensure that:

- Independent hardware emergency stops, limit switches and safety interlock circuits remain in place;
- Actuators are disconnected or tested under no load during initial commissioning;
- Detection thresholds are calibrated for the actual process and the system has been fully tested on site before automatic operation;
- Operators have received appropriate equipment and electrical safety training.

Detection deviations caused by site conditions, camera mounting angle, lighting and PLC program differences must be evaluated and tuned by the deployer.

## 13. License and Acknowledgements

This repository does not currently include an open-source license. Add a `LICENSE` file before public release to define the terms of use.

This project builds on open-source work including [Ultralytics YOLO](https://github.com/ultralytics/ultralytics), [OpenCV](https://opencv.org/) and [python-snap7](https://github.com/gijzelaerr/python-snap7). Thanks to their authors and communities.
