import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|avioflags;direct|max_delay;500000"
import logging,cv2,time,threading,queue,socket
from collections import deque
import logging.handlers
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from queue import PriorityQueue
from ultralytics import YOLO
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (
    VIDEO_SOURCE, MODEL_PATH, LOG_DIR,
    CLARITY_THRESHOLD,
    DANGER_RATIO_THRESHOLD, DANGER_THRESHOLD,
    SAFETY_RATIO_THRESHOLD, SAFETY_THRESHOLD,
    STOP_THRESHOLD, MOVE_EPSILON,
    YOLO_CONF, YOLO_IMGSZ, VID_STRIDE, FPS_UPDATE_INTERVAL, LOG_INTERVAL,
    print_config,
    FREEZE_SECONDS,
    PLC_IP, PLC_RACK, PLC_SLOT, PLC_CONN_TYPE, PLC_AUTO_START,
)
from plc import PLCManager

def setup_logger(log_dir=LOG_DIR, max_log_size=50*1024*1024, backup_count=3):
    """配置日志系统"""
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    logger = logging.getLogger('DemoApp')
    logger.setLevel(logging.INFO)
    
    # 文件处理器 - 按大小轮转
    log_file = os.path.join(log_dir, 'app.log')
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=max_log_size, backupCount=backup_count, encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    
    return logger

logger = setup_logger()

# ==================== 树莓派摄像头参数定义 ====================
# picam2 = Picamera2()
# # 创建优化的预览配置
# config = picam2.create_preview_configuration(
#     main={
#         "size": (320, 320),
#         "format": "RGB888"  # 关键：比RGB888快30-50%
#     },
#     controls={
#         "FrameRate": 60.0,  # 明确设置目标帧率
#         "AwbEnable": False,  # 关闭自动白平衡（减少计算）
#         "AeEnable": False,   # 关闭自动曝光（减少延迟）
#         "ExposureTime": 10000,  # 固定曝光时间（微秒）
#         "AnalogueGain": 2.0,    # 固定增益
#         "NoiseReductionMode": 0,  # 关闭降噪
#     },
#     buffer_count=6,  # 增加缓冲区数量
#     queue=False,     # 关闭队列模式，减少延迟
#     display=None     # 不绑定显示，减少开销
# )
# # picam2.preview_configuration.main.size = (640, 480)
# # picam2.preview_configuration.main.format = "RGB888"
# # picam2.preview_configuration.align()
# picam2.configure(config)
# picam2.start()


# ==================== 常量定义 ====================
# ── 命令标识（保持与旧代码兼容） ──
CMD_VALVE_ON = 'valve_on'
CMD_VALVE_OFF = 'valve_off'
CMD_VALVE_ON_TOTALLY = 'valve_on_totally'
CMD_VALVE_OFF_TOTALLY = 'valve_off_totally'
CMD_VALVE_OFF_ON = 'valve_off_on'
CMD_SPRAY = 'spray'

PRIORITY_HIGH = 0
PRIORITY_LOW = 1

# ── PLC 控制位定义 (VB100) ──
PLC_BIT_DOOR_OPEN  = [1,0,0,0,0,0,0,1]   # bit0: 开门 (1)
PLC_BIT_DOOR_OPEN_STOP  = [0,0,0,0,0,0,0,1]   # bit1: 停止 (2)
PLC_BIT_DOOR_CLOSE = [0,1,0,0,0,0,0,1]   # bit1: 关门 (2)
PLC_BIT_DOOR_CLOSE_STOP = [0,0,0,0,0,0,0,1]   # bit1: 停止 (2)
PLC_BIT_EXCUTE_STOP = [0,0,0,0,0,0,0,0]
PLC_BIT_SPRAY      = [0,0,1,0,0,0,0,0]  # bit2: 喷水 (4)
PLC_BIT_SPRAY_STOP = [0,0,0,1,0,0,0,0]  # bit3: 停止 (8)
PLC_BIT_WIPER      = [0,0,1,1,0,0,0,0]   # bit3: 雨刷 (8)
PLC_BIT_WIPER_STOP = [0,0,0,0,0,0,0,0]   # bit3: 停止 (8)

# ── PLC 寄存器地址 ──
PLC_CTRL_OFFSET   = 100   # VB100: 控制输出
PLC_STATUS_OFFSET = 200   # VB200: 状态读取

# ==================== 事件日志记录器 ====================
class EventLogger:
    """事件日志记录器 - 只记录事件的开始和结束"""
    def __init__(self):
        self.events = {}  # 存储事件状态
    
    def log_event_start(self, event_name, message):
        """记录事件开始"""
        if event_name not in self.events or not self.events[event_name]['active']:
            self.events[event_name] = {
                'active': True,
                'start_time': time.time(),
                'message': message
            }
            logger.warning(f"[事件开始] {message}")
            return True
        return False
    
    def log_event_end(self, event_name, message=None):
        """记录事件结束"""
        if event_name in self.events and self.events[event_name]['active']:
            event = self.events[event_name]
            duration = time.time() - event['start_time']
            end_message = message or f"{event['message']} (持续时间: {duration:.2f}秒)"
            logger.warning(f"[事件结束] {end_message}")
            event['active'] = False
            return True
        return False
    
    def is_event_active(self, event_name):
        """检查事件是否活跃"""
        return event_name in self.events and self.events[event_name]['active']

