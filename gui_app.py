"""
图形化监控界面 - 视觉监控系统
基于 main.py 应用，提供实时画面展示、状态监控和手动控制功能

采用多进程架构：YOLO 检测运行在独立进程中，彻底解决 GIL 争抢导致的 GUI 卡死。

依赖：pip install pillow
"""

import os
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|avioflags;direct|max_delay;500000"


import cv2,threading,time,logging,gc
import tkinter as tk
from tkinter import ttk, messagebox
import queue as queue_module
from datetime import datetime
from PIL import Image, ImageTk, ImageDraw, ImageFont
import numpy as np
import multiprocessing as mp

from main import (
    PLCWorker, cmd_queue, logger,
    CMD_SPRAY, CMD_VALVE_ON, CMD_VALVE_OFF,
    CMD_VALVE_ON_TOTALLY, CMD_VALVE_OFF_TOTALLY,
    PRIORITY_HIGH,CMD_VALVE_OFF_ON
)
from config import CLARITY_THRESHOLD, MODEL_PATH, VIDEO_SOURCE, print_config


# ==================== 颜色主题 ====================
C = {
    'bg':        '#0d1117',
    'panel':     '#161b22',
    'card':      '#21262d',
    'border':    '#30363d',
    'text':      '#e6edf3',
    'muted':     '#8b949e',
    'green':     '#3fb950',
    'red':       '#f85149',
    'yellow':    '#d29922',
    'blue':      '#388bfd',
    'orange':    '#db6d28',
}


# ==================== 自定义日志处理器 ====================
class TkLogHandler(logging.Handler):
    """将日志消息通过线程安全队列写入 tkinter Text 控件"""

    _LEVEL_FG = {
        'DEBUG':    C['muted'],
        'INFO':     C['green'],
        'WARNING':  C['yellow'],
        'ERROR':    C['red'],
        'CRITICAL': '#ff0000',
    }

    _MAX_LINES = 2000

    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self.text_widget = text_widget
        self._queue: queue_module.Queue = queue_module.Queue()
        self._line_count = 0

    def emit(self, record):
        msg = self.format(record)
        self._queue.put((record.levelname, msg))

    def flush_to_widget(self):
        limit = 50
        count = 0
        tw = self.text_widget
        while not self._queue.empty() and count < limit:
            try:
                level, msg = self._queue.get_nowait()
                fg = self._LEVEL_FG.get(level, C['text'])
                tw.configure(state='normal')
                if self._line_count >= self._MAX_LINES:
                    tw.delete('1.0', f'{self._MAX_LINES // 2}.0')
                    self._line_count = self._MAX_LINES // 2
                tw.insert(tk.END, msg + '\n', level)
                tw.tag_configure(level, foreground=fg)
                tw.see(tk.END)
                tw.configure(state='disabled')
                self._line_count += 1
                count += 1
            except Exception:
                break


# ==================== 状态指示灯小控件 ====================
class Indicator(tk.Frame):
    """带圆形彩灯的单行状态标签"""

    def __init__(self, parent, label: str, **kwargs):
        super().__init__(parent, bg=C['card'], **kwargs)
        self._canvas = tk.Canvas(self, width=14, height=14,
                                  bg=C['card'], highlightthickness=0)
        self._canvas.pack(side=tk.LEFT, padx=(0, 6))
        self._dot = self._canvas.create_oval(2, 2, 12, 12, fill=C['muted'], outline='')
        tk.Label(self, text=label, bg=C['card'], fg=C['text'],
                 font=('Consolas', 10)).pack(side=tk.LEFT)

    def set(self, active: bool, color_on: str = C['green']):
        self._canvas.itemconfig(self._dot, fill=color_on if active else C['muted'])


class SwitchPointer(tk.Frame):
    """开关位置指针控件：左(0%) 中(50%) 右(100%) 三档，实时显示当前位置"""

    _STOPS = {'0': 0.0, '50': 0.5, '100': 1.0}
    _TRACK_H = 6
    _POINTER_R = 8
    _CANVAS_H = 52

    def __init__(self, parent, **kwargs):
        super().__init__(parent, bg=C['card'], **kwargs)
        tk.Label(self, text="开关位置", bg=C['card'], fg=C['muted'],
                 font=('Consolas', 9)).pack(anchor='w', padx=4)
        self._cv = tk.Canvas(self, height=self._CANVAS_H,
                             bg=C['card'], highlightthickness=0)
        self._cv.pack(fill=tk.X, padx=6)
        self._cv.bind('<Configure>', self._redraw)
        self._sw0 = False
        self._sw50 = False
        self._sw100 = False
        self._cv.after(50, self._redraw)

    def update_state(self, sw0: bool, sw50: bool, sw100: bool):
        changed = (sw0 != self._sw0 or sw50 != self._sw50 or sw100 != self._sw100)
        self._sw0, self._sw50, self._sw100 = sw0, sw50, sw100
        if changed:
            self._redraw()

    def _redraw(self, *_):
        cv_ = self._cv
        cv_.delete('all')
        W = cv_.winfo_width()
        if W < 10:
            return
        pad = self._POINTER_R + 4
        track_y = self._POINTER_R + self._TRACK_H // 2 + 4
        x0, x100 = pad, W - pad
        x50 = (x0 + x100) // 2
        cv_.create_rectangle(x0, track_y - self._TRACK_H // 2,
                             x100, track_y + self._TRACK_H // 2,
                             fill=C['border'], outline='', tags='track')
        for x, lbl in [(x0, '0%'), (x50, '50%'), (x100, '100%')]:
            cv_.create_line(x, track_y - 8, x, track_y + 8, fill=C['muted'], width=1)
            cv_.create_text(x, track_y + 14, text=lbl,
                           fill=C['muted'], font=('Consolas', 8))
        if self._sw0:
            px, color = x0, C['yellow']
        elif self._sw50:
            px, color = x50, C['blue']
        elif self._sw100:
            px, color = x100, C['green']
        else:
            px, color = x50, C['muted']
        r = self._POINTER_R
        cv_.create_polygon(px, track_y - r, px + r, track_y,
                          px, track_y + r, px - r, track_y,
                          fill=color, outline=C['bg'], width=1)


# ==================== 跨进程日志转发 ====================
class _QueueLogHandler(logging.Handler):
    """将日志记录放入 mp.Queue，由主进程消费并显示到 UI"""

    def __init__(self, queue: mp.Queue):
        super().__init__()
        self._queue = queue

    def emit(self, record):
        try:
            msg = self.format(record)
            self._queue.put_nowait((record.levelname, msg))
        except Exception:
            pass


