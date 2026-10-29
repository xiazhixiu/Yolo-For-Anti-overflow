"""
配置管理模块
从.env文件加载环境变量，提供统一的配置接口
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# 加载.env文件
env_path = Path(__file__).parent / '.env'
load_dotenv(env_path)

# ==================== 核心配置 ====================
# 视频源配置
VIDEO_SOURCE = os.getenv('VIDEO_SOURCE', './train-video2/h2.mp4')

# 串口配置（已弃用，保留兼容）
SERIAL_PORT = os.getenv('SERIAL_PORT', 'COM3')
SERIAL_BAUDRATE = int(os.getenv('SERIAL_BAUDRATE', '9600'))
SERIAL_TIMEOUT = float(os.getenv('SERIAL_TIMEOUT', '1'))

# PLC 配置
PLC_IP = os.getenv('PLC_IP', '192.168.18.88')
PLC_RACK = int(os.getenv('PLC_RACK', '0'))
PLC_SLOT = int(os.getenv('PLC_SLOT', '1'))
PLC_CONN_TYPE = int(os.getenv('PLC_CONN_TYPE', '2'))
PLC_AUTO_START = os.getenv('PLC_AUTO_START', 'true').lower() == 'true'

# 模型路径配置
MODEL_PATH = os.getenv('MODEL_PATH', 'best.pt')

# ==================== 日志配置 ====================
LOG_DIR = os.getenv('LOG_DIR', './logs')
LOG_INTERVAL = int(os.getenv('LOG_INTERVAL', '600'))

# ==================== 污渍检测配置 ====================
CLARITY_THRESHOLD = float(os.getenv('CLARITY_THRESHOLD', '10'))
BLUR_THRESHOLD = int(os.getenv('BLUR_THRESHOLD', '30'))
SPRAY_COOLDOWN = int(os.getenv('SPRAY_COOLDOWN', '21600'))

# ==================== 危险检测配置 ====================
DANGER_RATIO_THRESHOLD = float(os.getenv('DANGER_RATIO_THRESHOLD', '0.7'))
DANGER_THRESHOLD = int(os.getenv('DANGER_THRESHOLD', '2'))

# ==================== 安全检测配置 ====================
SAFETY_RATIO_THRESHOLD = float(os.getenv('SAFETY_RATIO_THRESHOLD', '0.3'))
SAFETY_THRESHOLD = int(os.getenv('SAFETY_THRESHOLD', '2'))

# ==================== 物体跟踪配置 ====================
STOP_THRESHOLD = int(os.getenv('STOP_THRESHOLD', '5'))
MOVE_EPSILON = float(os.getenv('MOVE_EPSILON', '2'))
FREEZE_SECONDS = int(os.getenv('FREEZE_SECONDS', '30'))

# ==================== 串口配置 ====================
SERIAL_AUTO_START = os.getenv('SERIAL_AUTO_START', 'true').lower() == 'true'

# ==================== YOLO模型配置 ====================
YOLO_CONF = float(os.getenv('YOLO_CONF', '0.5'))
YOLO_IMGSZ = int(os.getenv('YOLO_IMGSZ', '320'))
VID_STRIDE = int(os.getenv('VID_STRIDE', '2'))  # 跳帧: 1=每帧, 2=隔帧(CPU减半)

# ==================== 性能监控配置 ====================
FPS_UPDATE_INTERVAL = int(os.getenv('FPS_UPDATE_INTERVAL', '100'))


def get_device_info():
    """获取 YOLO 推理设备信息"""
    try:
        import torch
        if torch.cuda.is_available():
            return f"CUDA (GPU: {torch.cuda.get_device_name(0)})"
        else:
            return "CPU"
    except ImportError:
        return "CPU (torch 未安装)"


def print_config():
    """打印当前配置信息"""
    print("\n" + "="*50)
    print("当前应用配置")
    print("="*50)
    print(f"视频源: {VIDEO_SOURCE}")
    print(f"模型路径: {MODEL_PATH}")
    print(f"推理设备: {get_device_info()}")
    print(f"PLC地址: {PLC_IP} (机架:{PLC_RACK} 槽位:{PLC_SLOT})")
    print(f"日志目录: {LOG_DIR}")
    print(f"清晰度阈值: {CLARITY_THRESHOLD}")
    print(f"危险比例阈值: {DANGER_RATIO_THRESHOLD}")
    print(f"安全比例阈值: {SAFETY_RATIO_THRESHOLD}")
    print(f"二阶段固定时间: {FREEZE_SECONDS}秒")
    print("="*50 + "\n")


if __name__ == '__main__':
    print_config()