event_logger = EventLogger()
# ==================== 获取网络摄像头地址 ====================


class CameraScanner:
    """网络摄像头扫描器"""
    
    def __init__(self, ports=[554], timeout=0.3, max_workers=100):
        """初始化扫描器
        
        Args:
            ports: 要扫描的端口列表
            timeout: 单个连接超时时间
            max_workers: 线程池最大工作线程数
        """
        self.ports = ports
        self.timeout = timeout
        self.max_workers = max_workers
    
    def get_local_ip(self):
        """获取本地IP地址"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
            sock.close()
            return local_ip
        except Exception as e:
            logger.error(f"获取本地IP失败: {e}")
            return ""
    
    def scan_ip_for_camera(self, ip):
        """扫描单个IP是否是摄像头"""
        for port in self.ports:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self.timeout)
                result = sock.connect_ex((ip, port))
                
                if result == 0:
                    if port == 554:
                        sock.close()
                        return ip, port, "RTSP摄像头"
                
                sock.close()
                
            except Exception as e:
                logger.debug(f"扫描IP {ip} 端口 {port} 时出错: {e}")
        
        return None
    
    def find_available_camera(self, scan_timeout=30):
        """扫描并返回可用的摄像头IP
        
        Args:
            scan_timeout: 扫描超时时间（秒）
        
        Returns:
            str: 摄像头IP地址，未找到返回None
        """
        try:
            local_ip = self.get_local_ip()
            if not local_ip:
                logger.error("无法获取本地IP")
                return None
            
            network_base = ".".join(local_ip.split(".")[:3])
            ip_list = [f"{network_base}.{i}" for i in range(1, 255)]
            
            logger.info(f"开始扫描网段: {network_base}.0/24")
            
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                future_to_ip = {
                    executor.submit(self.scan_ip_for_camera, ip): ip 
                    for ip in ip_list
                }
                
                for future in as_completed(future_to_ip, timeout=scan_timeout):
                    result = future.result()
                    if result:
                        ip, port, device_type = result
                        logger.info(f"✓ 发现摄像头: {ip}:{port} - {device_type}")
                        return ip
            
            logger.warning("✗ 未发现可用摄像头")
            return None
            
        except Exception as e:
            logger.error(f"✗ 摄像头扫描失败: {e}")
            return None


# ==================== 去重优先级队列 ====================
class DedupPriorityQueue:
    """支持去重的优先级队列"""
    def __init__(self, maxsize=3):
        self.queue = PriorityQueue(maxsize=maxsize)
        self.items_set = set()
        self.lock = threading.Lock()
        # self.mutex_groups = [
        #     {CMD_VALVE_OFF_TOTALLY, CMD_VALVE_ON_OFF_TOTALLY},
        # ]
        logger.info(f"初始化去重优先级队列，最大容量: {maxsize}")
    
    def put(self, item, block=True, timeout=None):
        """放入队列，如果已存在或不放入"""
        priority, value = item
        with self.lock:
            # 互斥组检查：同组内已有其他命令则跳过
            # for group in self.mutex_groups:
            #     if value in group and (group - {value}) & self.items_set:
            #         logger.info(f"互斥命令已在队列/执行中，跳过重复: {value}")
            #         return False
            if value not in self.items_set:
                try:
                    self.queue.put(item, block, timeout)
                    self.items_set.add(value)
                    logger.debug(f"命令入队: {value} (优先级: {priority})")
                    return True
                except queue.Full:
                    logger.warning(f"队列已满，无法放入命令: {value}")
                    return False
            else:
                logger.debug(f"命令已存在队列中，跳过重复: {value}")
                return False
    
    def get(self, block=True, timeout=None):
        """从队列取出（不从 items_set 移除，互斥保护延续到执行完成）"""
        priority, value = self.queue.get(block, timeout)
        return priority, value

    def empty(self):
        return self.queue.empty()

    def task_done(self, value):
        """命令执行完成后，从互斥集合中移除"""
        with self.lock:
            self.items_set.discard(value)
        self.queue.task_done()
    
    def qsize(self):
        return self.queue.qsize()

cmd_queue = DedupPriorityQueue(maxsize=3)

# ==================== PLC 工作线程 ====================
class PLCWorker(threading.Thread):
    """PLC 通讯工作线程 — 通过 VB100 位控制，VB200 读取状态"""

    # VB100 各位的控制对象
    _BIT_NAMES = {0: '开门', 1: '关门', 2: '喷水', 3: '雨刷'}

    def __init__(self, ip=PLC_IP, rack=PLC_RACK, slot=PLC_SLOT,
                 conn_type=PLC_CONN_TYPE):
        super().__init__()
        self.daemon = True
        # self.running = PLC_AUTO_START
        self.running = False

        self.ip = ip
        self.rack = rack
        self.slot = slot
        self.conn_type = conn_type

        # PLC 管理器
        self.plc = PLCManager()

        # 心跳 / 状态轮询
        self.last_heartbeat = time.time()
        self.last_listen_status = time.time()
        self.heartbeat_interval = 5
        self.status_interval = 0.1

        # 连接与状态标志（对外接口不变）
        self.connection_ok = False
        self.concrete_ready = False
        self.concrete_end = False
        self.switch_0_status = False
        self.switch_100_status = False
        self.mode_status = False

        # 断线补发
        self._pending_commands = []

        self._connect()

    # ── PLC 连接 ──────────────────────────────────────

    def _connect(self):
        """连接 PLC"""
        try:
            self.plc.connect(self.ip, self.conn_type, self.rack, self.slot)
            self.connection_ok = True
            logger.info(f"PLC {self.ip} 连接成功")
        except Exception as e:
            logger.error(f"PLC 连接失败: {e}")
            self.connection_ok = False

    def _reconnect(self):
        """重连 PLC"""
        try:
            self.plc.disconnect()
        except Exception:
            pass
        self._connect()
        if self.connection_ok:
            self._flush_pending()

    # ── 位操作 ─────────────────────────────────────────

    def _set_bit(self, list: list[int]):
        """写 VB100 的单个位：先读当前值 → 修改指定位 → 写回"""
        try:

            value = sum(b << i for i, b in enumerate(list))

            self.plc.write_VB(self.plc.client, PLC_CTRL_OFFSET, value)

            return True
        except Exception as e:
            logger.error(f"PLC 写位失败: {e}")
            self.connection_ok = False
            return False


    def _all_off(self):
        """关闭所有输出"""
        try:
            self.plc.write_VB(self.plc.client, PLC_CTRL_OFFSET, 0)
        except Exception:
            self.connection_ok = False

    # ── 状态读取 ───────────────────────────────────────

    def listen_status(self):
        """从 VB200 读取设备状态"""
        try:
            data = self.plc.read_memory_byte(PLC_STATUS_OFFSET, 1, 'db')
            if data is None or len(data) < 1:
                logger.warning("PLC 状态读取失败")
                self.connection_ok = False
                return False

            
            
            bits_status =[(data[0] >> i) & 1 for i in range(8)]
            self.concrete_ready = bool(bits_status[0])
            self.concrete_end = bool(bits_status[1]) 
            self.switch_0_status = bool(bits_status[3])
            self.switch_100_status = bool(bits_status[2])
            self.mode_status = bool(bits_status[4])

            # 状态码 — 保持与旧 Modbus 协议兼容
            
            return True
        except Exception as e:
            logger.error(f"PLC 状态监听失败: {e}")
            self.connection_ok = False
            return False

    def send_heartbeat(self):
        """心跳检测 — 读 VB200 确认 PLC 存活"""
        try:
            data = self.plc.read_memory_byte(PLC_STATUS_OFFSET, 1, 'db')
            if data is not None:
                return True
            logger.warning("PLC 心跳无响应")
            self.connection_ok = False
            return False
        except Exception as e:
            logger.error(f"PLC 心跳失败: {e}")
            self.connection_ok = False
            return False

    # ── 命令发送 ───────────────────────────────────────

    def send_command(self, cmd):
        """发送控制命令 — 写入对应 PLC 位"""
        if not self.connection_ok:
            logger.error(f"PLC 未连接，无法发送命令: {cmd}")
            if cmd not in self._pending_commands:
                self._pending_commands.append(cmd)
            return False

        try:
            if cmd == CMD_VALVE_ON:
                logger.info(f"发送开门命令: {cmd}")
                # 开门: bit0=1 → 2s → bit0=0
                self._set_bit(PLC_BIT_DOOR_OPEN)
                time.sleep(2)
                self._set_bit(PLC_BIT_EXCUTE_STOP)

            elif cmd == CMD_VALVE_OFF:
                logger.info(f"发送关门命令: {cmd}")
                # 关门: bit1=1 → 2s → bit1=0
                self._set_bit(PLC_BIT_DOOR_CLOSE)
                time.sleep(2)
                self._set_bit(PLC_BIT_EXCUTE_STOP)

            elif cmd == CMD_VALVE_OFF_ON:
                logger.info(f"发送防溢出命令: {cmd}")
                # 先关后开: bit1=1 → 1.5s → bit1=0 → 6s → bit0=1 → 1.5s → bit0=0
                self._set_bit(PLC_BIT_DOOR_CLOSE)
                time.sleep(1.5)
                self._set_bit(PLC_BIT_DOOR_CLOSE_STOP)
                time.sleep(6)
                self._set_bit(PLC_BIT_DOOR_OPEN)
                time.sleep(1.5)
                self._set_bit(PLC_BIT_DOOR_OPEN_STOP)
                time.sleep(2)
                self._set_bit(PLC_BIT_EXCUTE_STOP)

            elif cmd == CMD_VALVE_ON_TOTALLY:
                logger.info(f"发送完全开门命令: {cmd}")
                # 完全开门: bit0=1 → 3.2s → bit0=0
                self._set_bit(PLC_BIT_DOOR_OPEN)
                time.sleep(3.2)
                self._set_bit(PLC_BIT_EXCUTE_STOP)

            elif cmd == CMD_VALVE_OFF_TOTALLY:
                logger.info(f"发送完全关门命令: {cmd}")
                # 完全关门: bit1=1 → 3.2s → bit1=0
                self._set_bit(PLC_BIT_DOOR_CLOSE)
                time.sleep(3.2)
                self._set_bit(PLC_BIT_EXCUTE_STOP)

            elif cmd == CMD_SPRAY:
                logger.info(f"发送清洗命令: {cmd}")
                # 喷水+雨刷流程:
                #   bit2=1(喷水) → 3s → bit3=1(雨刷) → 1s → bit2=0 → 10s → bit3=0
                self._set_bit(PLC_BIT_SPRAY)
                time.sleep(3)
                self._set_bit(PLC_BIT_WIPER)
                time.sleep(1.0)
                self._set_bit(PLC_BIT_SPRAY_STOP)
                time.sleep(10)
                self._set_bit(PLC_BIT_WIPER_STOP)

        except Exception as e:
            logger.error(f"发送命令失败 ({cmd}): {e}")
            self.connection_ok = False
            if cmd not in self._pending_commands:
                self._pending_commands.append(cmd)
            return False

        self._flush_pending()
        return True

    def _flush_pending(self):
        """补发断线期间积压的命令"""
        if not self._pending_commands:
            return
        for cmd in self._pending_commands[:]:
            logger.info(f"PLC 补发命令: {cmd}")
            self.send_command(cmd)
            self._pending_commands.remove(cmd)

    # ── 主循环 ─────────────────────────────────────────

    def run(self):
        """线程主循环"""
        while self.running:
            try:
                # 轮询 PLC 状态
                now = time.time()
                if now - self.last_listen_status > self.status_interval:
                    self.listen_status()
                    self.last_listen_status = now

                # 取队列命令
                priority, cmd = cmd_queue.get(timeout=0.1)
                self.send_command(cmd)
                cmd_queue.task_done(cmd)
            except queue.Empty:
                now = time.time()
                if now - self.last_heartbeat > self.heartbeat_interval:
                    if not self.send_heartbeat():
                        self._reconnect()
                    self.last_heartbeat = now
            except Exception as e:
                logger.error(f"PLC 线程异常: {e}")
                time.sleep(1)

        logger.info("PLC 工作线程停止")

    def stop(self):
        """停止线程"""
        logger.info("正在停止 PLC 工作线程...")
        self.running = False
        try:
            self._all_off()
            self.plc.disconnect()
        except Exception:
            pass



# ==================== 主应用 ====================
class DemoApp:
    """主应用类"""
    def __init__(self, model_path=MODEL_PATH, video_source=VIDEO_SOURCE, worker=None):
        logger.info("应用启动")
        self.model_path = model_path
        self.video_source = video_source
        self.worker = worker      # 外部传入已运行的 PLC 实例，或 None
        self._owns_worker = worker is None
        self.model = None

        # 监听状态的参数
        self.ready_status = False
        
        # 污渍检测参数
        self.clarity_threshold = CLARITY_THRESHOLD  # 保留供 GUI 显示颜色判断
        # self.spray_cooldown    = SPRAY_COOLDOWN
        self.lap_var           = 0
        self._clarity_history  = deque(maxlen=5)    # (timestamp, lap_var) 最近 5 次采样
        self._last_sample_t    = 0.0
        self._last_spray_t     = 0.0
        self._sample_interval  = 120                # 采样间隔：2 分钟
        self._sudden_drop_ratio = 0.8               # 下降超过上次值的 20% 视为骤降
        
        # 物体跟踪参数
        self.pos_buffer = deque(maxlen=30)
        self.smoothed_center2 = None   # EMA 平滑后的中心点，消除检测框抖动
        self.stop_frames = 0
        self.moving_frames = 0         # 连续移动帧计数，防止单帧抖动清零
        self.lost_frames = 0           # 连续丢框帧计数，防止检测框临时丢失
        self.stop_threshold = STOP_THRESHOLD
        self.move_epsilon = MOVE_EPSILON
        self.second_stage = False
        self.fixed_center2 = None
        self.fixed_area2 = None
        self.fixed_radius_x = None
        self.fixed_radius_y = None
        self.second_stage_start_time = None  # 进入二阶段的时间戳，30秒后冻结
        
        # 物体1的固定参数（第一阶段）
        self.fixed_center_x1 = None
        self.fixed_center_y1 = None
        self.fixed_left_line_x = None
        self.fixed_right_line_x = None
        
        # 危险检测参数
        self.danger_frames = 0
        self.danger_threshold = DANGER_THRESHOLD
        self.danger_ratio_threshold = DANGER_RATIO_THRESHOLD
        self.danger_alert = False
        self.last_danger_alert = False  # 上一帧的危险状态
        self.danger_start_time = None  # 危险状态开始时间


        # 安全检测参数
        self.safety_frames = 0
        self.safety_threshold = SAFETY_THRESHOLD # 时间阈值（秒）
        self.safety_ratio_threshold = SAFETY_RATIO_THRESHOLD
        self.safety_alert = False
        self.last_safety_alert = False  # 上一帧的安全状态
        self.safety_start_time = None  # 安全状态开始时间

        # ==================== 性能监控参数 ====================
        self.frame_count_in_interval = 0
        # self.total_frames = 0
        self.fps_update_interval = FPS_UPDATE_INTERVAL
        self.last_fps_time = time.time()
        self.current_fps = 0
        self.last_log_time = time.time()
        self.log_interval = LOG_INTERVAL
        self.app_start_time = time.time()

        # ===================== 开关阀门冷却，保护阀门参数===========
        self.last_valve_open_time = 0.0 # 上次开阀时间戳
        self.valve_open_cooldown = 20 # 冷却秒数，防止电机频繁动作

        self.initialize()
    
    def initialize(self):
        """初始化应用"""
        try:
            if self._owns_worker:
                self.worker = PLCWorker()
                self.worker.start()
                logger.info("PLC 工作线程已启动")

            logger.info(f"加载YOLO模型: {self.model_path}")
            self.model = YOLO(self.model_path, task='detect')
            logger.info("YOLO模型加载成功")

        except Exception as e:
            logger.error(f"应用初始化失败: {e}")
            raise

    def _draw_text(self, img, text, pos, font_size=20, color=(0, 255, 0)):
        """在BGR图像上绘制中文文字（使用PIL）"""
        font_paths = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/msyhbd.ttc",
        ]
        font = None
        for fp in font_paths:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, font_size)
                break
        if font is None:
            font = ImageFont.load_default()
        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        draw = ImageDraw.Draw(pil_img)
        r, g, b = int(color[2]), int(color[1]), int(color[0])
        draw.text(pos, text, font=font, fill=(0, 0, 0), stroke_width=2, stroke_fill=(0, 0, 0))
        draw.text(pos, text, font=font, fill=(r, g, b))
        img[:] = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

    def detect_blur(self, img, spray_enabled=True):
        """检测画面洁净度；每 2 分钟采样一次，骤降时才触发喷水"""
        try:
            # 始终显示缓存的清晰度值，不在每帧重复计算 Laplacian
            self._draw_text(img, f'清晰度: {self.lap_var:.1f}', (10, 50),
                           12, (255, 255, 0))

            if not spray_enabled:
                return

            now = time.time()
            if now - self._last_sample_t < self._sample_interval:
                return  # 未到采样时间

            # 仅在采样时刻计算 Laplacian 方差
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            current = cv2.Laplacian(gray, cv2.CV_64F).var()
            self.lap_var = current
            self._last_sample_t = now

            # 判断是否骤降
            is_sudden = False
            if len(self._clarity_history) >= 2:
                prev = self._clarity_history[-1][1]
                if prev > 0:
                    drop_ratio = (prev - current) / prev
                    is_sudden = drop_ratio >= (1 - self._sudden_drop_ratio)
                    logger.info(
                        f"洁净度采样: 当前={current:.1f}, 上次={prev:.1f}, "
                        f"下降幅度={drop_ratio*100:.1f}%"
                    )

            self._clarity_history.append((now, current))

            if is_sudden:
                cmd_queue.put((PRIORITY_LOW, CMD_SPRAY), block=False)
                logger.info(f"检测到洁净度骤降，发送喷水命令 (清晰度: {current:.1f})")
            else:
                logger.info(f"洁净度缓慢变化或正常，不触发喷水 (清晰度: {current:.1f})")

        except Exception as e:
            logger.error(f"污渍检测失败: {e}")
    
    def process_detections(self, img, boxes, clses=None, confs=None):
        """处理检测结果"""
        try:
            if boxes is None or len(boxes) == 0:
                self.second_stage = False
                self.stop_frames = 0
                self.moving_frames = 0
                self.lost_frames = 0
                self.pos_buffer = deque(maxlen=30)
                self.smoothed_center2 = None
                self.fixed_center2 = None
                self.fixed_area2 = None
                self.fixed_radius_x = None
                self.fixed_radius_y = None
                self.second_stage_start_time = None
                self.fixed_center_x1 = None
                self.fixed_center_y1 = None
                self.fixed_left_line_x = None
                self.fixed_right_line_x = None

                return

            # 按类别 ID 重新排序，确保物体身份不依赖置信度顺序
            # class 0=dischargPort(卸料口), 1=collectionPort(收集口), 2=liquidconcrete(混凝土)
            if clses is not None and len(clses) == len(boxes):
                boxes = self._reorder_boxes_by_class(boxes, clses, confs)

            detections = len(boxes)
            self._draw_text(img, f'检测数: {detections}',
                           (10, 65), 20, (0, 255, 0))
            if len(boxes) > 1:
                self.lost_frames = 0
                self.detect_blur(img, spray_enabled=False)  # 二阶段：只显示洁净度，不发送喷水命令
                self._process_object2(img, boxes)
                if not self._is_frozen():
                    self._process_object1(img, boxes)
            elif len(boxes) == 1:
                self.lost_frames += 1
                if self.lost_frames >= 5:  # 连续丢框 5 帧才真正退出二阶段
                    self.second_stage = False
                    self.stop_frames = 0
                    self.moving_frames = 0
                    self.pos_buffer = deque(maxlen=30)
                    self.smoothed_center2 = None
                    self.fixed_center2 = None
                    self.fixed_area2 = None
                    self.fixed_radius_x = None
                    self.fixed_radius_y = None
                    self.second_stage_start_time = None
                    self.detect_blur(img, spray_enabled=True)   # 一阶段：正常检测并允许喷水
                    self._process_object1(img, boxes)
                else:
                    # 仍在迟滞期内，维持二阶段绘制
                    if self.fixed_center2 is not None:
                        self._draw_second_stage(img, boxes)
        
        except Exception as e:
            logger.error(f"检测处理失败: {e}")

    def _reorder_boxes_by_class(self, boxes, clses, confs=None):
        """按类别 ID 排序检测框：class 0→物体1, class 1→物体2, class 2→物体3"""
        ordered = []
        for cls_id in range(3):
            mask = (clses == cls_id)
            if not mask.any():
                continue
            cls_boxes = boxes[mask]
            if confs is not None:
                best = np.argmax(confs[mask])
                ordered.append(cls_boxes[best])
            else:
                ordered.append(cls_boxes[0])
        if ordered:
            return np.array(ordered)
        return boxes

    def _process_object2(self, img, boxes):
        """处理物体2的逻辑"""
        x1_2, y1_2, x2_2, y2_2 = boxes[1]
        center_x2 = int((x1_2 + x2_2) / 2)
        center_y2 = int((y1_2 + y2_2) / 2)
        radius_x = int((x2_2 - x1_2) / 2)
        radius_y = int((y2_2 - y1_2) / 2)
        area2 = int((x2_2 - x1_2) * (y2_2 - y1_2))

        

        # EMA 平滑中心点，消除检测框高频抖动
        alpha = 0.3
        if self.smoothed_center2 is not None:
            sx = alpha * center_x2 + (1 - alpha) * self.smoothed_center2[0]
            sy = alpha * center_y2 + (1 - alpha) * self.smoothed_center2[1]
            self.smoothed_center2 = (int(sx), int(sy))
            float_pos = (sx, sy)
        else:
            self.smoothed_center2 = (center_x2, center_y2)
            float_pos = (float(center_x2), float(center_y2))

        # 将当前平滑位置的浮点值加入滚动缓冲（避免 int 截断累积漂移）
        self.pos_buffer.append(float_pos)

        # 用"当前帧 vs 30帧前"的距离判定移动
        move_dist = 0.0
        if len(self.pos_buffer) >= 30:
            prev = self.pos_buffer[0]
            curr = self.pos_buffer[-1]
            move_dist = np.linalg.norm(np.array(curr) - np.array(prev))

        if move_dist < self.move_epsilon * 8:  # 死区约16px，远大于振动累积漂移
            self.stop_frames += 1
            self.moving_frames = 0
        else:
            self.moving_frames += 1
            if self.moving_frames >= 15:
                self.stop_frames = 0

        if self.stop_frames >= self.stop_threshold:
            # 进入二阶段（跟踪模式，位置未固定）
            self.second_stage = True

            if self.second_stage_start_time is None:
                # 首次进入二阶段，启动 30s 跟踪计时
                self.second_stage_start_time = time.time()
                logger.info(f"进入第二阶段，开始跟踪位置，"
                           f"{FREEZE_SECONDS}秒不移动后固定")

            if self.fixed_center2 is None:
                # 跟踪期间持续更新位置
                elapsed = time.time() - self.second_stage_start_time
                if elapsed >= FREEZE_SECONDS:
                    # 30s 未移动，固定位置
                    self.fixed_center2 = self.smoothed_center2
                    self.fixed_area2 = area2
                    self.fixed_radius_x = radius_x
                    self.fixed_radius_y = radius_y
                    logger.info(f"位置已固定，面积: {self.fixed_area2}")
        else:
            # 物体正在移动
            if self.fixed_center2 is not None:
                # 已固定后移动 → 保持固定
                pass
            elif self.second_stage_start_time is not None:
                # 跟踪期间移动 → 重置计时器，不退出二阶段
                self.second_stage_start_time = time.time()
                logger.info("物体移动，重新计时")

        if self.fixed_center2 is not None:
            self._draw_second_stage(img, boxes)
        else:
            cv2.ellipse(img, (center_x2, center_y2), (radius_x, radius_y),
                       0, 0, 360, (0, 255, 255), 2)
            cv2.circle(img,
                   (center_x2, center_y2-20),
                   20,
                   (0, 255, 255),
                   -1)
            # 跟踪期也绘制物体3（蓝色椭圆）
            if len(boxes) > 2:
                x1_3, y1_3, x2_3, y2_3 = boxes[2]
                cx3 = int((x1_3 + x2_3) / 2)
                cy3 = int((y1_3 + y2_3) / 2)
                rx3 = int((x2_3 - x1_3) / 2)
                ry3 = int((y2_3 - y1_3) / 2)
                cv2.ellipse(img, (cx3, cy3), (rx3, ry3),
                           0, 0, 360, (255, 0, 0), 2)
    
    def _draw_second_stage(self, img, boxes):
        """绘制第二阶段的界面"""
        cv2.ellipse(img, self.fixed_center2, 
                   (self.fixed_radius_x, self.fixed_radius_y), 
                   0, 0, 360, (0, 255, 255), 2)
        # cv2.putText(img, f'Area: {self.fixed_area2}', 
        #            (self.fixed_center2[0], self.fixed_center2[1]-20),
        #            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 255), 2)
        
        area3 = 0
        if len(boxes) > 2:
            x1_3, y1_3, x2_3, y2_3 = boxes[2]
            center_x3 = int((x1_3 + x2_3) / 2)
            center_y3 = int((y1_3 + y2_3) / 2)
            radius_x3 = int((x2_3 - x1_3) / 2)
            radius_y3 = int((y2_3 - y1_3) / 2)
            area3 = int((x2_3 - x1_3) * (y2_3 - y1_3))
            cv2.ellipse(img, (center_x3, center_y3), (radius_x3, radius_y3), 
                       0, 0, 360, (255, 0, 0), 2)
        
        self._draw_text(img, f'溢出率: {(area3 / self.fixed_area2) * 100:.2f}%',
                       (10, 100), 20, (0, 255, 0))
        
        # 危险检测 - 优化日志记录
        current_ratio = area3 / self.fixed_area2 if self.fixed_area2 > 0 else 0
        current_time = time.time()
        if current_ratio > self.danger_ratio_threshold and not self.worker.switch_0_status:
            if self.danger_start_time is None:
                self.danger_start_time = current_time
            # 检查是否持续超过阈值时间
            danger_duration = current_time - self.danger_start_time
            if danger_duration >= self.danger_threshold:
                self.danger_alert = True
                
                # 阀门的冷却保护
                if current_time - self.last_valve_open_time >=self.valve_open_cooldown:
                
                    cmd_queue.put((PRIORITY_HIGH, CMD_VALVE_OFF_ON))
                    self.last_valve_open_time = time.time()
                    self._draw_text(img, '正在关阀...',
                    (10, 140), 20, (0, 0, 255))

        else:
            self.danger_start_time = None  # 重置危险计时器
            self.danger_alert = False

        # if current_ratio < self.safety_ratio_threshold and current_ratio != 0  and not self.worker.switch_100_status:
        #     # 进入安全状态
        #     if self.safety_start_time is None:
        #         self.safety_start_time = current_time
        #     # 检查是否持续超过阈值时间
        #     safety_duration = current_time - self.safety_start_time
        #     if safety_duration >= self.safety_threshold:
        #         self.safety_alert = True
        #         cmd_queue.put((PRIORITY_HIGH, CMD_VALVE_ON))
        #         cv2.putText(img, f'turning on valve...',
        #            (10, 300), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            
        # else:
        #     self.safety_start_time = None  # 重置安全计时器
        #     self.safety_alert = False
            
        
        # 只在危险状态改变时记录日志
        if self.danger_alert and not self.last_danger_alert:
            # 危险警报开始
            event_logger.log_event_start('danger_alert', 
                f"危险警报开始！溢出比例: {current_ratio * 100:.2f}%")
        elif not self.danger_alert and  self.last_danger_alert:
            # 危险警报结束
            event_logger.log_event_end('danger_alert', 
                f"危险警报结束！溢出比例已降至: {current_ratio * 100:.2f}%")
        
        self.last_danger_alert = self.danger_alert

        # 绘制危险指示
        if self.danger_alert:
            if int(time.time() * 2) % 2 == 0:
                cv2.rectangle(img, (5, 5), (img.shape[1]-5, img.shape[0]-5),
                            (0, 0, 255), 5)
            self._draw_text(img, '危险!',
                       (img.shape[1]//2 - 80, 100),
                       50, (0, 0, 255))
        if self.safety_alert:

            if int(time.time() * 2) % 2 == 0:
                cv2.rectangle(img, (5, 5), (img.shape[1]-5, img.shape[0]-5),
                            (0, 255, 0), 5)
            self._draw_text(img, '安全!',
                       (img.shape[1]//2 - 60, 100),
                       50, (0, 255, 0))
        
        self._draw_funnel(img, current_ratio)
    
    def _draw_funnel(self, img, fill_ratio):
        """绘制漏斗指示器"""
        funnel_x = 20
        funnel_y = img.shape[0] - 150
        funnel_width_top = 100
        funnel_width_bottom = 60
        funnel_height = 120
        
        fill_height = int(funnel_height * fill_ratio)
        
        funnel_pts = np.array([
            [funnel_x, funnel_y],
            [funnel_x + funnel_width_top, funnel_y],
            [funnel_x + (funnel_width_top - funnel_width_bottom)//2 + funnel_width_bottom, 
             funnel_y + funnel_height],
            [funnel_x + (funnel_width_top - funnel_width_bottom)//2, funnel_y + funnel_height]
        ], np.int32)
        
        cv2.polylines(img, [funnel_pts], True, (255, 255, 255), 2)
        
        if fill_height > 0:
            fill_y_start = funnel_y + funnel_height - fill_height
            height_from_top = funnel_height - fill_height
            width_ratio = height_from_top / funnel_height
            fill_width_top = funnel_width_bottom + (funnel_width_top - funnel_width_bottom) * (1 - width_ratio)
            fill_x_offset = (funnel_width_top - fill_width_top) / 2
            
            fill_pts = np.array([
                [funnel_x + fill_x_offset, fill_y_start],
                [funnel_x + fill_x_offset + fill_width_top, fill_y_start],
                [funnel_x + (funnel_width_top - funnel_width_bottom)//2 + funnel_width_bottom, 
                 funnel_y + funnel_height],
                [funnel_x + (funnel_width_top - funnel_width_bottom)//2, funnel_y + funnel_height]
            ], np.int32)
            
            if fill_ratio < 0.5:
                fill_color = (0, 255, 0)
            elif fill_ratio < 0.8:
                fill_color = (0, 255, 255)
            else:
                fill_color = (0, 0, 255)
            
            cv2.fillPoly(img, [fill_pts], fill_color)
        
        self._draw_text(img, f'填充: {fill_ratio * 100:.1f}%',
                   (funnel_x + funnel_width_bottom//2 - 50, funnel_y + funnel_height + 25),
                   12, (255, 255, 255))

    def _is_frozen(self):
        """二阶段位置已固定，不再更新参照"""
        return self.fixed_center2 is not None

    def _process_object1(self, img, boxes):
        """处理物体1的逻辑"""
        x1, y1, x2, y2 = boxes[0]
        center_x1 = int((x1 + x2) / 2)
        center_y1 = int((y1 + y2) / 2)
        if self.fixed_center_x1 is None:
            self.fixed_center_x1 = center_x1
            self.fixed_center_y1 = center_y1
            dist_left = abs(center_x1 - x1)
            dist_right = abs(x2 - center_x1)
            half_dist = int((dist_left + dist_right) / 4)
            self.fixed_left_line_x = int(center_x1 - half_dist)
            self.fixed_right_line_x = int(center_x1 + half_dist)
            self.fixed_top_linx_y = int(center_y1 + 50)
            self.fixed_bottom_linx_y = int(center_y1 + 300)

        
        cv2.circle(img, (self.fixed_center_x1, self.fixed_center_y1), 6, (0, 0, 255), -1)
        # cv2.line(img, (self.fixed_left_line_x, 0), (self.fixed_left_line_x, img.shape[0]), (0, 255, 0), 2)
        # cv2.line(img, (self.fixed_right_line_x, 0), (self.fixed_right_line_x, img.shape[0]), (0, 255, 0), 2)
        cv2.rectangle(img, 
              (self.fixed_left_line_x, self.fixed_top_linx_y),   # 左上角
              (self.fixed_right_line_x, self.fixed_bottom_linx_y), # 右下角
              (0, 255, 0), 2)  # 颜色绿色，线宽2

    
    def update_fps(self):
        """更新FPS计算"""
        self.frame_count_in_interval += 1
        # self.total_frames += 1
        
        if self.frame_count_in_interval >= self.fps_update_interval:
            current_time = time.time()
            elapsed = current_time - self.last_fps_time
            self.current_fps = self.frame_count_in_interval / elapsed if elapsed > 0 else 0
            self.last_fps_time = current_time
            self.frame_count_in_interval = 0
            
            if current_time - self.last_log_time > self.log_interval:
                running_time = current_time - self.app_start_time
                hours = int(running_time // 3600)
                minutes = int((running_time % 3600) // 60)
                seconds = int(running_time % 60)
                logger.info(f"当前FPS: {self.current_fps:.2f}, "
                            f"当前洁净度: {self.lap_var:.1f}, "
                           f"运行时间: {hours}h {minutes}m {seconds}s")
                self.last_log_time = current_time
    
    def run(self):
        """主循环"""
        logger.info(f"开始处理视频源: {self.video_source}")
        
        try:
            results = self.model(
                source=self.video_source,
                stream=True,
                stream_buffer=False,
                conf=YOLO_CONF,
                show_labels=False,
                show_conf=False,
                imgsz=YOLO_IMGSZ,
                device='cpu',
                vid_stride=VID_STRIDE,
                verbose=False
            )
            
            for result in results:
                img = result.plot(boxes=False)
                
                
                if result.boxes is not None:
                    boxes = result.boxes.xyxy.cpu().numpy()
                    clses = result.boxes.cls.cpu().numpy()
                    confs = result.boxes.conf.cpu().numpy() if result.boxes.conf is not None else None
                    self.process_detections(img, boxes, clses, confs)
                
                self.update_fps()
                
                self._draw_text(img, f'帧率: {self.current_fps:.2f}', (img.shape[1] - 150, 30),
                           12, (0, 255, 0))

                scale = 1.2
                img_resized = cv2.resize(img,
                    (int(img.shape[1] * scale), int(img.shape[0] * scale)))
                cv2.imshow('result', img_resized)
                
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    logger.info("用户按下退出键")
                    break
            
            running_time = time.time() - self.app_start_time
            hours = int(running_time // 3600)
            minutes = int((running_time % 3600) // 60)
            seconds = int(running_time % 60)
            logger.info(f"视频处理完成，平均FPS: {self.current_fps:.2f},镜头洁净度: {self.lap_var:.1f}, "
                       f"总运行时间: {hours}h {minutes}m {seconds}s")
        
        except Exception as e:
            logger.error(f"视频处理异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
        
        finally:
            self.cleanup()
    
    def cleanup(self):
        """清理资源"""
        logger.info("正在清理资源...")
        cv2.destroyAllWindows()
        if self._owns_worker and self.worker:
            self._control_thread.stop()
            self.worker.stop()
            self.worker.join(timeout=5)
        logger.info("应用已关闭")

# ==================== 主程序入口 ====================
if __name__ == '__main__':
    try:
        # 打印当前配置
        print_config()
        
        # 创建扫描器实例
        # scanner = CameraScanner(ports=[554], timeout=0.3, max_workers=100)
        # camera_ip = scanner.find_available_camera(scan_timeout=30)
        # if not camera_ip:
        #     logger.error("未找到可用摄像头，应用启动中止")
        #     exit(1)
        # app = DemoApp(model_path='best.pt', video_source=f"rtsp://{camera_ip}:554/user=admin&password=&channel=1&stream=0.sdp?")
        # app = DemoApp(model_path='best.pt', video_source=f"rtsp://admin:tx86088106@{camera_ip}:554/Streaming/Channels/101")

        # 使用配置文件中的参数创建应用
        app = DemoApp()
        # app = DemoApp(model_path='./runs/detect/train/weights/best.pt', video_source='./eventvideos/2.mp4')

        # app = DemoApp(model_path='/home/pi/mytask/best_ncnn_model', video_source="rtsp://127.0.0.1:8554/cam")


        # 检测 PLC 通讯
        if not app.worker.connection_ok:
            logger.error("PLC 通讯失败，应用启动中止")
            app.cleanup()
            exit(1)

        logger.info("PLC 通讯正常，启动应用")
        app.run()
    except Exception as e:
        logger.critical(f"应用崩溃: {e}")
        import traceback
        logger.critical(traceback.format_exc())
        raise