# ==================== 检测进程（独立进程，拥有独立 GIL）====================
class DetectionProcess(mp.Process):
    """
    运行 YOLO 检测的独立进程。
    通信方式：
      - frame_queue (mp.Queue):   RGB 帧 → 主进程显示
      - cmd_queue    (mp.Queue):   检测结果触发的串口命令 → 主进程执行
      - state_queue (mp.Queue):   状态快照（FPS, stage, ratio 等）→ 主进程
      - stop_event   (mp.Event):    主进程通知检测进程退出
    """

    _RENDER_MAX_W = 960

    def __init__(self, model_path, input_queue,
                 frame_queue, cmd_queue, state_queue, ctrl_queue,
                 stop_event, log_queue=None):
        super().__init__()
        self.daemon = True
        self.model_path = model_path
        self.input_queue = input_queue
        self.frame_queue = frame_queue
        self.cmd_queue = cmd_queue
        self.state_queue = state_queue
        self.ctrl_queue = ctrl_queue
        self.stop_event = stop_event
        self._log_queue = log_queue

    # ------------------------------------------------------------------
    #  进程入口
    # ------------------------------------------------------------------

    def run(self):
        """子进程入口：加载 YOLO 模型 → 初始化状态 → 运行检测循环"""
        try:
            self._init_imports_and_constants()
            self._init_state()
            self._detection_loop()
        except Exception as e:
            logger.error(f"检测进程异常: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            logger.info("检测进程已退出")

    def _init_imports_and_constants(self):
        """在子进程中导入重型模块并缓存常量引用"""
        import torch
        from ultralytics import YOLO
        from config import (
            YOLO_CONF, YOLO_IMGSZ, VID_STRIDE,
            CLARITY_THRESHOLD,
            DANGER_RATIO_THRESHOLD, DANGER_THRESHOLD,
            SAFETY_RATIO_THRESHOLD, SAFETY_THRESHOLD,
            STOP_THRESHOLD, MOVE_EPSILON,
            FPS_UPDATE_INTERVAL, LOG_INTERVAL,
            FREEZE_SECONDS,
        )
        from main import logger as main_logger
        from main import event_logger
        from main import (
            CMD_SPRAY, CMD_VALVE_OFF_ON,
            PRIORITY_HIGH, PRIORITY_LOW,
        )

        self._torch = torch
        self._YOLO = YOLO
        self._YOLO_CONF = YOLO_CONF
        self._YOLO_IMGSZ = YOLO_IMGSZ
        self._VID_STRIDE = VID_STRIDE

        self._CLARITY_THRESHOLD = CLARITY_THRESHOLD
        self._DANGER_RATIO_THRESHOLD = DANGER_RATIO_THRESHOLD
        self._DANGER_THRESHOLD = DANGER_THRESHOLD
        self._SAFETY_RATIO_THRESHOLD = SAFETY_RATIO_THRESHOLD
        self._SAFETY_THRESHOLD = SAFETY_THRESHOLD
        self._STOP_THRESHOLD = STOP_THRESHOLD
        self._MOVE_EPSILON = MOVE_EPSILON
        self._FPS_UPDATE_INTERVAL = FPS_UPDATE_INTERVAL
        self._LOG_INTERVAL = LOG_INTERVAL

        self._logger = main_logger
        self._event_logger = event_logger
        self._FREEZE_SECONDS = FREEZE_SECONDS

        # 跨进程日志转发：子进程日志 → mp.Queue → 主进程 UI
        if self._log_queue is not None:
            self._log_handler = _QueueLogHandler(self._log_queue)
            self._log_handler.setLevel(logging.INFO)
            self._log_handler.setFormatter(logging.Formatter(
                '%(asctime)s  [%(levelname)s]  %(message)s',
                datefmt='%H:%M:%S',
            ))
            self._logger.addHandler(self._log_handler)

        self._CMD_SPRAY = CMD_SPRAY
        self._CMD_VALVE_OFF_ON = CMD_VALVE_OFF_ON
        self._PRIORITY_HIGH = PRIORITY_HIGH
        self._PRIORITY_LOW = PRIORITY_LOW

    def _init_state(self):
        from collections import deque

        self._model = self._YOLO(self.model_path, task='detect')
        self._draw_scale = 1.0  # 由 _detection_loop 根据首帧计算
        self._logger.info(f"检测进程启动，模型: {self.model_path}")

        # 清晰度 / 污渍检测
        self._lap_var = 0.0
        self._clarity_history = deque(maxlen=5)
        self._last_sample_t = 0.0
        self._last_spray_t = 0.0
        self._sample_interval = 120
        self._sudden_drop_ratio = 0.8

        # 物体跟踪（物体2 - 罐车）
        self._pos_buffer = deque(maxlen=30)
        self._smoothed_center2 = None
        self._stop_frames = 0
        self._moving_frames = 0
        self._lost_frames = 0

        # 二阶段状态
        self._second_stage = False
        self._fixed_center2 = None
        self._fixed_area2 = None
        self._fixed_radius_x = None
        self._fixed_radius_y = None
        self._second_stage_start_time = None

        # 物体1（罐口）固定参数
        self._fixed_center_x1 = None
        self._fixed_center_y1 = None
        self._fixed_left_line_x = None
        self._fixed_right_line_x = None
        self._fixed_top_linx_y = None
        self._fixed_bottom_linx_y = None

        # 危险检测
        self._danger_alert = False
        self._last_danger_alert = False
        self._danger_start_time = None

        # 安全检测
        self._safety_alert = True
        self._last_safety_alert = False
        self._safety_start_time = None

        # FPS
        self._frame_count_in_interval = 0
        self._last_fps_time = time.time()
        self._current_fps = 0.0
        self._last_log_time = time.time()
        self._app_start_time = time.time()

        # 阀门冷却
        self._last_valve_open_time = 0.0
        self._valve_open_cooldown = 20

        # ctrl_queue 传来的远端状态
        self._switch_0_status = False
        self._switch_100_status = False
        self._src_width = 0
        self._src_height = 0

        # 当前溢出比例（由 _draw_second_stage 设置）
        self._current_ratio = 0.0

    def _detection_loop(self):
        log = self._logger
        model = self._model

        with self._torch.no_grad():
            while not self.stop_event.is_set():
                try:
                    frame = self.input_queue.get(timeout=0.5)
                except queue_module.Empty:
                    continue

                # 读取 ctrl_queue 中的远端状态（串口开关、原始分辨率等）
                self._read_ctrl()

                # 计算绘制缩放比（标注参数等比缩小，匹配显示尺寸）
                if self._src_width and frame.shape[1] != self._src_width:
                    self._draw_scale = frame.shape[1] / self._src_width

                result = model(
                    frame, conf=self._YOLO_CONF, imgsz=self._YOLO_IMGSZ,
                    verbose=False,
                )[0]

                img = result.plot(boxes=False)

                if result.boxes is not None:
                    boxes = result.boxes.xyxy.cpu().numpy()
                    clses = result.boxes.cls.cpu().numpy()
                    confs = result.boxes.conf.cpu().numpy() if result.boxes.conf is not None else None
                    self._process_detections(img, boxes, clses, confs)

                self._update_fps()

                self._draw_text(
                    img, f'帧率: {self._current_fps:.1f}',
                    (img.shape[1] - 150, 30),
                    max(12, int(self._draw_scale * 16)), (0, 255, 0),
                )

                self._push_state()

                # 缩放到最大 960px 宽
                h_raw, w_raw = img.shape[:2]
                if w_raw > self._RENDER_MAX_W:
                    scale = self._RENDER_MAX_W / w_raw
                    img = cv2.resize(img, (self._RENDER_MAX_W, int(h_raw * scale)))

                # BGR → RGB（在子进程中完成）
                frame_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

                # 放入帧队列（丢弃旧帧）
                if self.frame_queue.full():
                    try:
                        self.frame_queue.get_nowait()
                    except Exception:
                        pass
                try:
                    self.frame_queue.put_nowait(frame_rgb)
                except Exception:
                    pass

                time.sleep(0.001)

    # ------------------------------------------------------------------
    #  FPS
    # ------------------------------------------------------------------

    def _update_fps(self):
        self._frame_count_in_interval += 1
        if self._frame_count_in_interval >= self._FPS_UPDATE_INTERVAL:
            now = time.time()
            elapsed = now - self._last_fps_time
            self._current_fps = self._frame_count_in_interval / elapsed if elapsed > 0 else 0
            self._last_fps_time = now
            self._frame_count_in_interval = 0
            if now - self._last_log_time > self._LOG_INTERVAL:
                running_time = now - self._app_start_time
                hours = int(running_time // 3600)
                minutes = int((running_time % 3600) // 60)
                seconds = int(running_time % 60)
                self._logger.info(f"当前FPS: {self._current_fps:.2f}, "
                                  f"当前洁净度: {self._lap_var:.1f}, "
                                  f"运行时间: {hours}h {minutes}m {seconds}s")
                self._last_log_time = now

    # ------------------------------------------------------------------
    #  跨进程通信辅助
    # ------------------------------------------------------------------

    def _read_ctrl(self):
        """非阻塞读取 ctrl_queue 中的远端控制状态"""
        while True:
            try:
                msg = self.ctrl_queue.get_nowait()
                if 'switch_0_status' in msg:
                    self._switch_0_status = msg['switch_0_status']
                if 'switch_100_status' in msg:
                    self._switch_100_status = msg['switch_100_status']
                if 'src_width' in msg:
                    self._src_width = msg['src_width']
                if 'src_height' in msg:
                    self._src_height = msg['src_height']
            except queue_module.Empty:
                break

    def _push_state(self):
        """将当前状态快照推送给主进程（非阻塞，满则丢弃旧数据）"""
        state = {
            'current_fps':    self._current_fps,
            'current_ratio':  self._current_ratio,
            'lap_var':        self._lap_var,
            'second_stage':   self._second_stage,
            'danger_alert':   self._danger_alert,
            'safety_alert':   self._safety_alert,
            'det_running':    True,
            'last_frame_time': time.time(),
        }
        if self.state_queue.full():
            try:
                self.state_queue.get_nowait()
            except Exception:
                pass
        try:
            self.state_queue.put_nowait(state)
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  中文绘制辅助
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    #  清晰度检测 / 污渍检测
    # ------------------------------------------------------------------

    def _detect_blur(self, img, spray_enabled=True):
        try:
            self._draw_text(img, f'清晰度: {self._lap_var:.1f}', (10, 50),
                           max(12, int(self._draw_scale * 16)), (255, 255, 0))
            now = time.time()
            if now - self._last_sample_t < self._sample_interval:
                return
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            current = cv2.Laplacian(gray, cv2.CV_64F).var()
            self._lap_var = current
            self._last_sample_t = now
            if not spray_enabled:
                return
            is_sudden = False
            if len(self._clarity_history) >= 2:
                prev = self._clarity_history[-1][1]
                if prev > 0:
                    drop_ratio = (prev - current) / prev
                    is_sudden = drop_ratio >= (1 - self._sudden_drop_ratio)
                    self._logger.info(
                        f"洁净度采样: 当前={current:.1f}, 上次={prev:.1f}, "
                        f"下降幅度={drop_ratio*100:.1f}%"
                    )
            self._clarity_history.append((now, current))
            if is_sudden:
                try:
                    self.cmd_queue.put_nowait((self._PRIORITY_LOW, self._CMD_SPRAY))
                except Exception:
                    pass
                self._logger.info(f"检测到洁净度骤降，发送喷水命令 (清晰度: {current:.1f})")
            else:
                self._logger.info(f"洁净度缓慢变化或正常，不触发喷水 (清晰度: {current:.1f})")
        except Exception as e:
            self._logger.error(f"污渍检测失败: {e}")

    # ------------------------------------------------------------------
    #  检测结果处理（原 DemoApp.process_detections）
    # ------------------------------------------------------------------

    def _process_detections(self, img, boxes, clses=None, confs=None):
        try:
            if boxes is None or len(boxes) == 0:
                self._second_stage = False
                self._stop_frames = 0
                self._moving_frames = 0
                self._lost_frames = 0
                self._pos_buffer.clear()
                self._fixed_center2 = None
                self._fixed_area2 = None
                self._fixed_radius_x = None
                self._fixed_radius_y = None
                self._second_stage_start_time = None
                self._fixed_center_x1 = None
                self._fixed_center_y1 = None
                self._fixed_left_line_x = None
                self._fixed_right_line_x = None
                return

            # 按类别 ID 重新排序，确保物体身份不依赖置信度顺序
            # class 0=dischargPort(卸料口), 1=collectionPort(收集口), 2=liquidconcrete(混凝土)
            if clses is not None and len(clses) == len(boxes):
                boxes = self._reorder_boxes_by_class(boxes, clses, confs)

            detections = len(boxes)
            self._draw_text(img, f'检测数: {detections}',
                           (10, 65),
                           max(14, int(self._draw_scale * 20)), (0, 255, 0))

            if len(boxes) > 1:
                self._lost_frames = 0
                self._detect_blur(img, spray_enabled=False)
                self._process_object2(img, boxes)
                if not self._is_frozen():
                    self._process_object1(img, boxes)
            elif len(boxes) == 1:
                self._lost_frames += 1
                if self._lost_frames >= 5:
                    self._second_stage = False
                    self._stop_frames = 0
                    self._moving_frames = 0
                    self._pos_buffer.clear()
                    self._smoothed_center2 = None
                    self._fixed_center2 = None
                    self._fixed_area2 = None
                    self._fixed_radius_x = None
                    self._fixed_radius_y = None
                    self._second_stage_start_time = None
                    self._detect_blur(img, spray_enabled=True)
                    self._process_object1(img, boxes)
                else:
                    if self._fixed_center2 is not None:
                        self._draw_second_stage(img, boxes)
        except Exception as e:
            self._logger.error(f"检测处理失败: {e}")

    def _reorder_boxes_by_class(self, boxes, clses, confs=None):
        """按类别 ID 排序检测框：class 0→物体1, class 1→物体2, class 2→物体3"""
        ordered = []
        for cls_id in range(3):  # 0=dischargPort, 1=collectionPort, 2=liquidconcrete
            mask = (clses == cls_id)
            if not mask.any():
                continue
            cls_boxes = boxes[mask]
            if confs is not None:
                cls_confs = confs[mask]
                best = np.argmax(cls_confs)
                ordered.append(cls_boxes[best])
            else:
                ordered.append(cls_boxes[0])
        if ordered:
            return np.array(ordered)
        return boxes

    def _process_object2(self, img, boxes):
        x1_2, y1_2, x2_2, y2_2 = boxes[1]
        center_x2 = int((x1_2 + x2_2) / 2)
        center_y2 = int((y1_2 + y2_2) / 2)
        radius_x = int((x2_2 - x1_2) / 2)
        radius_y = int((y2_2 - y1_2) / 2)
        area2 = int((x2_2 - x1_2) * (y2_2 - y1_2))

        alpha = 0.3
        if self._smoothed_center2 is not None:
            sx = alpha * center_x2 + (1 - alpha) * self._smoothed_center2[0]
            sy = alpha * center_y2 + (1 - alpha) * self._smoothed_center2[1]
            self._smoothed_center2 = (int(sx), int(sy))
            float_pos = (sx, sy)
        else:
            self._smoothed_center2 = (center_x2, center_y2)
            float_pos = (float(center_x2), float(center_y2))

        # 将当前平滑位置的浮点值加入滚动缓冲（避免 int 截断累积漂移）
        self._pos_buffer.append(float_pos)

        # 用"当前帧 vs 30帧前"的距离判定移动（振动首尾≈0，真移动首尾持续增大）
        move_dist = 0.0
        if len(self._pos_buffer) >= 30:
            prev = self._pos_buffer[0]
            curr = self._pos_buffer[-1]
            move_dist = np.linalg.norm(np.array(curr) - np.array(prev))

        if move_dist < self._MOVE_EPSILON * 8:  # 死区约16px，远大于振动累积漂移
            self._stop_frames += 1
            self._moving_frames = 0
        else:
            self._moving_frames += 1
            if self._moving_frames >= 15:
                self._stop_frames = 0

        if self._stop_frames >= self._STOP_THRESHOLD:
            # 进入二阶段（跟踪模式，位置未固定）
            self._second_stage = True

            if self._second_stage_start_time is None:
                # 首次进入二阶段，启动 30s 跟踪计时
                self._second_stage_start_time = time.time()
                self._logger.info(f"进入第二阶段，开始跟踪位置，"
                                  f"{self._FREEZE_SECONDS}秒不移动后固定")

            if self._fixed_center2 is None:
                # 跟踪期间持续更新位置
                elapsed = time.time() - self._second_stage_start_time
                if elapsed >= self._FREEZE_SECONDS:
                    # 30s 未移动，固定位置
                    self._fixed_center2 = self._smoothed_center2
                    self._fixed_area2 = area2
                    self._fixed_radius_x = radius_x
                    self._fixed_radius_y = radius_y
                    self._logger.info(f"位置已固定，面积: {self._fixed_area2}")
        else:
            # 物体正在移动
            if self._fixed_center2 is not None:
                # 已固定后移动 → 保持固定（不影响已固定的参照）
                pass
            elif self._second_stage_start_time is not None:
                # 跟踪期间移动 → 重置计时器，不退出二阶段
                self._second_stage_start_time = time.time()
                self._logger.info("物体移动，重新计时")

        if self._fixed_center2 is not None:
            self._draw_second_stage(img, boxes)
        else:
            cv2.ellipse(img, (center_x2, center_y2), (radius_x, radius_y),
                       0, 0, 360, (0, 255, 255), max(1, int(self._draw_scale * 2)))
            cv2.circle(img, (center_x2, center_y2 - 20), max(1, int(self._draw_scale * 20)),
                      (0, 255, 255), -1)
            # 跟踪期也绘制物体3（蓝色椭圆）
            if len(boxes) > 2:
                x1_3, y1_3, x2_3, y2_3 = boxes[2]
                cx3 = int((x1_3 + x2_3) / 2)
                cy3 = int((y1_3 + y2_3) / 2)
                rx3 = int((x2_3 - x1_3) / 2)
                ry3 = int((y2_3 - y1_3) / 2)
                cv2.ellipse(img, (cx3, cy3), (rx3, ry3),
                           0, 0, 360, (255, 0, 0), max(1, int(self._draw_scale * 2)))

    # ------------------------------------------------------------------
    #  二阶段绘制（含危险检测 + 自动命令）
    # ------------------------------------------------------------------

    def _draw_second_stage(self, img, boxes):
        ds = self._draw_scale
        cv2.ellipse(img, self._fixed_center2,
                   (self._fixed_radius_x, self._fixed_radius_y),
                   0, 0, 360, (0, 255, 255), max(1, int(ds * 2)))

        area3 = 0
        if len(boxes) > 2:
            x1_3, y1_3, x2_3, y2_3 = boxes[2]
            center_x3 = int((x1_3 + x2_3) / 2)
            center_y3 = int((y1_3 + y2_3) / 2)
            radius_x3 = int((x2_3 - x1_3) / 2)
            radius_y3 = int((y2_3 - y1_3) / 2)
            area3 = int((x2_3 - x1_3) * (y2_3 - y1_3))
            cv2.ellipse(img, (center_x3, center_y3), (radius_x3, radius_y3),
                       0, 0, 360, (255, 0, 0), max(1, int(ds * 2)))

        ovr_pct = (area3 / self._fixed_area2) * 100 if self._fixed_area2 > 0 else 0
        self._draw_text(img, f'溢出率: {ovr_pct:.2f}%',
                       (10, 100),
                       max(14, int(ds * 20)), (0, 255, 0))

        self._current_ratio = area3 / self._fixed_area2 if self._fixed_area2 > 0 else 0
        current_ratio = self._current_ratio
        now = time.time()

        # 危险检测（从 ctrl_queue 读取串口状态）
        if current_ratio > self._DANGER_RATIO_THRESHOLD and not self._switch_0_status:
            if self._danger_start_time is None:
                self._danger_start_time = now
            if now - self._danger_start_time >= self._DANGER_THRESHOLD:
                self._danger_alert = True
                self._safety_alert = False
                if now - self._last_valve_open_time >= self._valve_open_cooldown:
                    try:
                        self.cmd_queue.put_nowait((self._PRIORITY_HIGH, self._CMD_VALVE_OFF_ON))
                    except Exception:
                        pass
                    self._last_valve_open_time = now
                    self._draw_text(img, '正在关阀...',
                                   (10, 140),
                                   max(14, int(ds * 20)), (0, 0, 255))
        else:
            self._danger_start_time = None
            self._danger_alert = False
            self._safety_alert = True

        # 事件日志（只在状态变化时记录）
        if self._danger_alert and not self._last_danger_alert:
            self._event_logger.log_event_start('danger_alert',
                f"危险警报开始！溢出比例: {current_ratio * 100:.2f}%")
        elif not self._danger_alert and self._last_danger_alert:
            self._event_logger.log_event_end('danger_alert',
                f"危险警报结束！溢出比例已降至: {current_ratio * 100:.2f}%")
        self._last_danger_alert = self._danger_alert

        # 危险/安全闪烁指示
        if self._danger_alert:
            if int(time.time() * 2) % 2 == 0:
                cv2.rectangle(img, (5, 5), (img.shape[1] - 5, img.shape[0] - 5),
                            (0, 0, 255), max(1, int(ds * 5)))
            self._draw_text(img, '危险!',
                           (img.shape[1] // 2 - int(ds * 80), 100),
                           max(24, int(ds * 50)), (0, 0, 255))
        self._draw_funnel(img, current_ratio)

    def _draw_funnel(self, img, fill_ratio):
        ds = self._draw_scale
        funnel_x = 20
        funnel_y = img.shape[0] - 150
        funnel_width_top = 100
        funnel_width_bottom = 60
        funnel_height = 120
        fill_height = int(funnel_height * fill_ratio)

        funnel_pts = np.array([
            [funnel_x, funnel_y],
            [funnel_x + funnel_width_top, funnel_y],
            [funnel_x + (funnel_width_top - funnel_width_bottom) // 2 + funnel_width_bottom,
             funnel_y + funnel_height],
            [funnel_x + (funnel_width_top - funnel_width_bottom) // 2, funnel_y + funnel_height]
        ], np.int32)
        cv2.polylines(img, [funnel_pts], True, (255, 255, 255), max(1, int(ds * 2)))

        if fill_height > 0:
            fill_y_start = funnel_y + funnel_height - fill_height
            height_from_top = funnel_height - fill_height
            width_ratio = height_from_top / funnel_height
            fill_width_top = (funnel_width_bottom +
                             (funnel_width_top - funnel_width_bottom) * (1 - width_ratio))
            fill_x_offset = (funnel_width_top - fill_width_top) / 2
            fill_pts = np.array([
                [funnel_x + fill_x_offset, fill_y_start],
                [funnel_x + fill_x_offset + fill_width_top, fill_y_start],
                [funnel_x + (funnel_width_top - funnel_width_bottom) // 2 + funnel_width_bottom,
                 funnel_y + funnel_height],
                [funnel_x + (funnel_width_top - funnel_width_bottom) // 2, funnel_y + funnel_height]
            ], np.int32)
            if fill_ratio < 0.5:
                fill_color = (0, 255, 0)
            elif fill_ratio < 0.8:
                fill_color = (0, 255, 255)
            else:
                fill_color = (0, 0, 255)
            cv2.fillPoly(img, [fill_pts], fill_color)

        self._draw_text(img, f'填充: {fill_ratio * 100:.1f}%',
                       (funnel_x + funnel_width_bottom // 2 - 50, funnel_y + funnel_height + 25),
                       max(12, int(ds * 16)), (255, 255, 255))

    def _process_object1(self, img, boxes):
        x1, y1, x2, y2 = boxes[0]
        center_x1 = int((x1 + x2) / 2)
        center_y1 = int((y1 + y2) / 2)
        if self._fixed_center_x1 is None:
            self._fixed_center_x1 = center_x1
            self._fixed_center_y1 = center_y1
            dist_left = abs(center_x1 - x1)
            dist_right = abs(x2 - center_x1)
            half_dist = int((dist_left + dist_right) / 4)
            self._fixed_left_line_x = int(center_x1 - half_dist)
            self._fixed_right_line_x = int(center_x1 + half_dist)
            self._fixed_top_linx_y = int(center_y1 + 50)
            self._fixed_bottom_linx_y = int(center_y1 + 200)
        cv2.circle(img, (self._fixed_center_x1, self._fixed_center_y1),
                  max(1, int(self._draw_scale * 6)), (0, 0, 255), -1)
        cv2.rectangle(img,
                     (self._fixed_left_line_x, self._fixed_top_linx_y),
                     (self._fixed_right_line_x, self._fixed_bottom_linx_y),
                     (0, 255, 0), max(1, int(self._draw_scale * 2)))

    def _is_frozen(self):
        """二阶段位置已固定，不再更新参照"""
        return self._fixed_center2 is not None


# ==================== 主 GUI 类 ====================
class MonitorGUI:
    """AI系统主界面（多进程版）"""

    _FRAME_INTERVAL = 33
    _STATUS_INTERVAL = 200
    _LOG_INTERVAL = 100
    _SIDEBAR_W = 280
    _LOG_H = 120
    _HEADER_H = 48

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AI系统主界面")

        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        win_w = min(1280, max(960, int(sw * 0.70)))
        win_h = min(720, max(600, int(sh * 0.75)))
        self._WIN_W = win_w
        self._WIN_H = win_h
        self._VIDEO_H = win_h - self._HEADER_H - self._LOG_H - 24

        x = (sw - win_w) // 2
        y = (sh - win_h) // 2
        self.root.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.root.minsize(800, 500)
        self.root.resizable(True, True)
        self.root.configure(bg=C['bg'])
        self._is_fullscreen = False

        # ---- 多进程 IPC 对象 ----
        self._log_queue = mp.Queue(maxsize=200)
        self.state_queue = mp.Queue(maxsize=5)
        self.ctrl_queue = mp.Queue(maxsize=5)
        self.frame_queue = mp.Queue(maxsize=2)
        self.cmd_queue_mp = mp.Queue(maxsize=10)
        self.stop_event = mp.Event()
        self._input_queue = mp.Queue(maxsize=2)

        # 检测进程状态快照（由后台 state_queue 更新，主线程零 IPC 读取）
        self._state_snapshot = {
            'current_fps': 0.0,
            'current_ratio': 0.0,
            'lap_var': 0.0,
            'second_stage': False,
            'danger_alert': False,
            'safety_alert': False,
            'det_running': False,
            'last_frame_time': time.time(),
        }

        self.det_process: DetectionProcess | None = None
        self._det_running = False
        self._start_time = time.time()
        self._last_display_time = 0.0
        self._last_gc_time = 0.0
        self._user_stopped = False
        self._det_gen = 0
        self._src_width = 640
        self._src_height = 480

        # RTSP 捕获线程（主进程读帧 → 子进程推理）
        self._cap: cv2.VideoCapture | None = None
        self._capture_thread: threading.Thread | None = None
        self._capture_running = False

        # ---- 命令桥接线程 ----
        self._bridge_running = True
        self._bridge_thread = threading.Thread(target=self._cmd_bridge, daemon=True)
        self._bridge_thread.start()

        # 录制相关（所有文件操作在后台线程执行，不阻塞主线程）
        self._recording = False
        self._rec_running = True
        self._rec_frame_queue: queue_module.Queue = queue_module.Queue(maxsize=30)
        self._rec_event = threading.Event()
        self._in_stage2_session = False
        self._stage2_exit_time: float | None = None
        self._auto_recording_enabled = False
        self._rec_thread = threading.Thread(target=self._recording_worker, daemon=True)
        self._rec_thread.start()

        self._build_ui()
        self._install_log_handler()
        self._init_plc()
        self._start_detection()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._tick_frame()
        self._tick_status()
        self._tick_log()

    # ------------------------------------------------------------------ #
    #  命令桥接：检测进程 → DedupPriorityQueue                               #
    # ------------------------------------------------------------------ #

    def _cmd_bridge(self):
        while self._bridge_running:
            try:
                priority, command = self.cmd_queue_mp.get(timeout=0.5)
                cmd_queue.put((priority, command))
            except queue_module.Empty:
                continue
            except Exception as e:
                logger.error(f"命令桥接异常: {e}")

    # ------------------------------------------------------------------ #
    #  UI 构建                                                              #
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        header = tk.Frame(self.root, bg=C['panel'], height=self._HEADER_H)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="AI系统主界面",
                 bg=C['panel'], fg=C['text'],
                 font=('Microsoft YaHei', 13, 'bold')).pack(side=tk.LEFT, padx=18, pady=10)
        self._uptime_label = tk.Label(header, text="运行时长: 00:00:00",
                                       bg=C['panel'], fg=C['muted'],
                                       font=('Consolas', 10))
        self._uptime_label.pack(side=tk.RIGHT, padx=18)
        self._conn_label = tk.Label(header, text="● PLC 未连接",
                                     bg=C['panel'], fg=C['red'],
                                     font=('Microsoft YaHei', 10))
        self._conn_label.pack(side=tk.RIGHT, padx=(18, 8))
        self._mode_label = tk.Label(header, text="模式：自动",
                                     bg=C['panel'], fg=C['green'],
                                     font=('Microsoft YaHei', 10))
        self._mode_label.pack(side=tk.RIGHT, padx=8)
        self._fs_btn = tk.Button(header, text="⛶ 全屏",
                                 bg=C['panel'], fg=C['muted'],
                                 font=('Microsoft YaHei', 9),
                                 relief=tk.FLAT, bd=0, cursor='hand2',
                                 activebackground=C['border'], activeforeground=C['text'],
                                 command=self._toggle_fullscreen)
        self._fs_btn.pack(side=tk.RIGHT, padx=(0, 4))
        self.root.bind('<F11>', lambda _: self._toggle_fullscreen())
        self.root.bind('<Escape>', lambda _: self._exit_fullscreen())

        body = tk.Frame(self.root, bg=C['bg'], height=self._VIDEO_H + 20)
        body.pack(fill=tk.X, padx=8, pady=(6, 4))
        body.pack_propagate(False)

        sidebar_outer = tk.Frame(body, bg=C['bg'], width=self._SIDEBAR_W)
        sidebar_outer.pack(side=tk.RIGHT, fill=tk.Y)
        sidebar_outer.pack_propagate(False)
        _sb = ttk.Scrollbar(sidebar_outer, orient=tk.VERTICAL)
        _sb.pack(side=tk.RIGHT, fill=tk.Y)
        _canvas = tk.Canvas(sidebar_outer, bg=C['bg'], highlightthickness=0,
                            yscrollcommand=_sb.set)
        _canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        _sb.config(command=_canvas.yview)
        sidebar = tk.Frame(_canvas, bg=C['bg'])
        _canvas_win = _canvas.create_window((0, 0), window=sidebar, anchor='nw')

        def _on_sidebar_resize(_):
            _canvas.itemconfig(_canvas_win, width=_canvas.winfo_width())
        _canvas.bind('<Configure>', _on_sidebar_resize)

        def _on_sidebar_content_change(event):
            _canvas.configure(scrollregion=_canvas.bbox('all'))
        sidebar.bind('<Configure>', _on_sidebar_content_change)

        def _on_mousewheel(event):
            _canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        _canvas.bind('<MouseWheel>', _on_mousewheel)
        sidebar.bind('<MouseWheel>', _on_mousewheel)

        video_frame = tk.Frame(body, bg=C['panel'])
        video_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 6))
        tk.Label(video_frame, text="实时画面",
                 bg=C['panel'], fg=C['muted'],
                 font=('Microsoft YaHei', 9)).pack(anchor=tk.W, padx=10, pady=(6, 2))
        self._video_label = tk.Label(video_frame, bg='#000000',
                                      text="等待视频流…",
                                      fg=C['muted'],
                                      font=('Microsoft YaHei', 13))
        self._video_label.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        self._build_status_card(sidebar)
        self._build_detection_card(sidebar)
        self._build_control_card(sidebar)

        log_frame = tk.Frame(self.root, bg=C['panel'])
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 6))
        log_header = tk.Frame(log_frame, bg=C['panel'])
        log_header.pack(fill=tk.X, padx=10, pady=(6, 2))
        tk.Label(log_header, text="系统日志",
                 bg=C['panel'], fg=C['muted'],
                 font=('Microsoft YaHei', 9)).pack(side=tk.LEFT)
        clear_btn = tk.Button(log_header, text="清空",
                              bg=C['panel'], fg=C['muted'],
                              font=('Microsoft YaHei', 8),
                              relief=tk.FLAT, bd=0, cursor='hand2',
                              command=self._clear_log,
                              activebackground=C['border'], activeforeground=C['text'])
        clear_btn.pack(side=tk.RIGHT)
        self._log_text = tk.Text(log_frame, height=7,
                                  bg=C['card'], fg=C['text'],
                                  font=('Consolas', 9),
                                  state='disabled', relief=tk.FLAT, bd=0,
                                  selectbackground=C['border'])
        sb2 = ttk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=sb2.set)
        sb2.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(0, 8))
        self._log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

    def _card(self, parent: tk.Frame, title: str) -> tk.Frame:
        wrapper = tk.Frame(parent, bg=C['card'])
        wrapper.pack(fill=tk.X, pady=(0, 5))
        tk.Label(wrapper, text=title, bg=C['card'], fg=C['muted'],
                 font=('Microsoft YaHei', 9, 'bold')).pack(anchor=tk.W, padx=12, pady=(8, 4))
        return wrapper

    def _kv_row(self, parent, key: str, default='--', key_width=10) -> tk.Label:
        row = tk.Frame(parent, bg=C['card'])
        row.pack(fill=tk.X, padx=12, pady=2)
        tk.Label(row, text=key, width=key_width, anchor=tk.W,
                 bg=C['card'], fg=C['muted'],
                 font=('Consolas', 10)).pack(side=tk.LEFT)
        val = tk.Label(row, text=default, bg=C['card'], fg=C['text'],
                       font=('Consolas', 10, 'bold'))
        val.pack(side=tk.RIGHT)
        return val

    def _build_status_card(self, parent):
        card = self._card(parent, "系统状态")
        self._fps_val = self._kv_row(card, "FPS")
        self._clarity_val = self._kv_row(card, "清晰度")
        self._qsize_val = self._kv_row(card, "命令队列")
        self._stage_val = self._kv_row(card, "检测阶段")
        tk.Frame(card, bg=C['card'], height=6).pack()

    def _build_detection_card(self, parent):
        card = self._card(parent, "设备状态")
        ind_frame = tk.Frame(card, bg=C['card'])
        ind_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
        self._ind_ready = Indicator(ind_frame, "混凝土就绪")
        self._ind_ready.pack(fill=tk.X, pady=2)
        self._ind_end = Indicator(ind_frame, "卸料完毕")
        self._ind_end.pack(fill=tk.X, pady=2)
        self._ind_danger = Indicator(ind_frame, "危险警报")
        self._ind_danger.pack(fill=tk.X, pady=2)
        self._ind_safety = Indicator(ind_frame, "安全状态")
        self._ind_safety.pack(fill=tk.X, pady=(2, 4))
        self._sw_pointer = SwitchPointer(card)
        self._sw_pointer.pack(fill=tk.X, padx=12, pady=(0, 6))

        ratio_frame = tk.Frame(card, bg=C['card'])
        ratio_frame.pack(fill=tk.X, padx=12, pady=(0, 10))
        tk.Label(ratio_frame, text="溢出比例", bg=C['card'], fg=C['muted'],
                 font=('Consolas', 9)).pack(anchor=tk.W)
        style = ttk.Style()
        style.configure('Ratio.Horizontal.TProgressbar',
                        background=C['blue'], troughcolor=C['bg'],
                        borderwidth=0, thickness=10)
        self._ratio_bar = ttk.Progressbar(ratio_frame, length=240, maximum=100,
                                           style='Ratio.Horizontal.TProgressbar')
        self._ratio_bar.pack(fill=tk.X, pady=3)
        self._ratio_val = tk.Label(ratio_frame, text="0.0%",
                                    bg=C['card'], fg=C['text'],
                                    font=('Consolas', 10))
        self._ratio_val.pack(anchor=tk.E)

    def _build_control_card(self, parent):
        card = self._card(parent, "手动控制")
        _btn_common = dict(font=('Microsoft YaHei', 10), relief=tk.FLAT, bd=0,
                           cursor='hand2', pady=5)

        run_row = tk.Frame(card, bg=C['card'])
        run_row.pack(fill=tk.X, padx=12, pady=(0, 8))
        self._start_btn = tk.Button(run_row, text="▶  启动程序", bg=C['green'], fg=C['text'],
                                     activebackground='#2ea043', activeforeground=C['text'],
                                     command=self._cmd_start_program,
                                     state=tk.DISABLED, **_btn_common)
        self._start_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        self._stop_btn = tk.Button(run_row, text="■  停止程序", bg=C['red'], fg=C['text'],
                                    activebackground='#da3633', activeforeground=C['text'],
                                    command=self._cmd_stop_program,
                                    state=tk.NORMAL, **_btn_common)
        self._stop_btn.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        tk.Frame(card, bg=C['border'], height=1).pack(fill=tk.X, padx=12, pady=(0, 6))
        self._rec_btn = tk.Button(card, text="○  录制关", bg=C['card'], fg=C['muted'],
                                   activebackground=C['border'], activeforeground=C['text'],
                                   command=self._cmd_record_toggle,
                                   state=tk.DISABLED, **_btn_common)
        self._rec_btn.pack(fill=tk.X, padx=12, pady=(0, 6))
        tk.Frame(card, bg=C['border'], height=1).pack(fill=tk.X, padx=12, pady=(0, 6))

        tk.Button(card, text="💧  手动清洗镜头", bg=C['blue'], fg=C['text'],
                  activebackground='#2670cc', activeforeground=C['text'],
                  command=self._cmd_spray, **_btn_common).pack(fill=tk.X, padx=12, pady=(0, 6))

        row1 = tk.Frame(card, bg=C['card'])
        row1.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Button(row1, text="半开", bg=C['green'], fg=C['text'],
                  activebackground='#2ea043', activeforeground=C['text'],
                  command=self._cmd_valve_on,
                  **_btn_common).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        tk.Button(row1, text="半关", bg=C['red'], fg=C['text'],
                  activebackground='#da3633', activeforeground=C['text'],
                  command=self._cmd_valve_off,
                  **_btn_common).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        row2 = tk.Frame(card, bg=C['card'])
        row2.pack(fill=tk.X, padx=12, pady=(0, 10))
        tk.Button(row2, text="全开", bg=C['orange'], fg=C['text'],
                  activebackground='#b45a22', activeforeground=C['text'],
                  command=self._cmd_valve_on_totally,
                  **_btn_common).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3))
        tk.Button(row2, text="全关", bg=C['yellow'], fg='#000000',
                  activebackground='#a07818', activeforeground=C['text'],
                  command=self._cmd_valve_off_totally,
                  **_btn_common).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

    # ------------------------------------------------------------------ #
    #  初始化与启动                                                         #
    # ------------------------------------------------------------------ #

    def _install_log_handler(self):
        handler = TkLogHandler(self._log_text)
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            '%(asctime)s  [%(levelname)s]  %(message)s',
            datefmt='%H:%M:%S',
        ))
        logger.addHandler(handler)
        self._log_handler = handler

    def _init_plc(self):
        self._plc_worker = PLCWorker()
        self._plc_worker.start()
        if not self._plc_worker.connection_ok:
            logger.warning("PLC 未连接，部分功能不可用")

    def _start_detection(self):
        if getattr(self, '_det_starting', False):
            return
        self._det_starting = True
        try:
            if self.det_process is not None:
                self.stop_event.set()
                if self.det_process.is_alive():
                    self.det_process.terminate()  # 非阻塞，不 join
                self.det_process = None

            self.stop_event.clear()
            while not self.frame_queue.empty():
                try:
                    self.frame_queue.get_nowait()
                except Exception:
                    break
            while not self.state_queue.empty():
                try:
                    self.state_queue.get_nowait()
                except Exception:
                    break
            while not self.ctrl_queue.empty():
                try:
                    self.ctrl_queue.get_nowait()
                except Exception:
                    break
            while not self.cmd_queue_mp.empty():
                try:
                    self.cmd_queue_mp.get_nowait()
                except Exception:
                    break
            while not self._input_queue.empty():
                try:
                    self._input_queue.get_nowait()
                except Exception:
                    break

            self.det_process = DetectionProcess(
                model_path=MODEL_PATH,
                input_queue=self._input_queue,
                frame_queue=self.frame_queue,
                cmd_queue=self.cmd_queue_mp,
                state_queue=self.state_queue,
                ctrl_queue=self.ctrl_queue,
                stop_event=self.stop_event,
                log_queue=self._log_queue,
            )
            self.det_process.start()

            self._start_capture()

            self._start_time = time.time()
            self._det_running = True
            self._det_gen += 1
            self._set_run_buttons(running=True)
            self._state_snapshot['last_frame_time'] = time.time()
            logger.info("检测进程已启动")

        except Exception as e:
            logger.error(f"启动检测进程失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            messagebox.showerror("启动失败", f"检测进程启动失败: {e}")
        finally:
            self._det_starting = False

    # ------------------------------------------------------------------ #
    #  RTSP 捕获线程（主进程读帧 → 子进程推理）                                  #
    # ------------------------------------------------------------------ #

    def _start_capture(self):
        """启动捕获线程（实际视频连接在子线程内延迟初始化，不阻塞主线程）"""
        self._stop_capture()
        self._capture_running = True
        self._cap = None
        self._capture_thread = threading.Thread(target=self._capture_worker, daemon=True)
        self._capture_thread.start()
        logger.info(f"捕获线程已启动: {VIDEO_SOURCE}")

    def _stop_capture(self):
        """停止捕获线程（非阻塞，不 join）"""
        self._capture_running = False
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        self._capture_thread = None

    def _capture_worker(self):
        """捕获线程：在子线程内打开视频源，循环读取帧并发送给子进程推理"""
        # 在后台线程中初始化 VideoCapture，不阻塞主线程
        cap = None
        try:
            cap = cv2.VideoCapture(VIDEO_SOURCE)
            if not cap.isOpened():
                logger.error(f"无法打开视频源: {VIDEO_SOURCE}")
                return
            self._cap = cap
        except Exception as e:
            logger.error(f"打开视频源失败: {e}")
            return
        last_rec_frame_time = 0
        while self._capture_running and not self.stop_event.is_set():
            try:
                ret, frame = cap.read()
                if not ret:
                    logger.warning("视频帧读取失败，等待重试...")
                    time.sleep(0.5)
                    continue

                # 先缩小到最大 960px 宽，减少 IPC 序列化开销
                h, w = frame.shape[:2]
                self._src_width, self._src_height = w, h
                # 通过 ctrl_queue 推送原始分辨率给检测进程
                ctrl_msg = {'src_width': w, 'src_height': h}
                if self.ctrl_queue.full():
                    try:
                        self.ctrl_queue.get_nowait()
                    except Exception:
                        pass
                try:
                    self.ctrl_queue.put_nowait(ctrl_msg)
                except Exception:
                    pass
                if w > 960:
                    scale = 960 / w
                    frame = cv2.resize(frame, (960, int(h * scale)))
                # 放入输入队列（满则丢弃旧帧）
                if self._input_queue.full():
                    try:
                        self._input_queue.get_nowait()
                    except Exception:
                        pass
                try:
                    self._input_queue.put_nowait(frame)
                except Exception:
                    pass
                # 录制：推送纯画面（无绘制叠加）到录制队列
                if self._recording:
                    now = time.time()
                    if now - last_rec_frame_time >= 0.067:  # 限制 ~15fps
                        try:
                            self._rec_frame_queue.put_nowait(frame.copy())
                        except queue_module.Full:
                            pass
                        last_rec_frame_time = now
            except Exception as e:
                logger.error(f"捕获线程异常: {e}")
                time.sleep(0.5)

    # ------------------------------------------------------------------ #
    #  定时更新回调                                                         #
    # ------------------------------------------------------------------ #

    def _tick_frame(self):
        try:
            self._update_frame()
        except Exception:
            pass
        self.root.after(self._FRAME_INTERVAL, self._tick_frame)

    def _tick_status(self):
        try:
            self._update_status()
        except Exception:
            pass
        if time.time() - self._last_gc_time > 300:
            threading.Thread(target=gc.collect, daemon=True).start()
            self._last_gc_time = time.time()
        self.root.after(self._STATUS_INTERVAL, self._tick_status)

    def _tick_log(self):
        try:
            # 消费子进程日志队列 → UI
            if self._log_handler and self._log_queue is not None:
                lq = self._log_queue
                while not lq.empty():
                    try:
                        level, msg = lq.get_nowait()
                        self._log_handler._queue.put((level, msg))
                    except Exception:
                        break
                self._log_handler.flush_to_widget()
        except Exception:
            pass
        self.root.after(self._LOG_INTERVAL, self._tick_log)

    def _update_frame(self):
        if not self._det_running:
            return

        if not self._user_stopped:
            elapsed = time.time() - self._state_snapshot.get('last_frame_time', time.time())
            if elapsed > 30:
                logger.warning(f"检测进程已挂起 {elapsed:.0f} 秒，强制重启...")
                if self._recording:
                    self._stop_recording()
                self.stop_event.set()
                self.root.after(1000, self._start_detection)
                return

        now = time.time()
        if now - self._last_display_time < 0.067:
            return
        self._last_display_time = now

        try:
            frame_rgb = self.frame_queue.get_nowait()
        except (queue_module.Empty, Exception):
            return

        if self._auto_recording_enabled:
            current_stage = self._state_snapshot.get('second_stage', False)
            if current_stage:
                if not self._in_stage2_session:
                    logger.info("进入二阶段，自动开始录制")
                    self._start_recording()
                    self._in_stage2_session = True
                self._stage2_exit_time = None
            else:
                if self._in_stage2_session and self._stage2_exit_time is None:
                    self._stage2_exit_time = now
                elif self._in_stage2_session and now - self._stage2_exit_time >= 30:
                    logger.info("二阶段已退出超过30秒，自动停止录制")
                    self._stop_recording()

        # 主进程只做显示（没有 cv2 操作）
        pil_img = Image.fromarray(frame_rgb)

        # 缩放图片以适应当前视频区域大小（避免图片太大被裁剪导致边缘文字不可见）
        try:
            lw = self._video_label.winfo_width()
            lh = self._video_label.winfo_height()
            if lw > 10 and lh > 10:
                # 等比例缩放，完全填充label区域
                iw, ih = pil_img.size
                scale = min(lw / iw, lh / ih)
                new_w = int(iw * scale)
                new_h = int(ih * scale)
                if new_w != iw or new_h != ih:
                    pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)
        except Exception:
            pass

        prev_ref = getattr(self, '_photo_ref', None)
        if prev_ref is None or prev_ref.width() != pil_img.width or prev_ref.height() != pil_img.height:
            self._photo_ref = ImageTk.PhotoImage(pil_img)
        else:
            self._photo_ref.paste(pil_img)
        self._video_label.configure(image=self._photo_ref, text='')

    def _update_status(self):
        # 检测进程存活检查
        if self._det_running and self.det_process and not self.det_process.is_alive():
            self._det_running = False
            gen = self._det_gen
            self.root.after(0, lambda g=gen: self._on_detection_stopped(g))
            return

        if not self._det_running:
            self._update_plc_status()
            return

        # 非阻塞消费 state_queue，拿到最新快照（零 IPC 阻塞）
        while not self.state_queue.empty():
            try:
                self._state_snapshot = self.state_queue.get_nowait()
            except queue_module.Empty:
                break

        s = self._state_snapshot

        elapsed = int(time.time() - self._start_time)
        h, m, sec = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
        self._uptime_label.config(text=f"运行时长: {h:02d}:{m:02d}:{sec:02d}")

        fps = s.get('current_fps', 0.0)
        self._fps_val.config(text=f"{fps:.1f}", fg=C['blue'])

        lap = s.get('lap_var', 0.0)
        if lap > 0:
            clarity_color = C['green'] if lap >= CLARITY_THRESHOLD else C['red']
            self._clarity_val.config(text=f"{lap:.1f}", fg=clarity_color)

        qsz = cmd_queue.qsize()
        self._qsize_val.config(text=str(qsz),
                                fg=C['orange'] if qsz > 0 else C['text'])

        stage = "二阶段" if s.get('second_stage', False) else "一阶段"
        self._stage_val.config(text=stage)

        self._sync_plc_to_shared()

        self._ind_danger.set(s.get('danger_alert', False), C['red'])
        self._ind_safety.set(s.get('safety_alert', False), C['green'])

        ratio_pct = min(100.0, s.get('current_ratio', 0.0) * 100)
        self._ratio_bar['value'] = ratio_pct
        self._ratio_val.config(text=f"{ratio_pct:.1f}%")

        bar_color = C['red'] if ratio_pct >= 80 else (C['yellow'] if ratio_pct >= 50 else C['blue'])
        if getattr(self, '_last_bar_color', '') != bar_color:
            ttk.Style().configure('Ratio.Horizontal.TProgressbar', background=bar_color)
            self._last_bar_color = bar_color

    def _update_plc_status(self):
        if self._plc_worker:
            ok = self._plc_worker.connection_ok
            mode = self._plc_worker.mode_status
            self._conn_label.config(
                text="● PLC 已连接" if ok else "● PLC 未连接",
                fg=C['green'] if ok else C['red'],
            )
            self._mode_label.config(
                text="模式：自动" if mode else "模式：手动",
                fg=C['green'] if mode else C['red'],
            )
            w = self._plc_worker
            self._ind_ready.set(w.concrete_ready, C['green'])
            self._ind_end.set(w.concrete_end, C['blue'])
            sw50 = not w.switch_0_status and not w.switch_100_status
            self._sw_pointer.update_state(w.switch_0_status, sw50, w.switch_100_status)

    def _sync_plc_to_shared(self):
        if not self._plc_worker:
            return
        w = self._plc_worker
        self._update_plc_status()
        # 通过 ctrl_queue 推送串口状态给检测进程
        msg = {
            'concrete_ready': w.concrete_ready,
            'concrete_end': w.concrete_end,
            'switch_0_status': w.switch_0_status,
            'switch_100_status': w.switch_100_status,
        }
        if self.ctrl_queue.full():
            try:
                self.ctrl_queue.get_nowait()
            except Exception:
                pass
        try:
            self.ctrl_queue.put_nowait(msg)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  启停控制                                                            #
    # ------------------------------------------------------------------ #

    def _set_run_buttons(self, running: bool):
        if running:
            self._start_btn.config(state=tk.DISABLED)
            self._stop_btn.config(state=tk.NORMAL)
            self._rec_btn.config(state=tk.NORMAL)
            self._update_rec_btn_style()
        else:
            self._start_btn.config(state=tk.NORMAL)
            self._stop_btn.config(state=tk.DISABLED)
            self._rec_btn.config(state=tk.DISABLED, bg=C['card'],
                                  fg=C['muted'], text="○  录制关")
            if self._recording:
                self._stop_recording()

    def _update_rec_btn_style(self):
        if self._recording:
            self._rec_btn.config(text="⏹  录制中", bg=C['red'], fg=C['text'],
                                  activebackground='#da3633')
        elif self._auto_recording_enabled:
            self._rec_btn.config(text="●  录制开", bg=C['green'], fg=C['text'],
                                  activebackground='#2ea043')
        else:
            self._rec_btn.config(text="○  录制关", bg=C['card'], fg=C['muted'],
                                  activebackground=C['border'])

    def _toggle_fullscreen(self):
        self._is_fullscreen = not self._is_fullscreen
        self.root.attributes('-fullscreen', self._is_fullscreen)
        self._fs_btn.config(text="⛶ 退出全屏" if self._is_fullscreen else "⛶ 全屏")

    def _exit_fullscreen(self):
        if self._is_fullscreen:
            self._toggle_fullscreen()

    def _on_detection_stopped(self, gen):
        if gen != self._det_gen:
            return
        self._det_running = False
        self._stop_capture()
        if self._user_stopped:
            self._user_stopped = False
            self._set_run_buttons(running=False)
            self._video_label.configure(image='', text="已停止")
            self._conn_label.config(text="● 已停止", fg=C['muted'])
            self._mode_label.config(text="模式：--", fg=C['muted'])
            logger.info("检测程序已停止")
        else:
            logger.warning("检测进程意外退出，5秒后自动重启...")
            self._video_label.configure(image='', text="意外退出，即将重启…")
            self.root.after(5000, self._start_detection)

    def _cmd_record_toggle(self):
        self._auto_recording_enabled = not self._auto_recording_enabled
        if not self._auto_recording_enabled and self._recording:
            self._stop_recording()
        self._update_rec_btn_style()
        logger.info(f"自动录制已{'开启' if self._auto_recording_enabled else '关闭'}"
                     f"{'，将在进入二阶段时自动录制' if self._auto_recording_enabled else ''}")

    _REC_MAX_BYTES = 50 * 1024 * 1024 * 1024

    def _enforce_storage_limit(self, out_dir: str):
        try:
            files = [
                os.path.join(out_dir, f)
                for f in os.listdir(out_dir)
                if os.path.isfile(os.path.join(out_dir, f))
            ]
            files.sort(key=lambda p: os.path.getmtime(p))
            total = sum(os.path.getsize(p) for p in files)
            while total > self._REC_MAX_BYTES and files:
                oldest = files.pop(0)
                size = os.path.getsize(oldest)
                os.remove(oldest)
                total -= size
                logger.info(f"存储超限，已删除旧录像: {os.path.basename(oldest)} ({size / 1024 / 1024:.1f} MB)")
        except Exception as e:
            logger.warning(f"存储清理异常: {e}")

    def _start_recording(self):
        if not self._det_running:
            return
        self._recording = True
        self._rec_event.set()
        self._update_rec_btn_style()
        logger.info("正在开始录制...")

    def _stop_recording(self):
        self._recording = False
        self._rec_event.set()
        self._update_rec_btn_style()
        self._in_stage2_session = False
        self._stage2_exit_time = None

    def _recording_worker(self):
        """后台录制线程：循环读取帧并写入文件，所有文件操作不阻塞主线程"""
        recorder = None
        while self._rec_running:
            if self._recording:
                if recorder is None:
                    # 在后台线程创建 VideoWriter（不阻塞主线程）
                    try:
                        w, h = self._src_width, self._src_height
                        if w > 960:
                            scale = 960 / w
                            w = 960
                            h = int(h * scale)
                        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'recordings')
                        os.makedirs(out_dir, exist_ok=True)
                        self._enforce_storage_limit(out_dir)
                        filename = datetime.now().strftime('%Y%m%d_%H%M%S') + '.mp4'
                        filepath = os.path.join(out_dir, filename)
                        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                        fps_val = max(1.0, self._state_snapshot.get('current_fps', 15.0))
                        recorder = cv2.VideoWriter(filepath, fourcc, fps_val, (w, h))
                        logger.info(f"录制已开始: {filepath} ({w}x{h} @ {fps_val:.1f}fps)")
                    except Exception as e:
                        logger.error(f"创建录像文件失败: {e}")
                        self._recording = False
                        self.root.after(0, self._update_rec_btn_style)
                        recorder = None

                if recorder is not None:
                    try:
                        frame = self._rec_frame_queue.get(timeout=0.5)
                        recorder.write(frame)
                    except queue_module.Empty:
                        pass
                    except Exception as e:
                        logger.error(f"录制写帧失败: {e}")
                        if recorder is not None:
                            recorder.release()
                            recorder = None
                        self._recording = False
                        self.root.after(0, self._update_rec_btn_style)
            else:
                if recorder is not None:
                    recorder.release()
                    recorder = None
                    logger.info("录制已停止，文件已保存至 recordings/ 目录")
                    # 清空残留帧
                    while not self._rec_frame_queue.empty():
                        try:
                            self._rec_frame_queue.get_nowait()
                        except Exception:
                            break
                    self.root.after(0, self._update_rec_btn_style)
                self._rec_event.wait(timeout=0.5)
                self._rec_event.clear()

        if recorder is not None:
            recorder.release()

    def _cmd_start_program(self):
        if self._det_running:
            return
        logger.info("用户启动检测程序")
        self._video_label.configure(image='', text="正在启动…")
        self.root.after(0, self._start_detection)

    def _cmd_stop_program(self):
        if not self._det_running:
            return
        logger.info("用户停止检测程序")
        self._user_stopped = True
        self._stop_btn.config(state=tk.DISABLED)
        self.stop_event.set()

    # ------------------------------------------------------------------ #
    #  控制命令                                                            #
    # ------------------------------------------------------------------ #

    def _cmd_spray(self):
        cmd_queue.put((PRIORITY_HIGH, CMD_SPRAY), block=False)
        logger.info("手动触发清洗镜头命令")

    def _cmd_valve_on(self):
        cmd_queue.put((PRIORITY_HIGH, CMD_VALVE_ON), block=False)

    def _cmd_valve_off(self):
        cmd_queue.put((PRIORITY_HIGH, CMD_VALVE_OFF), block=False)

    def _cmd_valve_on_totally(self):
        cmd_queue.put((PRIORITY_HIGH, CMD_VALVE_ON_TOTALLY), block=False)

    def _cmd_valve_off_totally(self):
        cmd_queue.put((PRIORITY_HIGH, CMD_VALVE_OFF_TOTALLY), block=False)
        
    def _cmd_valve_off_on(self):
        cmd_queue.put((PRIORITY_HIGH, CMD_VALVE_OFF_ON), block=False)
    def _clear_log(self):
        self._log_text.configure(state='normal')
        self._log_text.delete('1.0', tk.END)
        self._log_text.configure(state='disabled')
        if self._log_handler:
            self._log_handler._line_count = 0

    # ------------------------------------------------------------------ #
    #  关闭                                                               #
    # ------------------------------------------------------------------ #

    def _on_close(self):
        logger.info("用户关闭窗口，正在退出…")
        self._user_stopped = True

        # 先调度窗口销毁，窗口会立刻关闭
        self.root.after(100, self.root.destroy)

        # 通知所有子线程/进程退出（非阻塞，不 join）
        self.stop_event.set()
        self._capture_running = False
        self._bridge_running = False
        self._rec_running = False
        self._rec_event.set()

        # 释放摄像头资源
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None

        if self.det_process is not None and self.det_process.is_alive():
            self.det_process.terminate()

        if self._plc_worker:
            self._plc_worker.stop()


# ==================== 程序入口 ====================
if __name__ == '__main__':
    mp.freeze_support()
    print_config()

    root = tk.Tk()
    try:
        root.iconbitmap(default='')
    except Exception:
        pass

    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except Exception:
        pass

    MonitorGUI(root)
    root.mainloop()
    os._exit(0)  # 强制退出，跳过 Python 清理（Windows 下 mp.Queue 残留易卡死）
