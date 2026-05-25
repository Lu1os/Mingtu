"""
YOLOv8 WebSocket 视频流检测服务端（三模型版本）

功能：
1. 接收手机 App 推送的摄像头视频帧（JPEG 格式）
2. 使用三 YOLOv8 模型进行实时目标检测：
   - model_custom: 自定义分割模型（盲道/斑马线/路沿）
   - model_official: 官方检测模型（人/车辆）
   - model_traffic_light: 红绿灯专用模型（红灯/绿灯）
3. 返回合并后的检测结果（JSON 格式）
4. 可选：本地预览窗口

使用方法：
1. 安装依赖：pip install ultralytics opencv-python websockets numpy Pillow
2. 运行：python yolo_websocket_server.py
3. 手机 App 连接 ws://你的电脑IP:8765
重启服务器即可生效。如果实际测试中发现距离阈值（1.5% / 6%）不合适，调整 FAR_RATIO_MAX 和 NEAR_RATIO_MIN 两个常量就行
协议：
- 接收：JPEG 字节流（单帧）
- 返回：JSON 格式检测结果
"""

import asyncio
import websockets
import cv2
import numpy as np
import threading
import warnings
warnings.filterwarnings("ignore", message=".*iCCP.*")  # ★ 过滤 libpng sRGB 警告
from PIL import Image, ImageDraw, ImageFont

# 抑制 PIL libpng sRGB 警告（每帧刷屏）
warnings.filterwarnings("ignore", message=".*iCCP.*")
import json
import time
from concurrent.futures import ThreadPoolExecutor
import os
from datetime import datetime
from ultralytics import YOLO

# ============ 配置 ============
# ★ 小助手服务器 IP
# 本地测试用 127.0.0.1，连接队友电脑时改成对方 IP
ASSISTANT_HOST = "127.0.0.1"

# 自定义模型路径（分割模型：盲道/斑马线/路沿石）
MODEL_CUSTOM_PATH = os.path.join(os.path.dirname(__file__), "models", "custom_seg.pt")

# 官方模型路径（检测模型：人/车辆）
OFFICIAL_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "yolov8s.pt")

# 官方模型白名单（保留的类别）
ALLOWED_CLASSES = {0, 1, 2, 3, 5, 7, 9}  # person, bicycle, car, motorcycle, bus, truck, traffic_light

# 官方模型类别中英文映射（用于预览窗口显示）
OFFICIAL_CLASS_NAMES_EN = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    5: "bus",
    7: "truck",
    9: "traffic_light"
}

# 官方模型类别中文名（用于JSON返回）
OFFICIAL_CLASS_NAMES_CN = {
    0: "人",
    1: "自行车",
    2: "汽车",
    3: "摩托车",
    5: "公交车",
    7: "卡车",
    9: "红绿灯"
}

# 自定义模型（盲道）类别中英文映射
CUSTOM_CLASS_NAMES_EN = {
    0: "stop_curb",      # 停止盲道
    1: "straight_curb",  # 直行盲道
    2: "zebra_crossing",  # 斑马线
    3: "curb"            # 路沿石
}

HOST = "0.0.0.0"  # 监听所有网卡，局域网可访问
PORT = 8765        # App 连接此端口（不要改）
ASSISTANT_PORT = 8768  # 小助手连接此端口
SHOW_PREVIEW = True  # 是否显示本地预览窗口

# ★ 启动时获取屏幕尺寸（用于预览窗口自适应）
_SCREEN_SIZE = None
def _get_screen_size():
    global _SCREEN_SIZE
    if _SCREEN_SIZE is not None:
        return _SCREEN_SIZE
    try:
        import tkinter as tk
        _root = tk.Tk()
        _SCREEN_SIZE = (_root.winfo_screenwidth() - 80, _root.winfo_screenheight() - 80)
        _root.destroy()
    except Exception:
        _SCREEN_SIZE = (1200, 800)
    return _SCREEN_SIZE
CONFIDENCE = 0.6  # 置信度阈值（0.5→0.6，减少暗画面幻觉误报）

# 推理缩放尺寸（缩小图像加速推理，坐标按比例还原）
INFER_WIDTH = 640
INFER_HEIGHT = 480

# 画面亮度检测（防止纯黑/太暗画面产生幻觉误报）
FRAME_BRIGHTNESS_MIN = 15  # 平均亮度低于此值视为无效帧，跳过播报

# 预览窗口颜色配置 (BGR格式)
COLOR_CUSTOM = (0, 255, 0)      # 绿色 - 自定义模型（盲道）
COLOR_OFFICIAL = (255, 0, 0)    # 蓝色 - 官方模型（人/车辆）

# 分割掩码透明度
MASK_ALPHA = 0.4

# ============ 加载字体（用于中文显示） ============
def load_font():
    """加载中文字体"""
    font_paths = [
        "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
        "C:/Windows/Fonts/simhei.ttf",     # 黑体
        "C:/Windows/Fonts/simsun.ttc",     # 宋体
    ]

    for font_path in font_paths:
        try:
            return ImageFont.truetype(font_path, 22)
        except:
            continue

    print(f"[{datetime.now()}] 警告: 未找到中文字体，将使用拼音显示")
    return None

FONT_CN = load_font()

# ============ 绘制中文文字工具 ============
def put_chinese_text(img, text, position, font_size=22, text_color=(255, 255, 255), bg_color=(0, 0, 0)):
    """在图像上绘制中文文字（使用PIL）"""
    if FONT_CN is None:
        return img  # 如果没有字体，返回原图

    # 将 OpenCV 图像 (BGR numpy) 转换为 PIL 图像 (RGB)
    pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil_img)

    # 获取文字尺寸
    bbox = draw.textbbox(position, text, font=FONT_CN)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # 绘制背景矩形
    x, y = position
    draw.rectangle([(x, y - text_height - 3), (x + text_width + 6, y + 3)], fill=bg_color)

    # 绘制文字
    draw.text(position, text, font=FONT_CN, fill=text_color)

    # 转回 OpenCV 格式
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)

# ============ 加载模型 ============
print(f"[{datetime.now()}] " + "="*50)

# ============ CONFIG 集中管理（方便调参）============
CONFIG = {
    # --- 网络 ---
    "ASSISTANT_HOST": ASSISTANT_HOST,
    "PORT": PORT,
    "ASSISTANT_PORT": ASSISTANT_PORT,
    # --- 模型 ---
    "CONFIDENCE_THRESHOLD": CONFIDENCE,
    # --- 运动追踪 ---
    "MOTION_TRACK_COOLDOWN": 2.0,
    "MOVING_THRESHOLD_PX_PER_SEC": 15.0,
    # --- 事件确认（防抖）---
    "EVENT_CONFIRM_FRAMES": 3,
    "EVENT_HISTORY_LEN": 5,
    # --- 盲道追踪 ---
    "CURB_HISTORY_LEN": 5,
    "CURB_MISS_CONFIRM": 3,
    # --- 盲道偏移检测 ---
    "DEVIATION_THRESHOLD_RATIO": 0.20,
    "DEVIATION_COOLDOWN": 5.0,
    # --- 路口检测 ---
    "INTERSECTION_COOLDOWN": 8.0,
    # --- 距离估算 ---
    "FAR_RATIO_MAX": 0.015,
    "NEAR_RATIO_MIN": 0.060,
    # --- 方向感知 ---
    "DIR_LEFT_RATIO_MAX": 0.35,
    "DIR_RIGHT_RATIO_MIN": 0.65,
    # --- 帧率自适应 ---
    "FPS_RECENT_FRAME_COUNT": 10,
    "FPS_DEFAULT": 15.0,
    # --- OCR 场景文字识别 ---
    "SCENE_TEXT_COOLDOWN": 5.0,
    "SCENE_TEXT_MAX_LINES": 5,
    "SCENE_TEXT_MIN_CONF": 0.5,
    # --- 状态持久化 ---
    "STATE_SAVE_INTERVAL": 30.0,
    "STATE_VERSION": 1,
    # --- 全局发送冷却 ---
    "GLOBAL_SEND_COOLDOWN": 8.0,
    "FINGERPRINT_COOLDOWN": 30.0,
    # --- 斑马线偏移检测 ---
    "ZEBRA_DEVIATION_COOLDOWN": 1.5,          # ★ 3→1.5秒，减半冷却时间
    "ZEBRA_DEVIATION_THRESHOLD_RATIO": 0.15,  # ★ 0.08→0.15，降低灵敏度避免误报
    # --- 斑马线路口提醒 ---
    "ALIGN_REMINDER_COOLDOWN": 10.0,
    # --- 红绿灯检测（Feature 8）---
    "TRAFFIC_LIGHT_COOLDOWN": 2.0,           # 变灯播报冷却（秒）
    "TRAFFIC_LIGHT_STABLE_FRAMES": 1,        # ★ 改为1帧：检测到立即播报，不需要防抖
    "TRAFFIC_LIGHT_LOST_TIMEOUT": 8.0,       # 红绿灯消失超过N秒重置状态
    "TRAFFIC_LIGHT_NO_SIGNAL_WAIT": 10.0,    # ★ 10秒：给盲人足够时间抬手机对准红绿灯
    "TRAFFIC_LIGHT_RED_REPEAT_INTERVAL": 2.5, # ★ 红灯重复播报间隔（秒）- 需求2.5秒
    # --- 阶段3斑马线过马路 ---
    "ZEBRA_CROSSING_GREEN_WAIT": 0.5,        # ★ 绿灯后等待0.5秒播报"可通行"（给用户反应时间）- 减少延迟
    "ZEBRA_DEVIATION_ANNOUNCE_COOLDOWN": 1.5, # ★ 斑马线偏移播报冷却（避免频繁播报）- 减半
    "CROSSING_EXIT_GPS_NOTIFY_INTERVAL": 5.0, # ★ 退出路口模式时GPS通知间隔（秒）
}

# 状态文件路径（放在脚本同目录下）
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG["STATE_FILE"] = os.path.join(_SCRIPT_DIR, "yolo_state.json")

# ============ 红绿灯专用模型（第三模型 - Feature 8）============
TRAFFIC_LIGHT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "traffic_light.pt")

# ★ 红绿灯模型类别：不再硬编码，从模型自动读取
# 新模型只有"红灯"和"绿灯"两个类别（ID=0,1），旧模型有6个类别（ID=0-5）
# 无论哪种模型，代码都能自动适配
TRAFFIC_LIGHT_CLASSES_CN = {}  # 运行时从模型填充
TRAFFIC_LIGHT_COLOR_CLASSES = set()  # 运行时从模型填充（包含"红"或"绿"的类别ID）

# ============ 红绿灯状态机全局状态 ============
_traffic_light_state = "unknown"      # 当前红绿灯状态：red/green/none/unknown
_traffic_light_last_change_time = 0.0  # 上次变灯时间
_last_traffic_light_time = 0.0         # 上次检测到红绿灯的时间
_traffic_light_stable_frames = 0       # 连续检测到同一颜色的帧数（防抖）
IN_INTERSECTION_MODE = False           # 是否处于路口模式（红绿灯检测模式）
_traffic_light_confirmed = False       # 是否已确认红绿灯状态（避免刚进入路口就播报）
_traffic_light_no_signal_announced = False  # 是否已播报"无信号灯"（只播一次）
_crossing_green_passed = False          # ★ 绿灯通行中标志：绿灯后停止红绿灯检测，专注障碍物和斑马线偏移
# ============ 斑马线偏移检测全局状态 ============
_last_zebra_deviation_time = 0.0

# ★ 斑马线偏移检测状态机
_zebra_deviation_active = False      # 是否处于偏移检测模式（全屏斑马线时才开启）
_zebra_last_seen_time = 0.0          # 上次检测到斑马线的时间
ZEBRA_EXIT_TIMEOUT = 10.0            # 斑马线消失超过10秒后关闭偏移检测
_zebra_announced_in_session = False  # ★ 非导航模式下斑马线是否已播报过（整个会话只播一次）
ZEBRA_FULL_SCREEN_RATIO = 0.40       # ★ 阶段1→2转换阈值（站上斑马线）- 0.65→0.40降低
ZEBRA_DEVIATION_ACTIVATION_RATIO = 0.10  # ★ 阶段3偏移检测激活阈值（走动中斑马线面积小，10%即可）

# ★ 斑马线引导模式（导航联动 - 三阶段状态机）
# 阶段1 seeking: 找斑马线，引导用户走向斑马线
# 阶段2 on_zebra: 站上斑马线，提示用户抬手机看红绿灯
# 阶段3 crossing: 过马路，检测红绿灯 + 斑马线偏移
_zebra_guide_phase = ""             # 当前阶段："" / "seeking" / "on_zebra" / "crossing"
_zebra_guide_direction = ""         # 转弯方向（"左转"/"右转"/"左前方"/"右前方"）
_zebra_guide_remaining = 0.0        # 距离路口剩余距离
_zebra_guide_last_announce = 0.0    # 上次引导播报时间
ZEBRA_GUIDE_COOLDOWN = 3.0          # 引导播报冷却（秒）
ZEBRA_ON_ZEBRA_AREA = 0.60          # ★ 0.75→0.60，降低阈值提高灵敏度
_zebra_guide_phone_hint = False      # 是否已提示用户抬手机（只提示一次）
_zebra_phone_hint_ever = False       # ★ 整个会话中是否已播过"抬起手机"（防止反复进出斑马线重复播报）
_zebra_guide_on_zebra_time = 0.0     # 进入 on_zebra 阶段的时间
_zebra_no_light_announced = False    # 是否已播报"未检测到红绿灯"
_zebra_guide_crossing_announced = False  # ★ 是否已播报"找到红绿灯了"（防止反复检测到又丢失导致重复）
_zebra_on_zebra_zebra_lost_time = 0.0  # on_zebra 阶段斑马线消失的时间（用于缓冲）
_pending_no_light_msg = False         # ★ 待发送的"未检测到红绿灯"消息（绕过冷却）
_pending_on_zebra_msg = False         # ★ 待发送的"站在斑马线上"消息（绕过冷却）
_pending_zebra_guide_intro_msg = False  # ★ 拟人化：待发送的路口引导流程介绍消息
_pending_intersection_mode_msg = False  # ★ 拟人化：待发送的红绿灯模式激活说明消息
_pending_green_pass_msg = False  # ★ 待发送的"绿灯可通行"消息（单独发送，不和障碍物合并）

# ★ 阶段3（crossing）专用状态
_crossing_green_first_time = 0.0         # ★ 首次检测到绿灯的时间（用于延迟播报）
_crossing_announced_green_pass = False   # ★ 是否已播报"绿灯可通行"
_crossing_announced_deviation_mode = False  # ★ 是否已播报"开启偏移检测模式"
_crossing_exit_announced = False         # ★ 是否已播报"路口模式退出"
_crossing_last_red_remind_time = 0.0     # ★ 红灯重复提醒的上次播报时间
_crossing_deviation_announced_time = 0.0  # ★ 斑马线偏移播报时间（用于冷却）

# ============ 路口模式扩展状态 ============
_last_align_reminder_time = 0.0

# ============ 前向距离估算（单目透视几何）============
# 原理：假设摄像头固定在胸前，高度 CAMERA_HEIGHT_M = 1.4m，向下倾斜 CAMERA_ANGLE_DEG = 30°
# 物体底部y坐标越接近画面底部 → 距离越近（透视投影规律）
_CAMERA_HEIGHT_M = 1.4        # 摄像头高度（米）
_CAMERA_ANGLE_DEG = 30.0       # 摄像头朝下倾斜角度（度）
_VANISHING_ROW_RATIO = 0.25    # 消失线在画面的位置（上方25%，此距离=∞）
_DISTANCE_TBL = [              # 障碍物底部像素比例 → 估算距离（米）
    (1.00, 0.8),   # 底部贴底 → 0.8米（臂展内）
    (0.85, 1.5),   # 85%   → 1.5米
    (0.70, 2.5),   # 70%   → 2.5米
    (0.55, 4.0),   # 55%   → 4.0米
    (0.40, 6.0),   # 40%   → 6.0米
    (0.25, 20.0),  # 消失线附近 → 很远
]

def _estimate_forward_distance_meters(bbox: list, frame_height: int) -> float:
    """
    根据检测框底部y坐标，估算障碍物到摄像头的直线距离（米）。
    使用透视几何近似：假设摄像头水平安装，向下倾斜30°。
    底部y比例 → 角度 → 距离 = h / tan(θ)
    """
    if frame_height <= 0:
        return -1.0
    _, _, _, y2 = bbox
    bottom_ratio = y2 / frame_height  # 0=顶部，1=底部
    # 反算：bottom_ratio接近1 → 角度大(朝下) → 距离近
    import math
    angle_rad = math.radians(_CAMERA_ANGLE_DEG)
    # 透视映射：消失线以上=无穷远，以下按tan映射
    if bottom_ratio < _VANISHING_ROW_RATIO:
        return 999.0   # 消失线以上，视为极远
    effective_ratio = (bottom_ratio - _VANISHING_ROW_RATIO) / (1.0 - _VANISHING_ROW_RATIO)
    effective_ratio = max(0.01, min(0.99, effective_ratio))
    theta = math.atan2(1.0, (1.0 / effective_ratio) * math.tan(angle_rad))
    dist = _CAMERA_HEIGHT_M / max(math.tan(theta), 0.01)
    return round(dist, 1)

def _get_stop_distance_text(meters: float) -> str:
    """
    将距离（米）转换为自然语言停止建议。
    已知盲道时，前方障碍的停止距离建议。
    """
    if meters < 0:
        return ""
    if meters < 1.0:
        return "请立即停下"
    elif meters < 2.0:
        return "前方约一米，请准备停下"
    elif meters < 3.5:
        return f"约{int(meters)}米，请注意"
    elif meters < 6.0:
        return f"约{int(meters)}米"
    else:
        return ""  # 太远不播报

# ============ 状态持久化 ============
_last_save_time = 0.0

def _save_state():
    """保存当前跨帧状态到 JSON 文件"""
    try:
        state = {
            "version": CONFIG["STATE_VERSION"],
            "timestamp": datetime.now().isoformat(),
            "event_history": [list(frame) for frame in _event_history],
            "curb_history": _curb_history[-CONFIG["CURB_HISTORY_LEN"]:],
            "last_frame_curbs": _last_frame_curbs,
            "tracked_objects": {k: v for k, v in _tracked_objects.items()},
            "last_deviation_time": _last_deviation_time,
            "last_intersection_warning_time": _last_intersection_warning_time,
            "in_intersection_mode": _in_intersection_mode,
            "last_zebra_deviation_time": _last_zebra_deviation_time,
            "last_align_reminder_time": _last_align_reminder_time,
            # ★ Feature 8: 红绿灯状态持久化
            "traffic_light_state": _traffic_light_state,
            "traffic_light_last_change_time": _traffic_light_last_change_time,
            "last_traffic_light_time": _last_traffic_light_time,
            "in_traffic_light_mode": IN_INTERSECTION_MODE,
            "traffic_light_stable_frames": _traffic_light_stable_frames,
            "traffic_light_confirmed": _traffic_light_confirmed,
            "traffic_light_no_signal_announced": _traffic_light_no_signal_announced,
            # ★ 斑马线引导状态持久化
            "zebra_guide_phase": _zebra_guide_phase,
            "zebra_guide_direction": _zebra_guide_direction,
            "zebra_guide_last_announce": _zebra_guide_last_announce,
            "zebra_guide_phone_hint": _zebra_guide_phone_hint,
            # ★ 斑马线偏移检测状态持久化
            "zebra_deviation_active": _zebra_deviation_active,
            "zebra_last_seen_time": _zebra_last_seen_time,
        }
        state_file = CONFIG["STATE_FILE"]
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        print(f"[{datetime.now()}] 状态已保存: {state_file}")
    except Exception as e:
        print(f"[{datetime.now()}] 状态保存失败: {e}")

def _load_state():
    """启动时加载上次保存的状态"""
    global _event_history, _curb_history, _last_frame_curbs, _tracked_objects
    global _last_deviation_time, _last_intersection_warning_time, _in_intersection_mode
    global _last_zebra_deviation_time, _last_align_reminder_time
    global _traffic_light_state, _traffic_light_last_change_time, _last_traffic_light_time
    global IN_INTERSECTION_MODE, _traffic_light_stable_frames
    global _traffic_light_confirmed, _traffic_light_no_signal_announced
    global _zebra_guide_phase, _zebra_guide_direction, _zebra_guide_last_announce, _zebra_guide_phone_hint
    global _zebra_deviation_active, _zebra_last_seen_time

    state_file = CONFIG["STATE_FILE"]
    if not os.path.exists(state_file):
        return

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        version = state.get("version", 0)
        if version != CONFIG["STATE_VERSION"]:
            print(f"[{datetime.now()}] 状态文件版本不匹配，从零初始化")
            return

        _event_history = [set(tuple(ev) for ev in frame) for frame in state.get("event_history", [])]
        _curb_history = state.get("curb_history", [])
        _last_frame_curbs = state.get("last_frame_curbs", [])
        for k, v in state.get("tracked_objects", {}).items():
            _tracked_objects[k] = v
        _last_deviation_time = state.get("last_deviation_time", 0.0)
        _last_intersection_warning_time = state.get("last_intersection_warning_time", 0.0)
        _in_intersection_mode = state.get("in_intersection_mode", False)
        _last_zebra_deviation_time = state.get("last_zebra_deviation_time", 0.0)
        _last_align_reminder_time = state.get("last_align_reminder_time", 0.0)
        # ★ Feature 8: 红绿灯状态恢复
        _traffic_light_state = state.get("traffic_light_state", "unknown")
        _traffic_light_last_change_time = state.get("traffic_light_last_change_time", 0.0)
        _last_traffic_light_time = state.get("last_traffic_light_time", 0.0)
        IN_INTERSECTION_MODE = state.get("in_traffic_light_mode", False)
        _traffic_light_stable_frames = state.get("traffic_light_stable_frames", 0)
        _traffic_light_confirmed = state.get("traffic_light_confirmed", False)
        _traffic_light_no_signal_announced = state.get("traffic_light_no_signal_announced", False)
        # ★ 斑马线引导状态恢复
        _zebra_guide_phase = state.get("zebra_guide_phase", "")
        _zebra_guide_direction = state.get("zebra_guide_direction", "")
        _zebra_guide_last_announce = state.get("zebra_guide_last_announce", 0.0)
        _zebra_guide_phone_hint = state.get("zebra_guide_phone_hint", False)
        # ★ 斑马线偏移检测状态恢复
        _zebra_deviation_active = state.get("zebra_deviation_active", False)
        _zebra_last_seen_time = state.get("zebra_last_seen_time", 0.0)

        print(f"[{datetime.now()}] ✅ 状态已加载: {len(_event_history)} 帧历史, {len(_tracked_objects)} 追踪对象")
    except Exception as e:
        print(f"[{datetime.now()}] 状态加载失败: {e}")

def _try_periodic_save():
    """定时保存检查"""
    global _last_save_time
    now = time.time()
    if now - _last_save_time >= CONFIG["STATE_SAVE_INTERVAL"]:
        _save_state()
        _last_save_time = now


# ============ OCR 场景文字识别（懒加载 RapidOCR）============
_ocr_model = None
_last_scene_text_time = 0.0
_ocr_loading = False  # ★ 加载锁，防止重复加载

# ★ 找店模式状态
_find_mode_active = False       # 是否正在找店
_find_target = ""               # 要找的目标文字（如"肯德基"）— 用于播报
_find_keywords = []              # OCR 搜索关键词列表（如["肯德基","KFC"]）— 用于匹配
_find_start_time = 0.0          # 找店模式开始时间
_find_last_scan_time = 0.0      # 上次扫描时间
_find_max_duration = 120.0      # 找店模式最长持续时间（秒）
_find_scan_interval = 1.0       # 扫描间隔（秒）
_find_ws = None                 # 当前 WebSocket 连接（用于发送结果）
_find_assistant = None           # AssistantServer 实例（用于发送结果给小助手）
_find_last_guidance = ""         # 上次播报的方向指引（避免重复播报）
_find_last_found_time = 0.0     # 上次找到目标的时间
_find_lost_timeout = 15.0       # 目标消失多久后提示"已走过"（15秒，避免遮挡误判）
_find_close_ratio = 0.0         # 最近一次目标占画面宽度比（判断距离）
_find_arrived = False            # 是否已到达（文字曾占画面30%以上宽度）
_find_arrived_cooldown = 0.0    # 到达提示冷却（避免重复说"到了"）
_crossing_mode_gps_confirmed = False  # ★ 问题1修复：GPS是否确认进入了路口模式
_find_store_detect_start_time = 0.0  # ★ 问题10修复：连续检测到目标文字的开始时间
_find_store_continuous_frames = 0     # ★ 问题10修复：连续检测到目标的帧数


def _start_find_mode(target: str, websocket, assistant_server, keywords: str = ""):
    """启动找店模式：持续 OCR 扫描直到找到目标文字"""
    global _find_mode_active, _find_target, _find_start_time, _find_last_scan_time, _find_ws, _find_assistant, _find_keywords
    _find_mode_active = True
    _find_target = target.strip()
    # 解析关键词：支持逗号分隔的字符串
    if keywords:
        _find_keywords = [k.strip() for k in keywords.split(",") if k.strip()]
    else:
        _find_keywords = [_find_target]
    _find_start_time = time.time()
    _find_last_scan_time = 0.0
    _find_ws = websocket
    _find_assistant = assistant_server
    print(f"[{datetime.now()}] 🔍 找店模式启动: 目标=\"{_find_target}\", 搜索词={_find_keywords}")


def _stop_find_mode():
    """停止找店模式"""
    global _find_mode_active, _find_target, _find_ws, _find_last_guidance
    global _find_last_found_time, _find_close_ratio, _find_arrived, _find_arrived_cooldown, _find_keywords
    _find_mode_active = False
    _find_target = ""
    _find_keywords = []
    _find_ws = None
    _find_last_guidance = ""
    _find_last_found_time = 0.0
    _find_close_ratio = 0.0
    _find_arrived = False
    _find_arrived_cooldown = 0.0
    print(f"[{datetime.now()}] 🔍 找店模式已停止")


def _check_find_mode(frame):
    """找店模式：每2秒OCR扫描一次，找到目标后发送方向指引"""
    global _find_last_scan_time

    if not _find_mode_active:
        return

    now = time.time()

    # 超时自动停止
    if (now - _find_start_time) > _find_max_duration:
        print(f"[{datetime.now()}] 🔍 找店模式超时({_find_max_duration}秒)，未找到\"{_find_target}\"")
        _send_find_result(_find_assistant, False, f"未找到{_find_target}，请尝试调整方向或位置")
        _stop_find_mode()
        return

    # 控制扫描频率
    if (now - _find_last_scan_time) < _find_scan_interval:
        return
    _find_last_scan_time = now

    # ★ OCR 在后台线程运行，不阻塞主循环（避免预览窗口卡死）
    import threading
    frame_copy = frame.copy()
    threading.Thread(target=_do_find_ocr, args=(frame_copy,), daemon=True).start()


def _do_find_ocr(frame):
    """后台线程执行 OCR 扫描"""
    global _find_store_continuous_frames, _find_store_detect_start_time  # ★ 问题10修复
    ocr = _get_ocr_model()
    if ocr is None:
        return

    try:
        result, _ = ocr(frame)

        # ★ RapidOCR 返回 [[box, text_str, conf_str], ...]
        if not result:
            _find_store_continuous_frames = 0  # ★ 问题10修复：重置连续检测计数
            return

        frame_width = frame.shape[1] if frame is not None else 640
        all_texts = []

        for line in result:
            text = ""
            conf = 0.0
            box = None

            try:
                if isinstance(line, (list, tuple)) and len(line) >= 3:
                    box = line[0]
                    # ★ RapidOCR 格式: [box, text_str, conf_str]
                    text = str(line[1]).strip()
                    conf = float(line[2])
                elif isinstance(line, (list, tuple)) and len(line) >= 2:
                    box = line[0]
                    info = line[1]
                    if isinstance(info, (list, tuple)) and len(info) >= 2:
                        text = str(info[0]).strip()
                        conf = float(info[1])
                    elif isinstance(info, str):
                        text = info.strip()
                        conf = 1.0
                    elif isinstance(info, dict):
                        text = str(info.get("text", "")).strip()
                        conf = float(info.get("score", 0))
                else:
                    continue
            except (IndexError, TypeError, ValueError):
                continue

            if not text or conf < CONFIG["SCENE_TEXT_MIN_CONF"]:
                continue

            all_texts.append(text)

            # ★ 用搜索关键词匹配（肯德基→匹配"肯德基"或"KFC"）
            # 规则：去掉所有空格后比较（"luckincoffee" 匹配 "luckin coffee"）
            matched_keyword = ""
            text_upper = text.upper().replace(" ", "")  # 去掉空格
            for kw in _find_keywords:
                kw_upper = kw.upper().replace(" ", "")  # 去掉空格
                # 方向1：关键词是识别文字的子串（如"肯德基" in "肯德基店"）
                if kw_upper in text_upper:
                    # ★ 防止短词子串误匹配：关键词长度至少3，或识别文字长度不超过关键词2倍
                    if len(kw) >= 3 or len(text) <= len(kw) * 2:
                        matched_keyword = kw
                        break
                # 方向2：识别文字是关键词的子串（如"KFC" in "KFC"）
                elif text_upper in kw_upper:
                    # ★ 防止短识别文字子串误匹配（如"of" in "luckin coffee"）
                    if len(text) >= 3 or len(text) >= len(kw) * 0.5:
                        matched_keyword = kw
                        break

            if matched_keyword:
                # ★ 误匹配过滤
                # 1. 黑名单前缀：新闻标题、广告文案等
                noise_prefixes = ["当", "如果", "为什么", "怎么", "如何", "关于", "据报道",
                                   "据悉", "近日", "最新", "重磅", "突发", "官宣",
                                   "总", "搜索", "下载", "点击", "查看", "打开"]
                for prefix in noise_prefixes:
                    if text.startswith(prefix):
                        matched_keyword = ""
                        break

                if matched_keyword:
                    # 2. 黑名单后缀：关键词后面跟了不相关的词
                    noise_suffixes = ["门轴承", "门牌", "门配件", "公仔", "玩具", "周边", "联名", "同款",
                                      "门", "轴承", "配件", "装饰", "装修"]
                    kw_pos = text_upper.find(matched_keyword.upper())
                    after_kw = text[kw_pos + len(matched_keyword):].strip()
                    for suffix in noise_suffixes:
                        if after_kw.startswith(suffix):
                            matched_keyword = ""  # 取消匹配
                            break

                # 2. 长文字过滤：关键词不在开头附近（前30%位置内）
                if matched_keyword and len(text) > len(matched_keyword) * 3:
                    if kw_pos > len(text) * 0.3:
                        prefix = text[:kw_pos].strip()
                        if prefix and prefix not in ("·", "-", "—", " ", "KFC"):
                            matched_keyword = ""  # 取消匹配

            if matched_keyword:
                # 计算方向和距离
                if box is not None:
                    try:
                        if hasattr(box[0], '__getitem__'):
                            xs = [p[0] for p in box]
                            ys = [p[1] for p in box]
                        else:
                            xs = [box[0], box[2]]
                            ys = [box[1], box[3]]
                    except Exception:
                        xs = [frame_width / 2]
                        ys = [0, 100]
                else:
                    xs = [frame_width / 2]
                    ys = [0, 100]

                center_x = (min(xs) + max(xs)) / 2
                ratio = center_x / frame_width

                # ★ 计算目标占画面的宽度比（判断距离）
                text_width = max(xs) - min(xs)
                width_ratio = text_width / frame_width  # 文字宽度占画面比例
                text_height = max(ys) - min(ys)
                height_ratio = text_height / (frame.shape[0] if frame is not None else 480)

                global _find_last_guidance, _find_last_found_time, _find_close_ratio
                global _find_arrived, _find_arrived_cooldown
                _find_last_found_time = time.time()
                _find_close_ratio = max(_find_close_ratio, width_ratio)

                # ★ 判断是否到达（文字占画面宽度30%以上 或 高度40%以上 = 很近了）
                is_close = width_ratio > 0.30 or height_ratio > 0.40

                # ★ 问题10修复：时间兜底检测
                # 连续检测到目标文字超过8秒（约80帧@10fps），即使面积不够也提示到达
                _find_store_continuous_frames += 1
                if _find_store_continuous_frames == 1:
                    _find_store_detect_start_time = time.time()
                time_detected = time.time() - _find_store_detect_start_time
                time_threshold_arrived = (time_detected > 8.0) or (_find_store_continuous_frames > 80)
                # 面积曾达到20%以上且持续超过3秒也触发
                area_soft_arrived = (_find_close_ratio > 0.20 and time_detected > 3.0)
                if time_threshold_arrived or area_soft_arrived:
                    is_close = True  # 兜底触发到达
                    print(f"[{datetime.now()}] ★ 找店时间兜底到达: 持续{time_detected:.1f}秒, 最大面积{_find_close_ratio:.0%}")
                if is_close:
                    _find_arrived = True

                # ★ 到达提示（首次到达时播报，然后5秒后自动停止找店模式）
                now = time.time()
                if is_close and _find_arrived_cooldown == 0:
                    _find_arrived_cooldown = now
                    _find_last_guidance = "arrived"  # 标记为到达状态
                    print(f"[{datetime.now()}] 🔍 已到达! \"{text}\" 占画面{width_ratio:.0%}宽")
                    _send_find_result(_find_assistant, True,
                        f"已经到{_find_target}门口了，请注意安全")
                    # ★ 5秒后自动停止找店模式
                    import threading
                    def _auto_stop():
                        import time as t
                        t.sleep(5)
                        if _find_mode_active and _find_arrived:
                            _stop_find_mode()
                            _send_find_result(_find_assistant, True,
                                f"{_find_target}找店导航结束")
                    threading.Thread(target=_auto_stop, daemon=True).start()
                    return

                # ★ 已到达后不再播报方向指引（避免"已到门口"后又说"前方发现"）
                if _find_arrived:
                    return

                # ★ 方向指引（未到达时，加死区防抖动）
                # 死区：ratio 在 0.30-0.40 之间视为"前方"，避免来回跳
                if ratio < 0.30:
                    direction = "左侧"
                    guidance = f"左侧发现{_find_target}，请向左前方走"
                elif ratio > 0.70:
                    direction = "右侧"
                    guidance = f"右侧发现{_find_target}，请向右前方走"
                else:
                    direction = "前方"
                    guidance = f"前方发现{_find_target}，请直行"

                # ★ 方向变化时才重新播报（加2秒冷却，避免来回跳）
                now = time.time()
                if guidance != _find_last_guidance:
                    # ★ 冷却检查：如果上次播报不到2秒，不播报新方向
                    # 但"左/右→前方"是重要变化，立即播报（用户快到了）
                    last_guidance_time = getattr(_do_find_ocr, '_last_broadcast_time', 0)
                    is_important_change = (direction == "前方" and _find_last_guidance != "前方")
                    if (now - last_guidance_time) > 2 or is_important_change:
                        _find_last_guidance = guidance
                        _do_find_ocr._last_broadcast_time = now
                        print(f"[{datetime.now()}] 🔍 找到了! \"{text}\" 在{direction} (ratio={ratio:.2f}, 宽度占比={width_ratio:.0%})")
                        _send_find_result(_find_assistant, True, guidance)
                return  # 找到了，不需要继续检查其他文字

        # ★ 检查目标是否消失（已走过）
        if _find_last_found_time > 0:
            lost_duration = time.time() - _find_last_found_time
            if lost_duration > _find_lost_timeout:
                _find_last_found_time = 0
                _find_last_guidance = ""
                # ★ 如果之前已经到达过，不播报"已走过"
                if _find_arrived:
                    print(f"[{datetime.now()}] 🔍 已到达过{_find_target}，目标消失不播报")
                    _find_arrived = False
                else:
                    print(f"[{datetime.now()}] 🔍 目标消失超过{_find_lost_timeout}秒，可能已走过")
                    _send_find_result(_find_assistant, True,
                        f"{_find_target}已不在视野中，可能已经走过了，请回头看看")

        # ★ 打印识别到的所有文字，方便调试
        print(f"[{datetime.now()}] 🔍 扫描中... 未找到\"{_find_target}\" (识别到: {', '.join(all_texts[:10])})")
        _find_store_continuous_frames = 0  # ★ 问题10修复：重置连续检测计数

    except Exception as e:
        print(f"[{datetime.now()}] 🔍 找店OCR扫描出错: {e}")


def _send_find_result(assistant_server, success: bool, message: str):
    """发送找店结果给小助手（通过 AssistantServer，支持从后台线程调用）"""
    if assistant_server is None or not assistant_server.connected:
        print(f"[{datetime.now()}] 🔍 找店结果发送失败: 小助手未连接")
        return
    try:
        import asyncio
        # ★ 从后台线程安全地调用异步函数
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(
                lambda: asyncio.ensure_future(
                    _async_send_find_result(assistant_server, success, message)
                )
            )
        else:
            loop.run_until_complete(_async_send_find_result(assistant_server, success, message))
    except RuntimeError:
        # 没有事件循环时，尝试创建新的
        try:
            asyncio.run(_async_send_find_result(assistant_server, success, message))
        except Exception as e2:
            print(f"[{datetime.now()}] 🔍 找店结果发送失败: {e2}")
    except Exception as e:
        print(f"[{datetime.now()}] 🔍 找店结果发送失败: {e}")


async def _async_send_find_result(assistant_server, success: bool, message: str):
    """异步发送找店结果"""
    try:
        msg = json.dumps({
            "type": "find_result",
            "success": success,
            "text": message.strip(),
            "target": _find_target,
        }, ensure_ascii=False)
        await assistant_server.ws.send(msg)
        print(f"[{datetime.now()}] 🔍 找店结果已发送给小助手: {message}")
    except Exception as e:
        print(f"[{datetime.now()}] 🔍 找店结果发送失败: {e}")

def _get_ocr_model():
    """懒加载 RapidOCR 模型（轻量级，比 PaddleOCR 快10倍）"""
    global _ocr_model, _ocr_loading
    if _ocr_model is not None:
        return _ocr_model
    if _ocr_loading:
        return None  # 正在加载中，不要重复加载
    _ocr_loading = True
    try:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_model = RapidOCR(lang="ch")
        print(f"[{datetime.now()}] ✅ RapidOCR 模型加载成功")
    except ImportError:
        print(f"[{datetime.now()}] ❌ RapidOCR 未安装，正在安装...")
        import subprocess
        subprocess.check_call(["pip", "install", "rapidocr_onnxruntime", "--break-system-packages"])
        try:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_model = RapidOCR(lang="ch")
            print(f"[{datetime.now()}] ✅ RapidOCR 安装并加载成功")
        except Exception as e2:
            print(f"[{datetime.now()}] ❌ RapidOCR 安装失败: {e2}")
            return None
    except Exception as e:
        print(f"[{datetime.now()}] ❌ OCR 初始化失败: {e}")
        return None
    finally:
        _ocr_loading = False
    return _ocr_model

def _run_scene_text_ocr(frame):
    """对当前帧运行 OCR，按文字框面积从大到小排序"""
    global _last_scene_text_time

    now = time.time()
    if now - _last_scene_text_time < CONFIG["SCENE_TEXT_COOLDOWN"]:
        return []
    _last_scene_text_time = now

    ocr = _get_ocr_model()
    if ocr is None:
        return []

    try:
        result, _ = ocr(frame)
        texts = []
        if result:
            for line in result:
                box = line[0]
                text = ""
                conf = 0.0
                try:
                    if isinstance(line, (list, tuple)) and len(line) >= 3:
                        text = str(line[1]).strip()
                        conf = float(line[2])
                    elif isinstance(line, (list, tuple)) and len(line) >= 2:
                        info = line[1]
                        if isinstance(info, (list, tuple)) and len(info) >= 2:
                            text = str(info[0]).strip()
                            conf = float(info[1])
                        elif isinstance(info, str):
                            text = info.strip()
                            conf = 1.0
                        elif isinstance(info, dict):
                            text = str(info.get("text", "")).strip()
                            conf = float(info.get("score", 0))
                        else:
                            continue
                    else:
                        continue
                except (IndexError, TypeError, ValueError):
                    continue
                if not text or conf < CONFIG["SCENE_TEXT_MIN_CONF"]:
                    continue
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                area = (max(xs) - min(xs)) * (max(ys) - min(ys))
                texts.append({
                    "text": text,
                    "bbox": [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))],
                    "area": float(area),
                    "confidence": float(conf)
                })
        texts.sort(key=lambda x: x["area"], reverse=True)
        print(f"[{datetime.now()}] OCR 识别到 {len(texts)} 个文字区域")
        return texts
    except Exception as e:
        print(f"[{datetime.now()}] OCR 识别出错: {e}")
        return []
print(f"[{datetime.now()}] 正在加载自定义模型: {MODEL_CUSTOM_PATH}")
model_custom = YOLO(MODEL_CUSTOM_PATH)
custom_type = model_custom.task if hasattr(model_custom, 'task') else 'detect'
if custom_type == 'segment':
    print(f"[{datetime.now()}] 自定义模型类型: 分割模型 (segment)")
elif custom_type == 'detect':
    print(f"[{datetime.now()}] 自定义模型类型: 检测模型 (detect)")
else:
    print(f"[{datetime.now()}] 自定义模型类型: {custom_type}")
print(f"[{datetime.now()}] 自定义模型类别: {list(model_custom.names.values())}")
print(f"[{datetime.now()}] 自定义模型加载完成！")

print(f"[{datetime.now()}] " + "-"*50)
print(f"[{datetime.now()}] 正在加载官方模型: {OFFICIAL_MODEL_PATH}")
model_official = YOLO(OFFICIAL_MODEL_PATH)
official_type = model_official.task if hasattr(model_official, 'task') else 'detect'
print(f"[{datetime.now()}] 官方模型类型: 检测模型 (detect)")
print(f"[{datetime.now()}] 官方模型白名单类别: {[OFFICIAL_CLASS_NAMES_CN.get(i, model_official.names.get(i, f'class_{i}')) for i in sorted(ALLOWED_CLASSES)]}")
print(f"[{datetime.now()}] 官方模型加载完成！")
print(f"[{datetime.now()}] " + "="*50)

# ============ 加载红绿灯专用模型（第三模型 - Feature 8）============
print(f"[{datetime.now()}] " + "-"*50)
print(f"[{datetime.now()}] 正在加载红绿灯专用模型: {TRAFFIC_LIGHT_MODEL_PATH}")
try:
    model_traffic_light = YOLO(TRAFFIC_LIGHT_MODEL_PATH)
    print(f"[{datetime.now()}] 红绿灯模型类型: 检测模型 (detect)")
    # ★ 自动从模型读取类别名称，不再硬编码
    # 适配新旧模型：新模型只有红灯/绿灯（ID=0,1），旧模型有6个类别（ID=0-5）
    if hasattr(model_traffic_light, 'names') and model_traffic_light.names:
        TRAFFIC_LIGHT_CLASSES_CN = {int(k): v for k, v in model_traffic_light.names.items()}
        # 自动识别包含"红"或"绿"的类别作为颜色类别
        TRAFFIC_LIGHT_COLOR_CLASSES = {
            cid for cid, cname in TRAFFIC_LIGHT_CLASSES_CN.items()
            if "红" in cname or "绿" in cname
        }
    else:
        # 回退：如果模型没有names属性，使用默认值
        TRAFFIC_LIGHT_CLASSES_CN = {0: "红灯", 1: "绿灯"}
        TRAFFIC_LIGHT_COLOR_CLASSES = {0, 1}
    print(f"[{datetime.now()}] 红绿灯模型类别: {TRAFFIC_LIGHT_CLASSES_CN}")
    print(f"[{datetime.now()}] 红绿灯颜色类别ID: {TRAFFIC_LIGHT_COLOR_CLASSES}")
    if not TRAFFIC_LIGHT_COLOR_CLASSES:
        print(f"[{datetime.now()}] ⚠️ 警告：未找到红灯/绿灯类别，红绿灯检测将不可用！")
    print(f"[{datetime.now()}] 红绿灯模型加载完成！")
except Exception as e:
    print(f"[{datetime.now()}] ❌ 红绿灯模型加载失败: {e}")
    print(f"[{datetime.now()}] 红绿灯功能将不可用，其他功能正常")
    model_traffic_light = None
print(f"[{datetime.now()}] " + "="*50)

# ============ 预加载 RapidOCR 模型（避免首次找店时卡死 WebSocket）============
print(f"[{datetime.now()}] 正在预加载 RapidOCR 模型...")
_get_ocr_model()
if _ocr_model is not None:
    print(f"[{datetime.now()}] ✅ RapidOCR 预加载完成")
else:
    print(f"[{datetime.now()}] ⚠️ RapidOCR 预加载失败，首次找店时会重试")


# ============ 偏移检测冷却计时 ============
_last_deviation_time = 0.0          # 上次播报偏移的时间戳
DEVIATION_COOLDOWN = 5.0            # 偏移提醒冷却时间（秒）
DEVIATION_THRESHOLD_RATIO = 0.20    # 偏移阈值（画面宽度的比例），超过此值才提醒

# ============ 全局状态（跨帧追踪）============
_tracked_objects = {}              # {track_key: {"bbox": [...], "last_seen": float, "prev_bbox": [...], "velocity": float}}
_TRACK_COOLDOWN = 2.0              # 追踪对象多久没出现就删除（秒）
_MOVING_THRESHOLD = 15.0           # 像素/秒，判断为"移动中"的阈值

# ============ Feature 1: 连续帧稳定性（防抖）============
# 确认事件历史：每个元素是本帧已确认的障碍物事件集合
_event_history = []                # [ {(name, direction, distance, is_on_curb), ...}, ... ]
_CONFIRM_FRAMES = 3               # 同一状态连续出现 3 帧才确认（防抖）
_EVENT_HISTORY_LEN = 5            # 滑动窗口长度（帧）

# ============ Feature 2: 盲道追踪（丢失上下文判断）============
# 盲道历史：每帧记录当时的直行盲道状态
_curb_history = []                 # [ {"has_curb": bool, "count": int, "time": float}, ... ]
_CURB_HISTORY_LEN = 5             # 盲道滑动窗口长度
_CURB_MISS_CONFIRM = 3            # 盲道连续丢失 3 帧才确认真正消失（防误判）

# ============ Feature 6: 盲道断裂填充（跨帧连接）============
# 记录上一帧的盲道边界，用于跨帧断裂修复
_last_frame_curbs = []            # 上一帧的盲道 bbox 列表 [{bbox, conf}, ...]

# ============ 距离估算阈值（相对于画面面积）============
# 框越大 → 越近
FAR_RATIO_MAX = 0.015    # 面积 < 1.5% → 远
NEAR_RATIO_MIN = 0.060    # 面积 > 6% → 近
# 1.5% ~ 6% → 中

def _is_frame_too_dark(frame: np.ndarray) -> bool:
    """
    检测画面是否太暗（纯黑/镜头盖住等情况）。
    太暗的帧容易产生模型幻觉，应跳过播报。
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    mean_brightness = float(np.mean(gray))
    return mean_brightness < FRAME_BRIGHTNESS_MIN


def _estimate_distance(bbox: list, frame_area: float) -> str:
    """
    根据检测框面积估算距离远近。
    bbox: [x1, y1, x2, y2]
    返回: "远" / "中" / "近"
    """
    x1, y1, x2, y2 = bbox
    box_area = (x2 - x1) * (y2 - y1)
    ratio = box_area / frame_area if frame_area > 0 else 0

    if ratio < FAR_RATIO_MAX:
        return "远"
    elif ratio > NEAR_RATIO_MIN:
        return "近"
    else:
        return "中"

# ============ 方向感知（左/中/右）============
# 方向阈值：左右各占 35%，中间占 30%
DIR_LEFT_RATIO_MAX = 0.35
DIR_RIGHT_RATIO_MIN = 0.65

def _get_direction(bbox: list, frame_width: int) -> str:
    """
    根据检测框中心 X 坐标判断方向（左/中/右）。
    返回: "左侧" / "前方" / "右侧"
    """
    if frame_width <= 0:
        return "前方"
    x1, _, x2, _ = bbox
    cx = (x1 + x2) / 2.0
    ratio = cx / frame_width

    if ratio < DIR_LEFT_RATIO_MAX:
        return "左侧"
    elif ratio > DIR_RIGHT_RATIO_MIN:
        return "右侧"
    else:
        return "前方"

# ============ 路口 / 转角检测 ============
_last_intersection_warning_time = 0.0
INTERSECTION_COOLDOWN = 8.0          # 路口提醒冷却时间
_in_intersection_mode = False        # Feature 3: 是否处于路口提醒状态（播过但盲道可能还在消失中）

def _detect_intersection(detections: list) -> str:
    """
    检测是否处于路口场景。
    判断条件：
      - 盲道消失（没有直行盲道）+ 斑马线出现 → 路口
      - 直行盲道突然中断（面积突然变小）→ 转角
    返回：路口提醒文本（空字符串表示不在路口）
    """
    global _last_intersection_warning_time, _in_intersection_mode

    has_straight_curb = False
    has_zebra = False
    has_stop_curb = False
    curb_count = 0

    for d in detections:
        if d.get("source") != "custom":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        if name_cn == "直行盲道":
            has_straight_curb = True
            curb_count += 1
        elif name_cn == "斑马线":
            has_zebra = True
        elif name_cn == "停止盲道":
            has_stop_curb = True

    # 路口场景1：没有直行盲道但有斑马线
    if not has_straight_curb and has_zebra:
        now = time.time()
        # 路口提醒冷却较长，用固定值避免频繁播报
        if now - _last_intersection_warning_time < INTERSECTION_COOLDOWN:
            return ""
        _last_intersection_warning_time = now
        _in_intersection_mode = True   # Feature 3: 进入路口提醒状态
        return "前方路口，请注意方向"

    # 路口场景2：盲道数量突然变少（从多条变1条或0条）→ 提示转角
    if has_straight_curb and curb_count == 1 and not has_zebra:
        # 只在特别少见的单盲道+无斑马线情况下提示
        pass  # 暂时保守，盲道减少不单独提示，避免误报

    return ""


# ============ 斑马线偏移检测 ============
def _detect_zebra_deviation(detections: list, frame_width: int) -> str:
    """
    检测用户是否偏离斑马线。

    ★ 核心思路：斑马线是梯形（下宽上窄），用户偏离时，
    斑马线的某一边会离画面边缘更远（空白更多）。

    算法：
    1. 取所有斑马线检测框的最左x1和最右x2
    2. left_gap = 最左x1（斑马线左边离画面左边缘的距离）
    3. right_gap = frame_width - 最右x2（斑马线右边离画面右边缘的距离）
    4. gap_diff = left_gap - right_gap
       - gap_diff < 0 → left_gap小 → 斑马线贴左 → 用户偏右 → 应该向左走
       - gap_diff > 0 → right_gap小 → 斑马线贴右 → 用户偏左 → 应该向右走
    5. gap_ratio = abs(gap_diff) / frame_width > 阈值才触发
    """
    if frame_width <= 0:
        return ""

    # 只检测斑马线
    zebras = []
    for d in detections:
        if d.get("source") != "custom":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        if name_cn not in {"斑马线", "zebra_crossing"}:
            continue
        bbox = d.get("bbox", [])
        if len(bbox) == 4:
            zebras.append(bbox)

    if not zebras:
        return ""   # 看不到斑马线，不判断偏移

    # ★ 取所有斑马线检测框的最左x1和最右x2
    min_x1 = min(bbox[0] for bbox in zebras)
    max_x2 = max(bbox[2] for bbox in zebras)

    # ★ 计算斑马线左右两侧离画面边缘的空白
    left_gap = min_x1                                    # 斑马线左边离画面左边缘
    right_gap = frame_width - max_x2                     # 斑马线右边离画面右边缘

    # ★ gap_diff > 0 → 左边空白多 → 斑马线偏右 → 用户偏左 → 向右走
    gap_diff = left_gap - right_gap
    gap_ratio = abs(gap_diff) / frame_width

    # ★ 调试日志：只在有明显偏移时打印（ratio > 1%），减少日志噪音
    if gap_ratio > 0.01:
        print(f"[斑马线偏移检测] left_gap={left_gap:.0f} right_gap={right_gap:.0f} gap_diff={gap_diff:.0f} ratio={gap_ratio:.3f} 阈值={CONFIG['ZEBRA_DEVIATION_THRESHOLD_RATIO']}")

    if gap_ratio < CONFIG["ZEBRA_DEVIATION_THRESHOLD_RATIO"]:
        return ""   # 偏移在容忍范围内

    # ★ 冷却控制由调用方 build_speak_text() 和 send_obstacle_warning() 负责，
    #    此函数只负责计算偏移方向，不做冷却判断

    # ★ 方向判断（修复：原来逻辑完全反了）
    #   left_gap 小 → 斑马线贴左边缘 → 用户偏右 → 应该向左走（让用户往左回到中间）
    #   right_gap 小 → 斑马线贴右边缘 → 用户偏左 → 应该向右走（让用户往右回到中间）
    #   gap_diff = left_gap - right_gap
    #   gap_diff < 0 → left_gap < right_gap → 斑马线贴左 → 用户偏右 → 向左走
    #   gap_diff > 0 → left_gap > right_gap → 斑马线贴右 → 用户偏左 → 向右走
    if gap_diff < 0:
        return "您已偏离斑马线，向左前方走"
    else:
        return "您已偏离斑马线，向右前方走"


# ============ 运动状态检测（跨帧追踪）============
_OBJECT_MOVE_PX = 12.0  # 连续两帧间位移超过此像素认为在移动

def _track_and_detect_motion(detections: list, frame_width: int, frame_height: int, current_time: float) -> dict:
    """
    跨帧追踪障碍物，计算运动速度。
    返回: {name_cn: "移动中"/"静止"/"未知"}
    """
    now = current_time

    # 清理过期追踪对象
    expired_keys = [k for k, v in _tracked_objects.items() if (now - v["last_seen"]) > _TRACK_COOLDOWN]
    for k in expired_keys:
        del _tracked_objects[k]

    motion_state = {}   # {name_cn: "移动中"/"静止"}

    for d in detections:
        if d.get("source") != "official":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        if name_cn not in {"人", "汽车", "公交车", "卡车", "摩托车", "自行车"}:
            continue

        bbox = d.get("bbox", [])
        if len(bbox) != 4:
            continue

        # 用类别+大致位置生成track_key（以画面宽度归一化做桶）
        x1, y1, x2, y2 = bbox
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        bucket_x = int(cx / (frame_width / 10))
        bucket_y = int(cy / (frame_height / 10))
        track_key = f"{name_cn}_{bucket_x}_{bucket_y}"

        prev = _tracked_objects.get(track_key)
        if prev:
            prev_bbox = prev["bbox"]
            prev_cx = (prev_bbox[0] + prev_bbox[2]) / 2.0
            prev_cy = (prev_bbox[1] + prev_bbox[3]) / 2.0
            pixel_dist = ((cx - prev_cx) ** 2 + (cy - prev_cy) ** 2) ** 0.5
            time_delta = now - prev["last_seen"]
            if time_delta > 0:
                velocity = pixel_dist / time_delta
            else:
                velocity = 0.0
            _tracked_objects[track_key] = {
                "bbox": bbox,
                "prev_bbox": prev_bbox,
                "last_seen": now,
                "velocity": velocity
            }
            motion_state[name_cn] = "移动中" if velocity > _MOVING_THRESHOLD else "静止"
        else:
            # 新对象，建立追踪
            _tracked_objects[track_key] = {
                "bbox": bbox,
                "prev_bbox": bbox,
                "last_seen": now,
                "velocity": 0.0
            }
            motion_state[name_cn] = "未知"   # 第一帧无法判断运动状态

    return motion_state

# ============ 帧率自适应冷却 ============
_last_fps_check_time = 0.0
_recent_frame_times = []             # 最近若干帧的间隔时间（秒）
_RECENT_FRAME_COUNT = 10             # 滑动窗口大小
_measured_fps = 15.0                 # 默认假设 15 FPS

def _update_adaptive_fps(frame_interval: float):
    """
    根据本帧处理间隔更新 FPS 估算。
    frame_interval: 本帧距离上一帧的时间（秒）
    """
    global _last_fps_check_time, _measured_fps

    now = time.time()
    _recent_frame_times.append(frame_interval)
    if len(_recent_frame_times) > _RECENT_FRAME_COUNT:
        _recent_frame_times.pop(0)

    if _recent_frame_times:
        avg_interval = sum(_recent_frame_times) / len(_recent_frame_times)
        if avg_interval > 0:
            _measured_fps = 1.0 / avg_interval

    _last_fps_check_time = now

def _adaptive_cooldown(base_cooldown: float, frame_width: int) -> float:
    """
    根据实际帧率动态调整冷却时间。
    FPS 高 → 冷却短（高频检测需要更快的响应）
    FPS 低 → 冷却长（避免同一障碍物播报多次）
    """
    target_frames = 8   # 希望同一事件至少隔 8 帧才重复
    return max(base_cooldown, target_frames / max(_measured_fps, 1.0))


# ============ Feature 1: 障碍物事件防抖确认 ============
def _confirm_obstacle_events(detections: list, frame_width: int, frame_area: float,
                              curbs: list, current_time: float) -> set:
    """
    连续帧稳定性：判断哪些障碍物事件已被"确认"（连续出现 N 帧）。

    返回：确认事件的集合，元素格式为
          ("obstacle_on_curb", name, direction, distance)
          ("obstacle_free",    name, direction, distance)

    算法：
      1. 从本帧 detections 生成候选事件集合
      2. 与 _event_history 中的历史对比
      3. 在滑动窗口中累计出现次数，达到阈值则加入 confirmed_set
      4. 更新 _event_history（append本帧，pop最旧帧）
    """
    global _event_history

    # ---- 1. 生成候选事件 ----
    candidate_events = set()
    danger_names = {"人", "汽车", "公交车", "卡车", "摩托车", "自行车"}

    for d in detections:
        if d.get("source") != "official":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        if name_cn not in danger_names:
            continue
        bbox = d.get("bbox", [])
        if len(bbox) != 4:
            continue

        direction = _get_direction(bbox, frame_width)
        distance = _estimate_distance(bbox, frame_area)

        # 判断是否在盲道上
        x1, y1, x2, y2 = bbox
        is_on_curb = False
        for curb_bbox_dict in curbs:
            c_x1, c_y1, c_x2, c_y2 = curb_bbox_dict["bbox"]
            overlap_w = min(x2, c_x2) - max(x1, c_x1)
            if overlap_w <= 0:
                continue
            y_overlap = min(y2, c_y2) - max(y1, c_y1)
            if y_overlap > 0:
                is_on_curb = True
                break

        if is_on_curb:
            event_key = ("obstacle_on_curb", name_cn, direction, distance)
        else:
            event_key = ("obstacle_free", name_cn, direction, distance)

        candidate_events.add(event_key)

    # ---- 2. 统计滑动窗口内每个事件的累计出现次数 ----
    event_counts = {}   # {event_key: count}
    for frame_events in _event_history:
        for ev in frame_events:
            event_counts[ev] = event_counts.get(ev, 0) + 1

    # ---- 3. 确认标准：在历史窗口 + 本帧中共出现 >= _CONFIRM_FRAMES 次 ----
    confirmed = set()
    for ev in candidate_events:
        count = event_counts.get(ev, 0)
        if count + 1 >= _CONFIRM_FRAMES:
            confirmed.add(ev)
        # 事件存在但未达阈值 → 不确认（仍在积累中）

    # ---- 4. 更新历史滑动窗口 ----
    _event_history.append(candidate_events)
    if len(_event_history) > _EVENT_HISTORY_LEN:
        _event_history.pop(0)

    return confirmed


# ============ Feature 2: 盲道追踪 + Feature 6: 断裂填充 ============
def _collect_curbs_with_gapfill(detections: list, frame_width: int) -> list:
    """
    收集盲道检测框，并做断裂填充（跨帧连接）。

    逻辑：
      1. 收集当前帧的直行/停止盲道
      2. 如果当前帧盲道很少，检查上一帧盲道是否存在"断裂"（被遮挡导致的漏检）
         → 若上一帧某盲道框在画面中消失，但在其消失方向（通常是纵向）附近
           存在其他小盲道块，则认为是同一盲道被遮住，保留原位置做填充
      3. 更新 _last_frame_curbs

    返回：[{"bbox": [x1,y1,x2,y2], "conf": float}, ...]
    """
    global _last_frame_curbs, _curb_history

    # ---- 收集当前帧盲道 ----
    current_curbs = []
    has_straight_curb = False
    has_zebra = False
    curb_count = 0

    for d in detections:
        if d.get("source") != "custom":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        bbox = d.get("bbox", [])
        if len(bbox) != 4:
            continue

        if name_cn in {"直行盲道", "停止盲道"}:
            current_curbs.append({
                "bbox": bbox,
                "conf": d.get("confidence", 0.0),
                "name": name_cn
            })
            if name_cn == "直行盲道":
                has_straight_curb = True
                curb_count += 1
        elif name_cn == "斑马线":
            has_zebra = True

    # ---- 记录盲道历史（Feature 2）----
    _curb_history.append({
        "has_curb": has_straight_curb,
        "count": curb_count,
        "time": time.time()
    })
    if len(_curb_history) > _CURB_HISTORY_LEN:
        _curb_history.pop(0)

    # ---- 断裂填充（Feature 6）----
    # 如果当前帧盲道 ≤ 1，但上一帧有多条盲道 → 可能是遮挡导致的断裂
    if len(current_curbs) <= 1 and len(_last_frame_curbs) >= 2:
        for last_curb in _last_frame_curbs:
            lx1, ly1, lx2, ly2 = last_curb["bbox"]
            lcx = (lx1 + lx2) / 2.0

            # 上一帧盲道在画面中的 X 位置
            # 如果该 X 位置附近（左右各 15% 画面宽度）没有当前帧盲道 → 疑似被遮挡
            found_near = False
            for cur in current_curbs:
                cx1, cy1, cx2, cy2 = cur["bbox"]
                ccx = (cx1 + cx2) / 2.0
                if abs(ccx - lcx) < frame_width * 0.15:
                    found_near = True
                    break

            if not found_near:
                # 用上一帧位置做"虚拟盲道"（置信度降低），保留用于空间关联
                current_curbs.append({
                    "bbox": last_curb["bbox"],
                    "conf": last_curb["conf"] * 0.5,   # 降低置信度，避免误用
                    "name": last_curb.get("name", "直行盲道"),
                    "_virtual": True                    # 标记为虚拟（跨帧填充）
                })

    # ---- 更新上一帧记录 ----
    _last_frame_curbs = current_curbs

    return current_curbs


def _is_curb_truly_gone() -> bool:
    """
    Feature 2: 判断盲道是否真正消失（连续丢失 N 帧）。
    用于路口检测：只有盲道真正消失才报路口。
    """
    if len(_curb_history) < _CURB_MISS_CONFIRM:
        return False   # 历史不足，不判断

    # 连续最近 N 帧都没有直行盲道
    recent = _curb_history[-_CURB_MISS_CONFIRM:]
    all_missing = all(not entry["has_curb"] for entry in recent)
    return all_missing


def _was_curb_recently_seen() -> bool:
    """
    Feature 2: 判断盲道是否在最近出现过（用于路口取消判断）。
    如果路口判断后盲道重新出现，应立即取消/更新路口提醒。
    """
    for entry in reversed(_curb_history):
        if entry["has_curb"]:
            return True
    return False


def _detect_curb_deviation(detections: list, frame_width: int) -> str:
    """
    检测用户是否偏移盲道。
    返回：偏移提醒文本（空字符串表示走在盲道上）

    算法：
      - 取所有直行盲道检测框，计算加权中心 X（权重=置信度）
      - 与画面中心比较偏移比例
      - 偏移超过阈值才提醒，且有冷却时间
    """
    global _last_deviation_time

    if frame_width <= 0:
        return ""

    # 只用直行盲道（停止盲道不做偏移判断）
    curbs = []
    for d in detections:
        if d.get("source") != "custom":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        if name_cn not in {"直行盲道", "straight_curb"}:
            continue
        bbox = d.get("bbox", [])
        if len(bbox) == 4:
            curbs.append({
                "bbox": bbox,
                "conf": d.get("confidence", 0.0)
            })

    if not curbs:
        return ""   # 看不到盲道，不判断偏移

    # 加权平均盲道中心 X（置信度越高权重越大）
    total_weight = 0.0
    weighted_cx = 0.0
    for c in curbs:
        x1, y1, x2, y2 = c["bbox"]
        cx = (x1 + x2) / 2.0
        w = c["conf"] if c["conf"] > 0 else 0.01
        weighted_cx += cx * w
        total_weight += w

    if total_weight <= 0:
        return ""

    curb_center_x = weighted_cx / total_weight
    screen_center_x = frame_width / 2.0
    offset = curb_center_x - screen_center_x          # 正：盲道在右；负：盲道在左
    offset_ratio = abs(offset) / frame_width

    if offset_ratio < DEVIATION_THRESHOLD_RATIO:
        return ""   # 偏移在容忍范围内

    # 冷却：避免频繁播报（帧率自适应）
    now = time.time()
    adaptive_cd = _adaptive_cooldown(DEVIATION_COOLDOWN, frame_width)
    if now - _last_deviation_time < adaptive_cd:
        return ""

    _last_deviation_time = now

    if offset > 0:
        return "注意，请向右调整，盲道在右侧"
    else:
        return "注意，请向左调整，盲道在左侧"


# ============ 生成朗读文案 ============
def _is_overlapping_x(obstacle_x1, obstacle_x2, curb_x1, curb_x2, threshold=0.3):
    """
    判断障碍物和盲道是否在水平方向上重叠
    threshold: 重叠比例（0~1），0.3 表示障碍物有 30% 在盲道水平范围内就算重叠
    """
    # 计算重叠区域
    overlap_left = max(obstacle_x1, curb_x1)
    overlap_right = min(obstacle_x2, curb_x2)

    if overlap_right <= overlap_left:
        return False

    overlap_width = overlap_right - overlap_left
    obstacle_width = obstacle_x2 - obstacle_x1

    if obstacle_width <= 0:
        return False

    # 障碍物宽度中有多少比例和盲道重叠
    overlap_ratio = overlap_width / obstacle_width
    return overlap_ratio >= threshold


def _detect_obstacles_on_curb(detections: list) -> list:
    """
    检测盲道上的障碍物（空间关联）
    返回：在盲道上的障碍物名称列表
    """
    # 收集盲道的 bbox（取置信度最高的几个）
    curbs = []
    for d in detections:
        if d.get("source") != "custom":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        # 只看盲道相关的（排除斑马线，因为斑马线不是盲道）
        if name_cn not in {"直行盲道", "停止盲道"}:
            continue
        bbox = d.get("bbox", [])
        if len(bbox) == 4:
            curbs.append({
                "name": name_cn,
                "bbox": bbox,
                "conf": d.get("confidence", 0)
            })

    if not curbs:
        return []  # 没有盲道，检测不到障碍物

    # 收集障碍物（人/车辆等）
    obstacles = []
    danger_names = {"人", "汽车", "公交车", "卡车", "摩托车", "自行车"}
    for d in detections:
        if d.get("source") != "official":
            continue
        name_cn = d.get("class_cn") or d.get("class", "")
        if name_cn not in danger_names:
            continue
        bbox = d.get("bbox", [])
        if len(bbox) == 4:
            obstacles.append({
                "name": name_cn,
                "bbox": bbox,
                "conf": d.get("confidence", 0)
            })

    if not obstacles:
        return []

    # 判断每个障碍物是否在盲道上
    on_curb_obstacles = []
    for obs in obstacles:
        obs_x1, obs_y1, obs_x2, obs_y2 = obs["bbox"]
        for curb in curbs:
            curb_x1, curb_y1, curb_x2, curb_y2 = curb["bbox"]

            # 水平对齐：障碍物和盲道有重叠
            if not _is_overlapping_x(obs_x1, obs_x2, curb_x1, curb_x2, threshold=0.3):
                continue

            # 纵向关系：障碍物的底部（靠近用户）要在盲道范围内
            # 盲道画面中通常是纵向延伸的，检查障碍物的 y 是否落在盲道 y 范围内
            obs_center_y = (obs_y1 + obs_y2) / 2
            curb_center_y = (curb_y1 + curb_y2) / 2

            # 障碍物中心在盲道纵向范围内（允许一定偏移）
            y_overlap = min(obs_y2, curb_y2) - max(obs_y1, curb_y1)
            if y_overlap > 0:
                on_curb_obstacles.append(obs["name"])
                break

    return on_curb_obstacles


def build_speak_text(detections: list, frame_width: int = 640, frame_height: int = 480,
                     current_time: float = 0.0, assistant=None) -> tuple:
    """
    根据检测结果生成适合盲人收听的朗读文案（整合防抖 + 盲道追踪 + 断裂填充）。

    优先级：盲道障碍物（确认后） > 路口（确认后） > 盲道偏移 > 危险障碍物（确认后） > 红绿灯状态 > 斑马线偏移 > 盲道状态

    返回: (speak_text, event_fingerprint)
      - speak_text: TTS 朗读文案
      - event_fingerprint: 事件语义指纹，用于去重（如 "盲道障碍:人:前方:近"）
    """
    global _in_intersection_mode, _last_intersection_warning_time
    global _last_zebra_deviation_time, _last_align_reminder_time
    global _traffic_light_state, _traffic_light_last_change_time, _last_traffic_light_time
    global _traffic_light_stable_frames, IN_INTERSECTION_MODE, _traffic_light_confirmed
    global _traffic_light_no_signal_announced, _traffic_light_text, _traffic_light_fp  # ★ 添加 global 声明
    global _zebra_deviation_active, _zebra_last_seen_time
    global _zebra_guide_phase, _zebra_guide_direction, _zebra_guide_remaining, _zebra_guide_last_announce
    global _zebra_guide_phone_hint
    global _zebra_guide_on_zebra_time, _zebra_no_light_announced, _zebra_on_zebra_zebra_lost_time, _pending_no_light_msg, _pending_on_zebra_msg
    global _zebra_phone_hint_ever, _zebra_announced_in_session
    global _zebra_guide_crossing_announced  # ★ 修复：声明global，否则赋值时Python视为局部变量
    global _crossing_mode_gps_confirmed, _crossing_green_passed  # ★ 修复：声明global
    global _crossing_last_red_remind_time, _crossing_green_first_time, _crossing_announced_green_pass  # ★ 修复：声明阶段3状态变量
    global _crossing_announced_deviation_mode, _crossing_exit_announced, _crossing_deviation_announced_time  # ★ 修复：声明阶段3状态变量
    global _pending_green_pass_msg  # ★ 待发送的"绿灯可通行"消息

    mode = get_detection_mode()  # ★ 获取当前识别模式

    if not detections:
        return "", ""

    frame_area = frame_width * frame_height

    # ===== 分类统计 =====
    official_items = set()   # 官方模型：人/车辆等
    custom_items = set()     # 自定义模型：盲道/台阶

    for d in detections:
        class_cn = d.get("class_cn", "")
        class_name = d.get("class", "")
        source = d.get("source", "")
        name_cn = class_cn if class_cn else class_name

        if source == "official":
            official_items.add(name_cn)
        elif source == "custom":
            custom_items.add(name_cn)
        elif source == "traffic_light":
            # 红绿灯模型的红灯/绿灯单独处理，不加入 custom_items
            pass

    # ===== Feature 6: 收集盲道（含跨帧断裂填充）====
    # 返回的 curbs 包含了上一帧虚拟填充的盲道
    curbs = _collect_curbs_with_gapfill(detections, frame_width)
    curb_count = sum(1 for c in curbs if not c.get("_virtual", False))   # 真实盲道数量

    # ===== Feature 8: 红绿灯状态机（路口模式）====
    # ★ 路口场景：盲道消失 + 斑马线出现 → 进入路口模式
    # ★ 路口模式中持续检测红绿灯状态，播报"红灯停"/"绿灯行"/"路口无信号灯"
    # ★ 斑马线消失超过配置时间 → 退出路口模式
    has_zebra_now = any(x in custom_items for x in {"斑马线", "zebra_crossing"})
    has_straight_curb_now = curb_count > 0
    now = time.time()

    # ★ 修改：路口模式只通过外部命令进入（nav_turn_approaching），不再视觉自主触发
    # 原逻辑：看到斑马线+没盲道 → 自动进入路口模式 → 误判率太高
    # 新逻辑：路口模式由 assistant_server 通过 GPS proximity 触发，视觉只负责执行

    # ★ 问题1修复：路口模式退出保护
    # GPS确认进入的路口模式需要更长时间才退出，防止盲人过马路到一半失去红绿灯保护
    # ★ 修复：on_zebra 阶段不执行此超时退出，因为用户正在抬手机找红绿灯
    #   斑马线短暂消失是正常的（手机角度变化），on_zebra 阶段有自己的10秒超时逻辑
    # ★ 关键修复：绿灯通行后（_crossing_green_passed=True）延长到60秒才退出，
    #   给盲人足够时间过完马路
    # ★ 关键修复2：检测到红绿灯时立即更新 _last_traffic_light_time，防止第一帧就退出
    has_traffic_light_now = any(d.get("source") == "traffic_light" for d in detections)
    if has_traffic_light_now:
        _last_traffic_light_time = now  # ★ 立即更新，防止退出逻辑误触发
    
    if IN_INTERSECTION_MODE and not has_zebra_now and _zebra_guide_phase != "on_zebra":
        if _crossing_green_passed:
            exit_timeout = 60  # ★ 绿灯通行后，给60秒过完马路
        elif _crossing_mode_gps_confirmed:
            exit_timeout = 20  # ★ GPS确认但未通行，20秒（给用户更多时间）
        else:
            exit_timeout = CONFIG["TRAFFIC_LIGHT_LOST_TIMEOUT"]
        if (now - _last_traffic_light_time) > exit_timeout:
            IN_INTERSECTION_MODE = False
            _traffic_light_state = "unknown"
            _traffic_light_confirmed = False
            _traffic_light_no_signal_announced = False
            _crossing_green_passed = False  # ★ 重置绿灯通行标志
            _crossing_mode_gps_confirmed = False
            print(f"[红绿灯] 斑马线消失{exit_timeout}秒，退出路口模式，重置状态")

    # 红绿灯检测逻辑（只在路口模式中运行）
    _traffic_light_text = ""
    _traffic_light_fp = ""
    traffic_light_detected_colors = set()

    # 从检测结果中提取红绿灯颜色
    # ★ 调试：打印detections中的红绿灯检测结果
    tl_detections = [d for d in detections if d.get("source") == "traffic_light"]
    if tl_detections:
        tl_info = [(d.get('class_cn'), f"{d.get('confidence', 0):.3f}") for d in tl_detections]
        print(f"[红绿灯调试] build_speak_text收到 {len(tl_detections)} 个红绿灯检测: {tl_info}")
    for d in detections:
        if d.get("source") == "traffic_light":
            class_name = d.get("class_cn") or d.get("class", "")
            if class_name == "红灯":
                traffic_light_detected_colors.add("red")
            elif class_name == "绿灯":
                traffic_light_detected_colors.add("green")
    if traffic_light_detected_colors:
        print(f"[红绿灯调试] 提取到的颜色: {traffic_light_detected_colors}")

    if IN_INTERSECTION_MODE:
        current_detected = "none"
        if "red" in traffic_light_detected_colors:
            current_detected = "red"
        elif "green" in traffic_light_detected_colors:
            current_detected = "green"

        if current_detected != "none":
            _last_traffic_light_time = now
            _traffic_light_stable_frames += 1
        else:
            _traffic_light_stable_frames = max(0, _traffic_light_stable_frames - 1)

        # 连续多帧确认后更新状态
        if _traffic_light_stable_frames >= CONFIG["TRAFFIC_LIGHT_STABLE_FRAMES"]:
            if current_detected == "red" and _traffic_light_state != "red":
                # ★ 记录是否是首次检测（从unknown状态）
                is_first_detection = (_traffic_light_state == "unknown")
                _traffic_light_state = "red"
                _traffic_light_confirmed = True
                _traffic_light_last_change_time = now
                # ★ 首次检测到红灯，提示用户等待绿灯
                if is_first_detection:
                    _traffic_light_text = "现在是红灯，请等一下，变成绿灯我会告诉你"
                else:
                    _traffic_light_text = "红灯，请等待"
                _traffic_light_fp = "traffic_light:red"
                _crossing_green_passed = False  # ★ 红灯亮起，恢复红绿灯检测
                print(f"[红绿灯] 红灯亮起 (首次检测={is_first_detection})")
            elif current_detected == "red" and _traffic_light_state == "red":
                # ★ 红灯持续，每2.5秒重复播报（需求：2.5秒播报一次）
                red_repeat_interval = CONFIG.get("TRAFFIC_LIGHT_RED_REPEAT_INTERVAL", 2.5)
                if _crossing_last_red_remind_time == 0.0:
                    _crossing_last_red_remind_time = _traffic_light_last_change_time
                if (now - _crossing_last_red_remind_time) >= red_repeat_interval:
                    _crossing_last_red_remind_time = now
                    _traffic_light_text = "红灯，请等待"
                    # ★ 关键修复：每次红灯重复播报使用不同的指纹（带时间戳），绕过30秒指纹去重
                    _traffic_light_fp = f"traffic_light:red_repeat:{int(now)}"
                    print(f"[红绿灯] 红灯持续，每{red_repeat_interval}秒重复提醒")
            elif current_detected == "green" and _traffic_light_state != "green":
                _traffic_light_state = "green"
                _traffic_light_confirmed = True
                _traffic_light_last_change_time = now
                # ★ 记录绿灯首次检测时间，延迟1秒后播报"可通行"（让用户有反应时间）
                if _crossing_green_first_time == 0.0:
                    _crossing_green_first_time = now
                    _crossing_announced_green_pass = False  # 重置，等延迟后播报
                _crossing_green_passed = True   # ★ 绿灯通行中，停止红绿灯检测，专注脚下
                _zebra_last_seen_time = now  # ★ 重置斑马线消失计时器，防止绿灯后立刻退出
                _crossing_last_red_remind_time = 0.0  # ★ 重置红灯提醒计时器
                print(f"[红绿灯] 绿灯亮起，进入通行模式（停止红绿灯检测，专注障碍物和斑马线偏移）")
            elif current_detected == "green" and _traffic_light_state == "red":
                # 红灯变绿灯
                _traffic_light_state = "green"
                _traffic_light_confirmed = True
                _traffic_light_last_change_time = now
                # ★ 记录绿灯首次检测时间，延迟1秒后播报"可通行"
                if _crossing_green_first_time == 0.0:
                    _crossing_green_first_time = now
                    _crossing_announced_green_pass = False
                _crossing_green_passed = True   # ★ 绿灯通行中
                _zebra_last_seen_time = now  # ★ 重置斑马线消失计时器
                _crossing_last_red_remind_time = 0.0  # ★ 重置红灯提醒计时器
                print(f"[红绿灯] 红灯变绿灯，进入通行模式（停止红绿灯检测，专注障碍物和斑马线偏移）")

        # 路口无红绿灯的情况
        # ★ 关键修复：只有 on_zebra 阶段才播报"无信号灯"（10秒超时）
        #   crossing 阶段已经找到红绿灯，不再播报"无信号灯"
        if (_zebra_guide_phase == "on_zebra"  # ★ 只在 on_zebra 阶段检查
            and not traffic_light_detected_colors 
            and not _traffic_light_no_signal_announced):
            time_since_on_zebra = now - _zebra_guide_on_zebra_time
            if time_since_on_zebra > CONFIG["TRAFFIC_LIGHT_NO_SIGNAL_WAIT"]:
                _traffic_light_text = "未检测到红绿灯，请自行观察后通行"
                _traffic_light_fp = "no_light_detected"
                _traffic_light_no_signal_announced = True
                print(f"[红绿灯] on_zebra阶段10秒未检测到红绿灯，播报提示")

    # ===== Feature 3: 路口取消机制 =====
    # 如果之前报过路口提醒，现在盲道重新出现 → 发送取消指令给助手停止 TTS
    if _in_intersection_mode:
        if _was_curb_recently_seen():
            # 盲道回来了，通知助手取消路口 TTS
            if assistant is not None:
                asyncio.create_task(
                    assistant.send_intersection_cancel(frame_width=frame_width)
                )
            _in_intersection_mode = False
        elif time.time() - _last_intersection_warning_time > INTERSECTION_COOLDOWN:
            # 冷却时间已过，路口提醒自然结束
            _in_intersection_mode = False

    parts = []

    # ===== Feature 1: 连续帧防抖——确认障碍物事件 =====
    confirmed_events = _confirm_obstacle_events(
        detections, frame_width, frame_area, curbs, current_time
    )

    # 从确认事件中分离盲道障碍物和自由障碍物
    confirmed_on_curb = [ev for ev in confirmed_events if ev[0] == "obstacle_on_curb"]
    confirmed_free = [ev for ev in confirmed_events if ev[0] == "obstacle_free"]

    # ===== Feature 8: 红绿灯播报 =====
    # ★ 优先级：红绿灯状态变化 > 障碍物/路口 > 偏移/盲道状态
    # ★ 修复：红绿灯放在 parts 构建的最后面，确保 "找到红绿灯" 在前面
    # ★ 关键修复：首次检测到红绿灯时立即播报，不需要检查冷却
    #   （_traffic_light_last_change_time 刚被更新为 now，差值为0）
    # ★ 暂存红绿灯文本，等所有 parts 构建完成后再添加
    _pending_traffic_light_text = _traffic_light_text if _traffic_light_text else ""

    # ===== 1. ★ 盲道障碍物（已确认才播报）=====
    # ★ 修复：路口模式/斑马线引导模式下，只播报前方近处的障碍物（过滤远处和左右侧的）
    # 避免左右侧汽车抢占"前方路口"/"红灯请等待"等关键提示音
    # ★ 关键修复：阶段3（crossing）中障碍物由阶段3自己的逻辑处理，这里完全跳过
    _in_zebra_or_intersection = _zebra_guide_phase or IN_INTERSECTION_MODE
    if not parts and confirmed_on_curb and _zebra_guide_phase != "crossing":
        # 选"最近"的障碍物（框面积最大 = 最近）
        best = None
        best_area = -1
        for ev in confirmed_on_curb:
            _, name, direction, distance = ev
            # ★ 路口/斑马线模式下，只保留前方近处的障碍物
            if _in_zebra_or_intersection:
                if direction != "前方" or distance != "近":
                    continue
            # 从 detections 找到对应障碍物的 bbox 算面积
            for d in detections:
                if d.get("source") != "official":
                    continue
                cn = d.get("class_cn") or d.get("class", "")
                if cn == name:
                    bbox = d.get("bbox", [])
                    if len(bbox) == 4:
                        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                        if area > best_area:
                            best_area = area
                            best = ev
                    break
        if best:
            _, name, direction, distance = best
            parts.append(f"{direction}{distance}有{name}，注意躲避")

    # ===== 2. ★ 路口检测（Feature 2: 盲道追踪——确认消失才报）====
    if not parts:
        # 用 Feature 6 的断裂填充后结果判断路口
        has_straight_curb = curb_count > 0
        has_zebra = any(
            (d.get("class_cn") or d.get("class", "")) == "斑马线"
            for d in detections if d.get("source") == "custom"
        )
        if not has_straight_curb and has_zebra:
            # Feature 2: 只有盲道连续丢失 N 帧才报路口（防误判）
            # ★ 斑马线引导状态机激活时，跳过独立路口检测（避免和引导流程冲突）
            # ★ 导航模式下也跳过独立路口检测（路口由 GPS AI 负责，避免重复播报）
            if _is_curb_truly_gone() and not _zebra_guide_phase and mode != "navigation":
                intersection_text = _detect_intersection(detections)
                if intersection_text:
                    parts.append(intersection_text)

    # ===== 3. ★ 盲道偏移检测（无障碍物、无路口时）=====
    if not parts:
        deviation_text = _detect_curb_deviation(detections, frame_width)
        if deviation_text:
            parts.append(deviation_text)

    # ===== 4. ★ 危险障碍物（不在盲道上，已确认，带方向+距离）=====
    # ★ 修复：只保留"前方"的自由障碍物，过滤掉左侧/右侧的
    # ★ 路口模式/斑马线引导模式下，只保留前方近处障碍物（盲人过马路需要知道前方有没有人）
    # ★ 关键修复：阶段3（crossing）中障碍物由阶段3自己的逻辑处理，这里完全跳过
    if not parts and confirmed_free and _zebra_guide_phase != "crossing":
        if _in_zebra_or_intersection:
            # 路口/斑马线模式下：只播报前方近处的自由障碍物（人/车），过滤中远距离和左右侧
            front_near_free = [ev for ev in confirmed_free if ev[2] == "前方" and ev[3] == "近"]
            if front_near_free:
                ev = front_near_free[0]
                _, name, direction, distance = ev
                parts.append(f"{direction}{distance}有{name}，注意")
        else:
            # 非路口模式：只保留前方障碍物
            front_free = [ev for ev in confirmed_free if ev[2] == "前方"]
            if front_free:
                # 取最近的
                sorted_free = sorted(front_free, key=lambda ev: {"近": 0, "中": 1, "远": 2}.get(ev[3], 1))
                ev = sorted_free[0]
                _, name, direction, distance = ev
                parts.append(f"{direction}{distance}有{name}")

    # ===== 路口提醒（盲道消失+斑马线出现）=====
    # ★ 非导航模式下整个会话只报一次"前方路口"，避免反复播报
    has_zebra_now = any(
        (d.get("class_cn") or d.get("class", "")) == "斑马线"
        for d in detections if d.get("source") == "custom"
    )
    has_straight_curb_now = curb_count > 0
    if not parts and not has_straight_curb_now and has_zebra_now:
        if _is_curb_truly_gone():
            now = time.time()
            if now - _last_align_reminder_time > CONFIG["ALIGN_REMINDER_COOLDOWN"]:
                if mode == "navigation" or not _zebra_announced_in_session:
                    _last_align_reminder_time = now
                    parts.append("前方路口，请注意方向")
                    if mode != "navigation":
                        _zebra_announced_in_session = True

    # ===== 斑马线偏移检测状态机 =====
    # ★ 只有全屏是斑马线时才进入偏移检测模式，避免导航经过斑马线时误触发
    # 状态机：OFF → 检测到斑马线覆盖>50% → ON → 斑马线消失10秒 → OFF
    now = time.time()
    zebra_detected = any(
        (d.get("class_cn") or d.get("class", "")) == "斑马线"
        for d in detections if d.get("source") == "custom"
    )

    # ★ 计算斑马线面积（多阶段共用）
    zebra_area = 0.0
    zebra_center_x = 0.0
    zebra_count = 0
    for d in detections:
        if (d.get("class_cn") or d.get("class", "")) == "斑马线" and d.get("source") == "custom":
            bbox = d.get("bbox", [])
            if len(bbox) == 4:
                zebra_area += (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                zebra_center_x += (bbox[0] + bbox[2]) / 2
                zebra_count += 1
    frame_area = frame_width * frame_height if frame_width > 0 and frame_height > 0 else 1
    area_ratio = zebra_area / frame_area if frame_area > 0 else 0
    if zebra_count > 0:
        zebra_center_x /= zebra_count

    # ===== 斑马线引导三阶段状态机（导航联动）=====
    # 阶段1 seeking → 阶段2 on_zebra → 阶段3 crossing → 斑马线消失 → 退出
    if _zebra_guide_phase:
        # ★ 调试日志：打印斑马线检测状态
        if int(now) % 5 == 0:  # 每5秒打印一次，避免刷屏
            print(f"[斑马线引导] phase={_zebra_guide_phase}, zebra_detected={zebra_detected}, zebra_count={zebra_count}, area_ratio={area_ratio:.2%}")
    if _zebra_guide_phase == "seeking":
        if zebra_detected and zebra_count > 0:
            # 检查是否站上斑马线（覆盖≥60%）
            if area_ratio >= ZEBRA_ON_ZEBRA_AREA:
                _zebra_guide_phase = "on_zebra"
                _zebra_guide_phone_hint = False
                _zebra_guide_last_announce = now
                print(f"[斑马线引导] 阶段1→2: 站上斑马线（覆盖{area_ratio:.1%}）")
            elif (now - _zebra_guide_last_announce) >= ZEBRA_GUIDE_COOLDOWN:
                # ★ 精简播报：短促明确，适合盲人户外使用
                x_ratio = zebra_center_x / frame_width
                if x_ratio < 0.35:
                    guide_text = "斑马线在左边"
                elif x_ratio > 0.65:
                    guide_text = "斑马线在右边"
                else:
                    guide_text = "斑马线在正前方"
                _zebra_guide_last_announce = now
                parts.append(guide_text)
                print(f"[斑马线引导] 阶段1: x_ratio={x_ratio:.2f} → {guide_text}")

    elif _zebra_guide_phase == "on_zebra":
        # 阶段2：站上斑马线，提示用户抬手机看红绿灯（整个会话只播一次）
        # ★ 修复：只有GPS确认的路口才激活红绿灯模式，纯视觉检测不自主激活
        # 防止视觉误检斑马线导致错误进入红绿灯模式
        if not IN_INTERSECTION_MODE and _crossing_mode_gps_confirmed:
            IN_INTERSECTION_MODE = True
            _traffic_light_state = "unknown"
            _traffic_light_stable_frames = 0
            _traffic_light_confirmed = False
            _traffic_light_no_signal_announced = False
            _crossing_green_passed = False  # ★ 重置绿灯通行标志
            _last_traffic_light_time = 0.0  # ★ 重置红绿灯时间，防止旧值导致立刻播报"无信号灯"
            print(f"[红绿灯] on_zebra 阶段激活红绿灯路口模式（GPS确认）")

        if not _zebra_guide_phone_hint and not _zebra_phone_hint_ever:
            _zebra_guide_phone_hint = True
            _zebra_phone_hint_ever = True  # ★ 全局标记，防止反复进出斑马线重复播报
            _zebra_guide_on_zebra_time = now  # ★ 记录进入 on_zebra 的时间
            _pending_on_zebra_msg = True  # ★ 标记待发送，绕过冷却
            print(f"[斑马线引导] 阶段2: 提示用户抬手机看红绿灯")

        # 检测是否有红绿灯（traffic_light来源的检测结果）
        has_traffic_light = any(
            d.get("source") == "traffic_light"
            for d in detections
        )
        # ★ 调试：打印has_traffic_light状态
        print(f"[红绿灯调试] on_zebra阶段: has_traffic_light={has_traffic_light}, "
              f"_crossing_mode_gps_confirmed={_crossing_mode_gps_confirmed}, "
              f"IN_INTERSECTION_MODE={IN_INTERSECTION_MODE}")
        if has_traffic_light and _crossing_mode_gps_confirmed:
            _zebra_guide_phase = "crossing"
            # ★ crossing 阶段激活红绿灯路口模式（仅GPS确认时）
            # ★ 修复：不要重置状态，保持连续性
            if not IN_INTERSECTION_MODE:
                IN_INTERSECTION_MODE = True
                _traffic_light_state = "unknown"
                _traffic_light_stable_frames = 0
                _traffic_light_confirmed = False
                _traffic_light_no_signal_announced = False
                _last_traffic_light_time = time.time()  # ★ 重置为当前时间，防止旧值导致立刻播报"无信号灯"
                print(f"[红绿灯] crossing阶段首次激活红绿灯模式，重置无信号灯计时器")
            # ★ 修复：进入阶段3时，清除 on_zebra 的待发送消息（避免和红灯播报撞车）
            _pending_on_zebra_msg = False
            # ★ 播报"找到红绿灯了"（只播一次，防止反复检测到又丢失导致重复）
            if not _zebra_guide_crossing_announced:
                _zebra_guide_crossing_announced = True
                parts.append("找到红绿灯")
            print(f"[斑马线引导] ★★★ 阶段3 crossing: 检测到红绿灯，进入过马路模式 ★★★")
        elif not zebra_detected:
            # ★ on_zebra 阶段斑马线短暂消失不立即退出，给10秒缓冲
            # （用户站在斑马线上抬手机看红绿灯时，手机角度变化会导致斑马线短暂丢失）
            # 3秒太短，容易误退；10秒给用户足够时间调整手机角度
            if _zebra_on_zebra_zebra_lost_time == 0.0:
                _zebra_on_zebra_zebra_lost_time = now
            elif (now - _zebra_on_zebra_zebra_lost_time) > 10.0:
                _zebra_guide_phase = "seeking"
                _zebra_guide_phone_hint = False
                _zebra_on_zebra_zebra_lost_time = 0.0
                print(f"[斑马线引导] 阶段2→1: 斑马线消失10秒，回到找斑马线模式")
        else:
            # 斑马线仍然存在
            _zebra_on_zebra_zebra_lost_time = 0.0
            # ★ 站在斑马线上超过8秒没检测到红绿灯，提示无信号灯
            # 8秒缓冲：给盲人足够时间抬手机对准红绿灯
            if _zebra_guide_on_zebra_time > 0 and (now - _zebra_guide_on_zebra_time) > 8.0 and not _zebra_no_light_announced:
                _zebra_no_light_announced = True
                _pending_no_light_msg = True  # ★ 标记待发送，绕过冷却
                print(f"[斑马线引导] 阶段2: 站在斑马线8秒未检测到红绿灯")

    elif _zebra_guide_phase == "crossing":
        # 阶段3：过马路，红绿灯检测 + 斑马线偏移
        # ★★★ 阶段3核心逻辑 ★★★
        # 1. 检测红绿灯状态（红灯等待，绿灯通行）
        # 2. 绿灯后延迟1秒播报"可通行"
        # 3. 开启斑马线偏移检测（已检测到斑马线时）
        # 4. 斑马线消失3秒后退出路口模式
        
        # ★ 处理绿灯延迟播报（绿灯后等待1秒再播报）
        green_wait_time = CONFIG.get("ZEBRA_CROSSING_GREEN_WAIT", 1.0)
        if _crossing_green_passed and not _crossing_announced_green_pass:
            if _crossing_green_first_time > 0 and (now - _crossing_green_first_time) >= green_wait_time:
                # ★ 延迟播报"绿灯可通行"（单独发送，不和障碍物合并）
                _crossing_announced_green_pass = True
                _pending_green_pass_msg = True  # ★ 标记为待发送，绕过冷却，单独发送
                print(f"[斑马线引导] 阶段3: 绿灯延迟{green_wait_time}秒播报，用户可以通行了")
        
        # ★ 斑马线偏移模式开启播报（绿灯后立即播报，不依赖斑马线检测）
        if _crossing_green_passed and not _crossing_announced_deviation_mode:
            _crossing_announced_deviation_mode = True
            parts.append("已开启斑马线偏移检测，请保持走在斑马线上")
            print(f"[斑马线引导] 阶段3: 播报偏移检测模式开启")
        
        # ★ 阶段3障碍物播报限制：只播报非常近的前方障碍物（≤1米）
        # 避免过马路的盲人被障碍物播报干扰
        _crossing_near_obstacle_reported = False
        if confirmed_on_curb:
            for ev in confirmed_on_curb:
                _, name, direction, distance = ev
                if direction == "前方" and distance == "近":
                    # 只有极近的障碍物才播报（防止盲人撞上）
                    for det in detections:
                        if det.get("source") != "official":
                            continue
                        cn = det.get("class_cn") or det.get("class", "")
                        if cn == name:
                            bbox = det.get("bbox", [])
                            if len(bbox) == 4:
                                meters = _estimate_forward_distance_meters(bbox, frame_height)
                                if 0 < meters <= 1.5:  # 只有1.5米以内才播报
                                    parts.append(f"前方{meters:.1f}米有{name}，请注意")
                                    _crossing_near_obstacle_reported = True
                                    break
                    if _crossing_near_obstacle_reported:
                        break
        
        # ★ 绿灯通行后，斑马线消失10秒才退出（用户在过马路，斑马线会因角度变化短暂消失）
        if _crossing_green_passed:
            if not zebra_detected:
                if _zebra_last_seen_time == 0.0:
                    _zebra_last_seen_time = now
                elif (now - _zebra_last_seen_time) > 10.0:  # ★ 10秒缓冲（原来3秒太短）
                    # ★ 退出路口模式时，重置所有阶段3状态
                    _zebra_guide_phase = ""
                    _zebra_guide_direction = ""
                    _zebra_guide_phone_hint = False
                    _zebra_guide_on_zebra_time = 0.0
                    _zebra_no_light_announced = False
                    _zebra_last_seen_time = 0.0
                    # ★ 退出引导时同步退出红绿灯路口模式
                    IN_INTERSECTION_MODE = False
                    _traffic_light_state = "unknown"
                    _traffic_light_confirmed = False
                    _traffic_light_no_signal_announced = False
                    _crossing_green_passed = False
                    _crossing_mode_gps_confirmed = False
                    _crossing_green_first_time = 0.0
                    _crossing_announced_green_pass = False
                    _crossing_announced_deviation_mode = False
                    _crossing_exit_announced = False
                    _crossing_last_red_remind_time = 0.0
                    # ★ 通知小助手退出路口模式
                    if assistant is not None:
                        asyncio.create_task(assistant.send_crossing_exit())
                    print(f"[斑马线引导] 阶段3→退出: 绿灯通行后斑马线消失3秒，用户已通过马路")
                    # ★ 发送退出播报
                    parts.append("已通过路口，路口模式结束")
            else:
                _zebra_last_seen_time = now  # 重置计时器
        else:
            # ★ 红灯等待时，重置绿灯相关状态
            _crossing_green_first_time = 0.0
            _crossing_announced_green_pass = False

    # ★ 斑马线偏移检测状态机（仅 crossing 阶段且绿灯通行后才激活）
    # on_zebra 阶段用户正在抬手机找红绿灯，不应进行偏移检测
    # ★ 阶段3绿灯通行后用户在走动，斑马线面积小，用低阈值激活偏移检测
    # ★ 关键修复：只有绿灯通行后（_crossing_green_passed=True）才进入偏移检测模式
    if _zebra_guide_phase == "crossing" and zebra_detected and _crossing_green_passed:
        _zebra_last_seen_time = now
        if not _zebra_deviation_active:
            if area_ratio >= ZEBRA_DEVIATION_ACTIVATION_RATIO:
                _zebra_deviation_active = True
                print(f"[斑马线偏移] 进入偏移检测模式（覆盖面积={area_ratio:.1%}，阈值={ZEBRA_DEVIATION_ACTIVATION_RATIO:.0%}）")
    elif _zebra_deviation_active and (now - _zebra_last_seen_time) > ZEBRA_EXIT_TIMEOUT:
        _zebra_deviation_active = False
        _crossing_announced_deviation_mode = False  # ★ 重置偏移模式开启标志
        print(f"[斑马线偏移] 退出偏移检测模式（斑马线消失{ZEBRA_EXIT_TIMEOUT:.0f}秒）")

    # ★ 只在偏移检测模式激活且绿灯通行后才检测偏移
    # ★ 使用更长的冷却时间（3秒），避免频繁播报
    dev_cooldown = CONFIG.get("ZEBRA_DEVIATION_ANNOUNCE_COOLDOWN", 3.0)
    if _zebra_deviation_active and _crossing_green_passed and (now - _crossing_deviation_announced_time) > dev_cooldown:
        zebra_dev = _detect_zebra_deviation(detections, frame_width)
        if zebra_dev:
            _crossing_deviation_announced_time = now
            parts.append(zebra_dev)

    # ===== 5. 盲道状态 =====
    # 直行盲道 — ★ 导航中不播报"盲道延伸可直行"（废话，盲人走盲道当然直行）
    # 只在非导航模式（once/continuous）且无任何警告时才播报
    current_mode = get_detection_mode()
    if current_mode != "navigation":
        if any(x in custom_items for x in {"直行盲道", "straight_curb"}):
            if not any("躲避" in p or "路口" in p or "调整" in p or "有" in p for p in parts):
                parts.append("盲道延伸，可直行")
    # 停止盲道
    if any(x in custom_items for x in {"停止盲道", "stop_curb"}):
        if not any("停止" in p for p in parts):
            # ★ 停止盲道距离估算（估算还需走多远才到停止线）
            meter_text = ""
            for det in detections:
                if (det.get("class_cn") or det.get("class", "")) in {"停止盲道", "stop_curb"}:
                    if det.get("source") == "custom":
                        meters = _estimate_forward_distance_meters(det.get("bbox", []), frame_height)
                        if meters > 0 and meters < 999:
                            meter_text = _get_stop_distance_text(meters)
                        break
            if meter_text:
                parts.append(f"注意停止盲道，{meter_text}")
            else:
                parts.append("注意停止盲道，请停步")
    # 斑马线（路口场景不在此处重复播报，引导模式下也不重复播报）
    # ★ 非导航模式下整个会话只播报一次"前方斑马线"，避免反复播报干扰用户
    if any(x in custom_items for x in {"斑马线", "zebra_crossing"}):
        if not any("斑马线" in p or "路口" in p for p in parts):
            if mode == "navigation" or not _zebra_announced_in_session:
                parts.append("前方斑马线，请注意过往车辆")
                if mode != "navigation":
                    _zebra_announced_in_session = True
    # 路沿石
    if any(x in custom_items for x in {"路沿", "路沿石", "curb"}):
        if not any("路沿" in p for p in parts):
            parts.append("注意路沿石台阶")

    if not parts and not _pending_traffic_light_text:
        return "", ""

    # ★ 斑马线引导模式下，过滤掉无用的路口/斑马线提示（避免干扰引导流程）
    # ★ 阶段1/2: 过滤 "请注意方向"、"请注意过往车辆"
    # ★ 阶段3: 额外过滤 "前方斑马线"、"路沿石台阶"（用户正在过马路，不需要路沿石提醒）
    if _zebra_guide_phase:
        parts = [p for p in parts if "请注意方向" not in p and "请注意过往车辆" not in p]
        if _zebra_guide_phase == "crossing":
            parts = [p for p in parts if "前方斑马线" not in p and "路沿石" not in p]

    # ===== 事件指纹生成（用于去重）=====
    fp_parts = ""
    if confirmed_on_curb:
        best = confirmed_on_curb[0]
        _, name, direction, distance = best
        fp_parts = f"盲道障碍:{name}:{direction}:{distance}"
    elif any("路口" in p for p in parts):
        fp_parts = "路口"
    elif any("调整" in p for p in parts):
        fp_parts = "偏移"
    elif confirmed_free:
        ev = sorted(confirmed_free, key=lambda x: {"近": 0, "中": 1, "远": 2}.get(x[3], 1))[0]
        _, name, direction, distance = ev
        fp_parts = f"自由障碍:{name}:{direction}:{distance}"
    elif any("停止" in p for p in parts):
        fp_parts = "状态:停止盲道"
    elif any("红灯" in p or "绿灯" in p or "无信号灯" in p for p in parts):
        fp_parts = _traffic_light_fp or "traffic_light"
    elif any("斑马线" in p for p in parts):
        # ★ 区分引导消息和普通斑马线消息的指纹，避免30秒指纹去重阻塞引导
        if any("斑马线在" in p or "已到达斑马线" in p or "站在斑马线" in p or "未检测到红绿灯" in p for p in parts):
            # ★ 指纹包含方向，方向变了指纹不同，不会被去重挡掉
            for p in parts:
                if "斑马线在左边" in p:
                    fp_parts = "斑马线引导:左"
                    break
                elif "斑马线在右边" in p:
                    fp_parts = "斑马线引导:右"
                    break
                elif "斑马线在正前方" in p:
                    fp_parts = "斑马线引导:前"
                    break
                elif "站在斑马线" in p:
                    fp_parts = "斑马线引导:站上"
                    break
                elif "未检测到红绿灯" in p:
                    fp_parts = "斑马线引导:无灯"
                    break
                elif "找到红绿灯" in p:
                    fp_parts = "斑马线引导:有灯"
                    break
            if not fp_parts:
                fp_parts = "斑马线引导"
        # ★ 偏移消息：指纹包含方向，方向变了指纹不同，不会被去重挡掉
        elif any("偏离斑马线" in p for p in parts):
            for p in parts:
                if "向右前方走" in p:
                    fp_parts = "斑马线偏移:右"
                    break
                elif "向左前方走" in p:
                    fp_parts = "斑马线偏移:左"
                    break
            if not fp_parts:
                fp_parts = "斑马线偏移"
        else:
            fp_parts = "斑马线"
    elif any("信号灯" in p for p in parts):
        fp_parts = "对准信号灯"
    elif any("路沿" in p for p in parts):
        fp_parts = "路沿石"
    elif any("直行" in p or "延伸" in p for p in parts):
        fp_parts = "状态:直行"

    # ★ 在返回前，把红绿灯文本追加到 parts 最后（确保 "找到红绿灯" 在前面）
    # ★ 关键修复：重新获取 _traffic_light_text，因为状态机在后面才设置它
    _pending_traffic_light_text = _traffic_light_text if _traffic_light_text else ""
    if _pending_traffic_light_text:
        parts.append(_pending_traffic_light_text)
        # 更新 fp
        if _traffic_light_fp:
            fp_parts = _traffic_light_fp

    return "，".join(parts), fp_parts


# ============ 识别模式控制 ============
# off         - 不识别，不发送任何消息（默认）
# navigation  - 导航模式，仅导航中识别
# continuous  - 持续识别（测试用）
# once        - 识别一次后自动切回 off（超时10秒也自动切回）
_detection_mode = "off"  # 默认关闭，等小助手发命令开启
_detection_mode_lock = threading.Lock()
_detection_mode_set_time = 0.0  # 模式切换时间，用于 once 超时


def set_detection_mode(mode: str):
    """设置识别模式（线程安全）"""
    global _detection_mode, _detection_mode_set_time
    with _detection_mode_lock:
        old_mode = _detection_mode
        _detection_mode = mode
        _detection_mode_set_time = time.time()
    print(f"[{datetime.now()}] 识别模式切换: {old_mode} → {mode}")
    # ★ 切换到导航模式时，自动停止找店模式
    if mode == "navigation" and _find_mode_active:
        _stop_find_mode()
        print(f"[{datetime.now()}] 🔍 导航模式启动，自动停止找店模式")


def get_detection_mode() -> str:
    """获取当前识别模式（线程安全，once 模式超时自动切回 off）"""
    global _detection_mode
    with _detection_mode_lock:
        # once 模式超时 10 秒自动切回 off
        if _detection_mode == "once" and (time.time() - _detection_mode_set_time) > 10:
            _detection_mode = "off"
            print(f"[{datetime.now()}] 识别模式超时自动切换: once → off")
        return _detection_mode


# ============ 小助手 WebSocket 服务端 ============
class AssistantServer:
    """
    监听 8768 端口的 WebSocket 服务端，等待小助手（assistant_server.py）主动连接。
    小助手通过 VisionAIClient 连接到 ws://127.0.0.1:8768。
    """

    def __init__(self):
        self.ws = None
        self.connected = False
        self.host = ASSISTANT_HOST
        self.port = ASSISTANT_PORT
        self.last_sent_text = ""   # 上次发送的文案（用于冷却）
        self.last_send_time = 0     # 上次发送时间
        self.cooldown = 3.0         # 冷却时间（秒），避免重复播报
        self._server = None
        self._last_event_fp = ""    # 上次发送的事件指纹（用于去重）
        self._last_fp_time = 0.0    # 上次发送指纹的时间
        self._last_deviation_send_time = 0.0  # ★ 偏移消息独立冷却时间

    async def start(self):
        """启动 WebSocket 服务端，监听 8768 端口，等待小助手连接"""
        try:
            self._server = await websockets.serve(
                self._handle_connection, self.host, self.port
            )
            print(f"[{datetime.now()}] ✅ 小助手服务端已启动，监听: ws://{self.host}:{self.port}")
            print(f"[{datetime.now()}] 等待小助手连接...")
        except Exception as e:
            print(f"[{datetime.now()}] ❌ 小助手服务端启动失败: {e}")

    async def _handle_connection(self, websocket):
        """处理小助手的连接，接收模式切换命令"""
        global _zebra_guide_phase, _zebra_guide_direction, _zebra_guide_remaining, _zebra_guide_last_announce
        global _zebra_guide_phone_hint
        global IN_INTERSECTION_MODE, _traffic_light_state, _traffic_light_stable_frames
        global _traffic_light_confirmed, _traffic_light_no_signal_announced
        client_addr = websocket.remote_address
        self.ws = websocket
        self.connected = True
        print(f"[{datetime.now()}] ✅ 小助手已连接: {client_addr}")

        try:
            # 持续保持连接，接收小助手发来的命令
            async for message in websocket:
                try:
                    data = json.loads(message)
                    cmd = data.get("command", "")

                    if cmd == "set_mode":
                        mode = data.get("mode", "off")
                        if mode in ("off", "navigation", "continuous", "once"):
                            # ★★★ global声明必须在赋值之前
                            global _crossing_mode_gps_confirmed, IN_INTERSECTION_MODE, _traffic_light_state, _traffic_light_stable_frames, _traffic_light_confirmed, _traffic_light_no_signal_announced, _crossing_green_passed
                            set_detection_mode(mode)
                            # ★ 切换到 once/off 时清空事件历史，避免旧数据干扰新检测
                            if mode in ("once", "off"):
                                _event_history.clear()
                                _curb_history.clear()
                                _tracked_objects.clear()
                                _last_frame_curbs = []
                            # ★ 切换到 off/navigation 时清除斑马线引导状态
                            if mode in ("off", "navigation"):
                                _zebra_guide_phase = ""
                                _zebra_guide_direction = ""
                                _zebra_guide_phone_hint = False
                                _zebra_phone_hint_ever = False
                                _zebra_guide_on_zebra_time = 0.0
                                _zebra_no_light_announced = False
                                _crossing_mode_gps_confirmed = False
                                _pending_on_zebra_msg = False
                                _pending_no_light_msg = False
                                _pending_green_pass_msg = False
                            # ★ 手动切换到 navigation 模式时，视为用户确认进入路口
                            if mode == "navigation":
                                _crossing_mode_gps_confirmed = True
                                # ★ 关键修复：重置红绿灯时间，防止立即触发"斑马线消失退出"逻辑
                                global _last_traffic_light_time
                                _last_traffic_light_time = time.time()
                                # ★ 关键修复：手动开启路口模式时，也要启动斑马线引导（阶段1: 找斑马线）
                                # 否则 _zebra_guide_phase 为空，红绿灯模型不会运行
                                if not _zebra_guide_phase:
                                    _zebra_guide_phase = "seeking"
                                    _zebra_guide_direction = "过马路"
                                    print(f"[{datetime.now()}] 手动开启路口模式，启动斑马线引导（阶段1: 找斑马线）")
                            # ★ 切换到 off 时重置红绿灯状态
                            if mode == "off":
                                IN_INTERSECTION_MODE = False
                                _traffic_light_state = "unknown"
                                _traffic_light_stable_frames = 0
                                _traffic_light_confirmed = False
                                _traffic_light_no_signal_announced = False
                                _crossing_green_passed = False
                            print(f"[{datetime.now()}] 模式切换到 {mode}，已清空事件历史，重置红绿灯状态")
                            # 回复确认
                            await websocket.send(json.dumps({
                                "type": "mode_changed",
                                "mode": mode
                            }, ensure_ascii=False))
                        else:
                            print(f"[{datetime.now()}] 未知模式: {mode}")
                    elif cmd == "start_find_mode":
                        # ★ 找店模式：小助手发来的，目标文字在 target 字段，搜索词在 keywords 字段
                        target = data.get("target", "").strip()
                        keywords = data.get("keywords", "").strip()
                        if target:
                            _start_find_mode(target, None, self, keywords)
                            print(f"[{datetime.now()}] 🔍 找店模式启动: 目标=\"{target}\", 搜索词=\"{keywords}\"")
                            await websocket.send(json.dumps({
                                "type": "find_mode_started",
                                "target": target,
                            }, ensure_ascii=False))
                        else:
                            print(f"[{datetime.now()}] 🔍 找店模式启动失败: 未指定目标")
                    elif cmd == "stop_find_mode":
                        _stop_find_mode()
                        print(f"[{datetime.now()}] 🔍 找店模式已停止")
                    elif cmd == "nav_turn_approaching":
                        # ★ 导航联动：转弯接近，进入斑马线引导模式（阶段1: 找斑马线）
                        direction = data.get("direction", "")
                        remaining = data.get("remaining", "0")
                        _zebra_guide_phase = "seeking"
                        _zebra_guide_direction = direction
                        _zebra_guide_remaining = float(remaining) if remaining else 0.0
                        _zebra_guide_last_announce = 0.0  # 重置冷却，立即播报
                        _zebra_guide_phone_hint = False
                        _crossing_mode_gps_confirmed = True  # ★ 问题1修复：标记为GPS确认进入
                        print(f"[{datetime.now()}] 🧭 斑马线引导启动(阶段1): {direction} 距离{remaining}米")
                        # ★ 拟人化：进入路口引导时向用户解释流程
                        _pending_zebra_guide_intro_msg = True
                        await websocket.send(json.dumps({
                            "type": "zebra_guide_started",
                            "direction": direction,
                        }, ensure_ascii=False))
                    elif cmd == "nav_turn_passed":
                        # ★ 导航联动：转弯已过，退出斑马线引导模式
                        _zebra_guide_phase = ""
                        _zebra_guide_direction = ""
                        _zebra_guide_phone_hint = False
                        _crossing_mode_gps_confirmed = False  # ★ 问题1修复：重置GPS确认标记
                        _crossing_green_passed = False  # ★ 重置绿灯通行标志
                        print(f"[{datetime.now()}] 🧭 斑马线引导退出: 转弯已过")
                    else:
                        print(f"[{datetime.now()}] 未知命令: {cmd}")
                except json.JSONDecodeError:
                    print(f"[{datetime.now()}] 收到非JSON消息: {message[:100]}")
        except websockets.exceptions.ConnectionClosed:
            print(f"[{datetime.now()}] ⚠️ 小助手连接断开: {client_addr}")
        except Exception as e:
            print(f"[{datetime.now()}] ⚠️ 小助手连接异常: {e}")
        finally:
            self.connected = False
            self.ws = None
            print(f"[{datetime.now()}] 等待小助手重新连接...")

    async def send_obstacle_warning(self, text: str, frame_width: int = 640,
                                      event_fingerprint: str = "", force: bool = False):
        """发送障碍物警告给小助手（全局冷却 + 事件指纹去重）"""
        if not text:
            return

        now = time.time()

        # ★ force=True 时跳过所有冷却检查（用于重要消息如"未检测到红绿灯"）
        if not force:
            # ★ 偏移消息用独立冷却（3秒），不受障碍物全局冷却限制
            is_deviation = "偏离斑马线" in text
            # ★ 斑马线引导消息用独立冷却（5秒），不受障碍物全局冷却限制
            is_zebra_guide = "斑马线在" in text or "已到达斑马线" in text or "站在斑马线" in text or "未检测到红绿灯" in text
            # ★ 红绿灯消息用独立冷却（2秒），不受障碍物全局冷却限制
            is_traffic_light = "红灯" in text or "绿灯" in text or "无信号灯" in text or "找到红绿灯" in text
            if is_deviation:
                if (now - self._last_deviation_send_time) < CONFIG.get("ZEBRA_DEVIATION_COOLDOWN", 3.0):
                    return
                self._last_deviation_send_time = now
            elif is_zebra_guide:
                if (now - getattr(self, '_last_zebra_guide_send_time', 0.0)) < ZEBRA_GUIDE_COOLDOWN:
                    return
                self._last_zebra_guide_send_time = now
            elif is_traffic_light:
                if (now - getattr(self, '_last_traffic_light_send_time', 0.0)) < CONFIG["TRAFFIC_LIGHT_COOLDOWN"]:
                    return
                self._last_traffic_light_send_time = now
            else:
                # 非偏移/引导/红绿灯消息：全局冷却
                # ★ 斑马线引导模式下，障碍物消息冷却加长到15秒（避免干扰引导信息）
                if _zebra_guide_phase:
                    if (now - self.last_send_time) < 15.0:
                        return
                elif (now - self.last_send_time) < CONFIG["GLOBAL_SEND_COOLDOWN"]:
                    return

            # ★ 事件指纹去重：同一事件 CONFIG 秒内不重复发送
            if event_fingerprint and event_fingerprint == self._last_event_fp:
                if (now - self._last_fp_time) < CONFIG["FINGERPRINT_COOLDOWN"]:
                    return  # 同一事件还在持续，30秒内不重复发

        self.last_sent_text = text
        self.last_send_time = now

        if self.connected and self.ws:
            try:
                msg = {
                    "type": "obstacle_warning",
                    "text": text.strip()
                }
                await self.ws.send(json.dumps(msg, ensure_ascii=False))
                print(f"[{datetime.now()}] 发送 [全局{CONFIG['GLOBAL_SEND_COOLDOWN']:.0f}s] [fp={event_fingerprint or 'n/a'}]: {text}")
            except Exception as e:
                print(f"[{datetime.now()}] 发送失败: {e}")
                self.connected = False

        # 更新指纹记录
        if event_fingerprint:
            self._last_event_fp = event_fingerprint
            self._last_fp_time = now

    async def send_intersection_cancel(self, frame_width: int = 640):
        """
        Feature 3: 发送路口取消指令给小助手。
        当盲道重新出现时调用，助手收到后停止路口 TTS。
        """
        if not self.connected or not self.ws:
            return

        now = time.time()
        adaptive_cd = _adaptive_cooldown(3.0, frame_width)

        try:
            msg = {
                "type": "cancel_intersection",   # Feature 3: 路口取消消息类型
                "text": ""
            }
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
            print(f"[{datetime.now()}] 发送路口取消指令（{adaptive_cd:.1f}s冷却）")
        except Exception as e:
            print(f"[{datetime.now()}] 路口取消发送失败: {e}")
            self.connected = False

    async def send_crossing_exit(self):
        """
        ★ 路口模式退出通知：发送给助手，通知GPS AI恢复正常的导航播报。
        阶段3结束后调用，助手收到后恢复GPS播报。
        """
        if not self.connected or not self.ws:
            return

        try:
            msg = {
                "type": "crossing_mode_exit",  # ★ 路口模式退出消息类型
                "text": "路口模式已结束"
            }
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
            print(f"[{datetime.now()}] 发送路口模式退出通知给助手")
        except Exception as e:
            print(f"[{datetime.now()}] 路口模式退出通知发送失败: {e}")
            self.connected = False

    async def close(self):
        """关闭服务端"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self.ws:
            await self.ws.close()
            self.connected = False


class DetectionServer:
    def __init__(self, assistant_server: AssistantServer):
        self.assistant = assistant_server
        self.frame_count = 0
        self.start_time = time.time()
        self.last_fps_time = time.time()
        self.fps = 0
        self.custom_time = 0  # 自定义模型推理耗时(ms)
        self.official_time = 0  # 官方模型推理耗时(ms)
        self.traffic_light_time = 0  # 红绿灯模型推理耗时(ms)
        self._last_frame_time = 0.0  # 上一帧时间戳（用于帧率自适应）
        self._last_frame = None  # 最近一帧图像（用于 OCR 场景文字识别）

        # --- 状态持久化：启动时加载上次状态 ---
        _load_state()
        print(f"[{datetime.now()}] 状态持久化已初始化，保存间隔: {CONFIG['STATE_SAVE_INTERVAL']}秒")

        # 预览线程：独立线程绘制预览窗口，不阻塞主 WebSocket 循环
        self._preview_queue = []  # 待绘制的帧数据
        # ★ 推理线程池：三模型并行推理，大幅减少总推理时间
        self._infer_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="infer")
        self._preview_lock = threading.Lock()
        self._preview_running = True
        if SHOW_PREVIEW:
            self._preview_thread = threading.Thread(target=self._preview_loop, daemon=True)
            self._preview_thread.start()

    def _preview_loop(self):
        """预览线程主循环：从队列取帧并绘制"""
        while self._preview_running:
            annotated = None
            with self._preview_lock:
                if self._preview_queue:
                    annotated = self._preview_queue[-1]  # 只取最新帧
                    self._preview_queue.clear()

            if annotated is not None:
                try:
                    cv2.imshow("YOLOv8 Dual Model Detection", annotated)
                    cv2.waitKey(1)
                except Exception:
                    pass
            else:
                time.sleep(0.01)  # 没帧时休眠，降低 CPU 占用

    def _submit_preview(self, annotated_frame):
        """提交帧到预览队列（非阻塞）"""
        if not self._preview_running:
            return
        with self._preview_lock:
            self._preview_queue.append(annotated_frame)
            # 只保留最新 2 帧，防止队列堆积
            if len(self._preview_queue) > 2:
                self._preview_queue = self._preview_queue[-2:]

    def draw_mask(self, img, mask, color, alpha=0.4):
        """在图像上绘制分割掩码（简化版，去掉高斯模糊加速）"""
        if mask is None:
            return img

        # 创建彩色掩码
        color_mask = np.zeros_like(img)
        color_mask[mask > 0] = color

        # 直接混合到原图（去掉 GaussianBlur，节省 ~20ms/帧）
        result = img.copy()
        mask_bool = mask > 0
        result[mask_bool] = cv2.addWeighted(
            img[mask_bool], 1 - alpha,
            color_mask[mask_bool], alpha, 0
        )

        return result

    async def _send_scene_text_result(self, websocket, texts: list):
        """发送 OCR 场景文字识别结果给 App"""
        if not texts:
            speak_text = "未识别到文字，请调整摄像头角度重试"
            success = False
        else:
            speak_parts = [t["text"] for t in texts[:CONFIG["SCENE_TEXT_MAX_LINES"]]]
            speak_text = "，".join(speak_parts)
            success = True

        response = {
            "type": "scene_text_result",
            "success": success,
            "speak_text": speak_text,
            "texts": texts
        }
        try:
            await websocket.send(json.dumps(response, ensure_ascii=False))
            print(f"[{datetime.now()}] 场景文字识别结果: {speak_text[:50]}")
        except Exception as e:
            print(f"[{datetime.now()}] 场景文字结果发送失败: {e}")

    async def handle_client(self, websocket):
        """处理单个客户端连接"""
        client_addr = websocket.remote_address
        print(f"[{datetime.now()}] 新连接: {client_addr}")

        try:
            while True:
                # ★ 取最新帧：清空 WebSocket 缓冲区中的旧帧，只处理最新的一帧
                # 这样无论 App 发帧多快，视觉 AI 永远只处理最新画面，延迟不会累积
                message = None
                while True:
                    try:
                        # 非阻塞接收：timeout=0.001（1毫秒）快速清空缓冲区
                        # ★ 不能用 timeout=0，那会立即超时，永远收不到帧
                        msg = await asyncio.wait_for(websocket.recv(), timeout=0.001)
                        message = msg
                    except asyncio.TimeoutError:
                        break  # 缓冲区已空，使用最后收到的帧
                    except websockets.exceptions.ConnectionClosed:
                        raise  # 连接断开，往外抛

                if message is None:
                    # 没有新帧，短暂休眠避免空转
                    await asyncio.sleep(0.005)
                    continue

                # ============ 判断消息类型：二进制图像 或 JSON 命令 ============
                if isinstance(message, str):
                    # JSON 命令（如 OCR 请求）
                    try:
                        cmd = json.loads(message)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    # ★ 兼容 "type" 和 "command" 两种字段名
                    cmd_type = cmd.get("type") or cmd.get("command", "")
                    if cmd_type == "scene_text_request":
                        print(f"[{datetime.now()}] 收到场景文字识别请求")
                        if self._last_frame is not None:
                            ocr_texts = _run_scene_text_ocr(self._last_frame)
                            await self._send_scene_text_result(websocket, ocr_texts)
                        else:
                            await websocket.send(json.dumps({
                                "type": "scene_text_result",
                                "success": False,
                                "speak_text": "暂无画面",
                                "texts": []
                            }, ensure_ascii=False))
                    elif cmd_type == "start_find_mode":
                        # ★ 找店模式：小助手发来的，目标文字在 target 字段，搜索词在 keywords 字段
                        target = cmd.get("target", "").strip()
                        keywords = cmd.get("keywords", "").strip()
                        if target:
                            _start_find_mode(target, websocket, self.assistant, keywords)
                            await websocket.send(json.dumps({
                                "type": "find_mode_started",
                                "target": target,
                            }, ensure_ascii=False))
                        else:
                            await websocket.send(json.dumps({
                                "type": "find_result",
                                "success": False,
                                "text": "未指定要找的目标",
                            }, ensure_ascii=False))
                    elif cmd_type == "stop_find_mode":
                        _stop_find_mode()
                    continue

                # 解码图像
                try:
                    nparr = np.frombuffer(message, np.uint8)
                    frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

                    if frame is None:
                        print(f"[{datetime.now()}] 警告: 无法解码图像")
                        continue

                    # 保存当前帧，供 OCR 场景文字识别使用
                    self._last_frame = frame.copy()

                    # ★ 找店模式：每帧检查是否需要OCR扫描
                    _check_find_mode(frame)

                except Exception as e:
                    print(f"[{datetime.now()}] 图像解码错误: {e}")
                    continue

                # ★ 找店模式下暂停 YOLO 推理，避免和 OCR 抢 GPU 资源
                # 但仍然显示预览（避免 Windows 报"未响应"）
                if _find_mode_active:
                    try:
                        # 简单预览：只显示原始帧 + "找店中" 提示
                        annotated = frame.copy()
                        screen_w, screen_h = _get_screen_size()
                        preview_h, preview_w = annotated.shape[:2]
                        scale_h = screen_h / preview_h if preview_h > screen_h else 1.0
                        scale_w = screen_w / preview_w if preview_w > screen_w else 1.0
                        scale = min(scale_h, scale_w, 1.0)
                        if scale < 1.0:
                            annotated = cv2.resize(annotated, (int(preview_w * scale), int(preview_h * scale)))
                        cv2.putText(annotated, f"Finding: {_find_target}", (10, 30),
                                   cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                        self._submit_preview(annotated)
                    except Exception as e:
                        print(f"[{datetime.now()}] 找店模式预览错误: {e}")
                    continue

                # ★★★ 整体异常保护：推理→构建响应→发送 全流程 ★★★
                # 任何一步出错都不会断开连接，只跳过当前帧
                try:
                    # 双模型检测
                    detections = []
                    result_custom = None
                    result_official = None

                    # 记录原始帧尺寸（用于坐标还原）
                    orig_h, orig_w = frame.shape[:2]

                    # 缩放图像加速推理
                    infer_frame = frame
                    scale_x, scale_y = 1.0, 1.0
                    if orig_w > INFER_WIDTH or orig_h > INFER_HEIGHT:
                        infer_frame = cv2.resize(frame, (INFER_WIDTH, INFER_HEIGHT))
                        scale_x = orig_w / INFER_WIDTH
                        scale_y = orig_h / INFER_HEIGHT

                    # 检测画面是否太暗（纯黑/镜头盖住）
                    frame_too_dark = _is_frame_too_dark(frame)

                    # ★ 三模型并行推理（ThreadPoolExecutor）
                    # 原逻辑：custom→official→traffic_light 串行执行，总耗时=三者之和
                    # 新逻辑：三模型同时执行，总耗时≈最慢的那个模型
                    def _run_custom():
                        t0 = time.time()
                        results = model_custom(infer_frame, conf=CONFIDENCE, verbose=False)
                        elapsed = (time.time() - t0) * 1000
                        return results, elapsed

                    def _run_official():
                        t1 = time.time()
                        results = model_official(infer_frame, conf=CONFIDENCE, verbose=False)
                        elapsed = (time.time() - t1) * 1000
                        return results, elapsed

                    def _run_traffic_light():
                        t3 = time.time()
                        results = model_traffic_light(infer_frame, conf=0.4, verbose=False)
                        elapsed = (time.time() - t3) * 1000
                        return results, elapsed

                    # 提交自定义模型和官方模型（始终运行）
                    futures = {}
                    try:
                        futures["custom"] = self._infer_executor.submit(_run_custom)
                    except Exception as e:
                        print(f"[{datetime.now()}] 提交自定义模型失败: {e}")

                    try:
                        futures["official"] = self._infer_executor.submit(_run_official)
                    except Exception as e:
                        print(f"[{datetime.now()}] 提交官方模型失败: {e}")

                    # 红绿灯模型：根据条件决定是否运行
                    _should_check_traffic_light = False
                    _has_traffic_light_box = False
                    _has_zebra_now = False
                    _has_straight_curb = False

                    # 先获取自定义模型结果（用于判断是否需要红绿灯模型）
                    result_custom = None
                    result_official = None

                    # 等待自定义模型和官方模型完成
                    for key in ["custom", "official"]:
                        if key in futures:
                            try:
                                results, elapsed = futures[key].result(timeout=5.0)
                                if key == "custom":
                                    result_custom = results[0]
                                    self.custom_time = elapsed
                                else:
                                    result_official = results[0]
                                    self.official_time = elapsed
                            except Exception as e:
                                print(f"[{datetime.now()}] {key}模型推理失败: {e}")

                    # 构建自定义模型检测结果
                    if result_custom is not None and result_custom.boxes is not None and len(result_custom.boxes) > 0:
                        for i, box in enumerate(result_custom.boxes):
                            class_id = int(box.cls)
                            class_name = result_custom.names[class_id]
                            class_en = CUSTOM_CLASS_NAMES_EN.get(class_id, class_name)
                            x1, y1, x2, y2 = box.xyxy[0].tolist()
                            bbox = [x1 * scale_x, y1 * scale_y, x2 * scale_x, y2 * scale_y]
                            det = {
                                "class": class_name,
                                "class_cn": class_name,
                                "class_en": class_en,
                                "confidence": float(box.conf),
                                "bbox": bbox,
                                "source": "custom"
                            }
                            if result_custom.masks is not None and len(result_custom.masks) > i:
                                det["has_mask"] = True
                            detections.append(det)

                    # 构建官方模型检测结果
                    if result_official is not None and result_official.boxes is not None and len(result_official.boxes) > 0:
                        for i, box in enumerate(result_official.boxes):
                            class_id = int(box.cls)
                            if class_id not in ALLOWED_CLASSES:
                                continue
                            class_name = result_official.names[class_id]
                            class_cn = OFFICIAL_CLASS_NAMES_CN.get(class_id, class_name)
                            x1, y1, x2, y2 = box.xyxy[0].tolist()
                            bbox = [x1 * scale_x, y1 * scale_y, x2 * scale_x, y2 * scale_y]
                            det = {
                                "class": class_name,
                                "class_cn": class_cn,
                                "confidence": float(box.conf),
                                "bbox": bbox,
                                "source": "official"
                            }
                            detections.append(det)

                    # 判断是否需要运行红绿灯模型
                    if model_traffic_light is not None:
                        _has_traffic_light_box = any(
                            (d.get("class_cn") or d.get("class", "")) == "红绿灯"
                            for d in detections if d.get("source") == "official"
                        )
                        _has_zebra_now = any(
                            (d.get("class_cn") or d.get("class", "")) == "斑马线"
                            for d in detections if d.get("source") == "custom"
                        )
                        _loop_curb_count = sum(
                            1 for d in detections
                            if d.get("source") == "custom"
                            and (d.get("class_cn") or d.get("class", "")) in {"直行盲道", "straight_curb"}
                        )
                        _has_straight_curb = _loop_curb_count > 0

                        self.traffic_light_time = 0
                        _in_zebra_guide = _zebra_guide_phase in ("on_zebra", "crossing")
                        _should_check_traffic_light = (
                            _has_traffic_light_box
                            or IN_INTERSECTION_MODE
                            or _in_zebra_guide
                        ) and not _crossing_green_passed

                        if _should_check_traffic_light:
                            try:
                                results_tl, tl_elapsed = self._infer_executor.submit(_run_traffic_light).result(timeout=5.0)
                                result_traffic_light = results_tl[0]
                                self.traffic_light_time = tl_elapsed

                                # ★ 调试：打印红绿灯模型原始输出
                                if hasattr(result_traffic_light, 'boxes') and result_traffic_light.boxes is not None:
                                    box_count = len(result_traffic_light.boxes)
                                    if box_count > 0:
                                        print(f"[{datetime.now()}] [红绿灯调试] 检测到 {box_count} 个框")
                                        for i, box in enumerate(result_traffic_light.boxes):
                                            try:
                                                cls_val = box.cls
                                                if hasattr(cls_val, 'item'):
                                                    cid = int(cls_val.item())
                                                elif hasattr(cls_val, '__iter__'):
                                                    cid = int(cls_val[0])
                                                else:
                                                    cid = int(cls_val)
                                                conf_val = box.conf
                                                if hasattr(conf_val, 'item'):
                                                    c = float(conf_val.item())
                                                elif hasattr(conf_val, '__iter__'):
                                                    c = float(conf_val[0])
                                                else:
                                                    c = float(conf_val)
                                                print(f"[{datetime.now()}] [红绿灯调试] 框{i}: class={cid}, conf={c:.3f}, name={TRAFFIC_LIGHT_CLASSES_CN.get(cid, '未知')}")
                                            except Exception as debug_e:
                                                print(f"[{datetime.now()}] [红绿灯调试] 框{i}: 读取失败 {debug_e}")
                                    else:
                                        print(f"[{datetime.now()}] [红绿灯调试] 无检测框 (boxes=None)")

                                if result_traffic_light.boxes is not None and len(result_traffic_light.boxes) > 0:
                                    for box in result_traffic_light.boxes:
                                        try:
                                            cls_val = box.cls
                                            if hasattr(cls_val, 'item'):
                                                class_id = int(cls_val.item())
                                            elif hasattr(cls_val, '__iter__'):
                                                class_id = int(cls_val[0])
                                            else:
                                                class_id = int(cls_val)
                                        except Exception as cls_e:
                                            print(f"[{datetime.now()}] 获取红绿灯class_id错误: {cls_e}, cls={box.cls}")
                                            continue

                                        if class_id not in TRAFFIC_LIGHT_COLOR_CLASSES:
                                            continue

                                        class_name = TRAFFIC_LIGHT_CLASSES_CN.get(class_id, f"tl_{class_id}")

                                        try:
                                            xyxy = box.xyxy
                                            if hasattr(xyxy, 'cpu'):
                                                xyxy = xyxy.cpu().numpy()
                                            if hasattr(xyxy, 'flatten'):
                                                coords = xyxy.flatten().tolist()
                                            else:
                                                coords = list(xyxy)
                                            if len(coords) >= 4:
                                                x1, y1, x2, y2 = coords[:4]
                                                bbox = [x1 * scale_x, y1 * scale_y, x2 * scale_x, y2 * scale_y]
                                                det = {
                                                    "class": class_name,
                                                    "class_cn": class_name,
                                                    "confidence": float(box.conf),
                                                    "bbox": bbox,
                                                    "source": "traffic_light"
                                                }
                                                detections.append(det)
                                        except Exception as e:
                                            print(f"[{datetime.now()}] 红绿灯bbox处理错误: {e}")
                            except Exception as e:
                                print(f"[{datetime.now()}] 红绿灯模型检测错误: {e}")

                    # 计算 FPS
                    self.frame_count += 1
                    current_time = time.time()
                    if current_time - self.last_fps_time >= 1.0:
                        self.fps = self.frame_count / (current_time - self.last_fps_time)
                        self.frame_count = 0
                        self.last_fps_time = current_time
                        tl_time = getattr(self, 'traffic_light_time', 0) or 0
                        print(f"[{datetime.now()}] FPS: {self.fps:.1f} | Custom: {self.custom_time:.1f}ms | Official: {self.official_time:.1f}ms | TL: {tl_time:.1f}ms")

                    # ============ 帧率自适应更新 ============
                    if hasattr(self, "_last_frame_time") and self._last_frame_time > 0:
                        interval = time.time() - self._last_frame_time
                        _update_adaptive_fps(interval)
                    self._last_frame_time = time.time()

                    # --- 定时保存状态（每30秒一次，不阻塞主循环）---
                    _try_periodic_save()

                    # ============ 生成朗读文案（增强版）============
                    # 暗帧跳过播报（防止纯黑/太暗画面产生幻觉误报）
                    current_ts = time.time()
                    speak_text = ""
                    mode = get_detection_mode()

                    if mode == "off":
                        # 关闭模式：不识别不播报，但仍返回检测结果给 App
                        pass
                    elif not frame_too_dark:
                        try:
                            speak_text, event_fp = build_speak_text(
                                detections,
                                frame_width=orig_w,
                                frame_height=orig_h,
                                current_time=current_ts,
                                assistant=self.assistant
                            )
                        except Exception as e:
                            print(f"[{datetime.now()}] build_speak_text 异常: {e}")
                            speak_text, event_fp = "", ""

                        # once 模式：识别一次后自动切回 off
                        if mode == "once" and speak_text:
                            set_detection_mode("off")
                    else:
                        # 暗帧清空事件历史，防止积累幻觉数据
                        _event_history.clear()
                        _curb_history.clear()

                    # ============ 发送障碍物警告给小助手（冷却+指纹去重）============
                    # 只有非 off 模式才发送
                    if speak_text and mode != "off":
                        await self.assistant.send_obstacle_warning(
                            speak_text, frame_width=orig_w, event_fingerprint=event_fp
                        )

                    # ★ 发送"未检测到红绿灯"消息（绕过冷却，确保一定能发送）
                    global _pending_no_light_msg, _pending_on_zebra_msg, _pending_green_pass_msg
                    if _pending_no_light_msg and mode != "off":
                        _pending_no_light_msg = False
                        await self.assistant.send_obstacle_warning(
                            "未检测到红绿灯，请自行观察后通行",
                            frame_width=orig_w,
                            event_fingerprint="no_light_detected",
                            force=True
                        )
                    # ★ 发送"站在斑马线上"消息（绕过冷却，确保一定能发送）
                    if _pending_on_zebra_msg and mode != "off":
                        _pending_on_zebra_msg = False
                        await self.assistant.send_obstacle_warning(
                            "您已站在斑马线上，现在抬起手机帮您看红绿灯",
                            frame_width=orig_w,
                            event_fingerprint="on_zebra",
                            force=True
                        )
                    # ★ 发送"绿灯可通行"消息（单独发送，不和障碍物合并）
                    if _pending_green_pass_msg and mode != "off":
                        _pending_green_pass_msg = False
                        await self.assistant.send_obstacle_warning(
                            "现在是绿灯，可以通行",
                            frame_width=orig_w,
                            event_fingerprint="traffic_light:green_pass",
                            force=True
                        )

                    # 构建响应
                    response = {
                        "success": True,
                        "speak_text": speak_text,  # 小助手直接朗读此字段
                        "models": {
                            "custom": {
                                "type": "segment" if custom_type == 'segment' else "detect",
                                "loaded": True,
                                "path": MODEL_CUSTOM_PATH
                            },
                            "official": {
                                "type": "detect",
                                "loaded": True,
                                "path": OFFICIAL_MODEL_PATH,
                                "allowed_classes": list(ALLOWED_CLASSES)
                            },
                            "traffic_light": {
                                "type": "detect",
                                "loaded": model_traffic_light is not None,
                                "path": TRAFFIC_LIGHT_MODEL_PATH,
                                "classes": TRAFFIC_LIGHT_CLASSES_CN
                            }
                        },
                        "detections": detections,
                        "fps": round(self.fps, 1),
                        "timing": {
                            "custom_ms": round(self.custom_time, 1),
                            "official_ms": round(self.official_time, 1),
                            "traffic_light_ms": round(getattr(self, 'traffic_light_time', 0) or 0, 1)
                        },
                        "traffic_light_state": {
                            "in_intersection_mode": IN_INTERSECTION_MODE,
                            "current_state": _traffic_light_state
                        },
                        "timestamp": datetime.now().isoformat()
                    }

                    # 发送检测结果
                    await websocket.send(json.dumps(response, ensure_ascii=False))

                    # 本地预览（异步提交到线程池，不阻塞主循环）
                    if SHOW_PREVIEW:
                        try:
                            # ★ 捕获当前帧所需的局部变量，在线程中安全使用
                            _preview_frame = frame.copy()
                            _preview_custom = result_custom
                            _preview_official = result_official
                            _preview_detections = list(detections)
                            _preview_fps = self.fps
                            _preview_custom_time = self.custom_time
                            _preview_official_time = self.official_time
                            _preview_scale_x = scale_x
                            _preview_scale_y = scale_y
                            _preview_orig_w = orig_w
                            _preview_orig_h = orig_h

                            def _draw_preview():
                                try:
                                    annotated = _preview_frame

                                    # ★ 自适应屏幕预览：获取屏幕尺寸，等比缩放
                                    preview_h, preview_w = annotated.shape[:2]
                                    screen_w, screen_h = _get_screen_size()
                                    # 等比缩放，确保不超出屏幕
                                    scale_h = screen_h / preview_h if preview_h > screen_h else 1.0
                                    scale_w = screen_w / preview_w if preview_w > screen_w else 1.0
                                    scale = min(scale_h, scale_w, 1.0)  # 不放大，只缩小
                                    if scale < 1.0:
                                        annotated = cv2.resize(annotated, (int(preview_w * scale), int(preview_h * scale)))

                                    # ===== 绘制自定义模型结果（分割掩码 + 边界框）=====
                                    if _preview_custom is not None:
                                        # 绘制分割掩码
                                        if _preview_custom.masks is not None and len(_preview_custom.masks) > 0:
                                            for mask in _preview_custom.masks:
                                                mask_data = mask.data[0].cpu().numpy()
                                                mask_resized = cv2.resize(mask_data, (annotated.shape[1], annotated.shape[0]))
                                                annotated = self.draw_mask(annotated, mask_resized, COLOR_CUSTOM, MASK_ALPHA)

                                        # 绘制边界框和标签
                                        if _preview_custom.boxes is not None and len(_preview_custom.boxes) > 0:
                                            for box in _preview_custom.boxes:
                                                x1, y1, x2, y2 = map(int, box.xyxy[0])
                                                # ★ 坐标还原：推理缩放坐标 → 预览缩放坐标
                                                x1 = int(x1 * _preview_scale_x * (annotated.shape[1] / _preview_orig_w))
                                                y1 = int(y1 * _preview_scale_y * (annotated.shape[0] / _preview_orig_h))
                                                x2 = int(x2 * _preview_scale_x * (annotated.shape[1] / _preview_orig_w))
                                                y2 = int(y2 * _preview_scale_y * (annotated.shape[0] / _preview_orig_h))
                                                class_id = int(box.cls)
                                                class_en = CUSTOM_CLASS_NAMES_EN.get(class_id, f"class_{class_id}")
                                                conf = float(box.conf)

                                                cv2.rectangle(annotated, (x1, y1), (x2, y2), COLOR_CUSTOM, 2)
                                                label = f"{class_en} {conf:.2f}"
                                                cv2.rectangle(annotated, (x1, y1 - 25), (x1 + 160, y1), COLOR_CUSTOM, -1)
                                                cv2.putText(annotated, label, (x1 + 5, y1 - 6),
                                                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

                                    # ===== 绘制官方模型结果（边界框）=====
                                    if _preview_official is not None:
                                        if _preview_official.boxes is not None and len(_preview_official.boxes) > 0:
                                            for box in _preview_official.boxes:
                                                class_id = int(box.cls)
                                                if class_id not in ALLOWED_CLASSES:
                                                    continue

                                                x1, y1, x2, y2 = map(int, box.xyxy[0])
                                                # ★ 坐标还原：推理缩放坐标 → 预览缩放坐标
                                                x1 = int(x1 * _preview_scale_x * (annotated.shape[1] / _preview_orig_w))
                                                y1 = int(y1 * _preview_scale_y * (annotated.shape[0] / _preview_orig_h))
                                                x2 = int(x2 * _preview_scale_x * (annotated.shape[1] / _preview_orig_w))
                                                y2 = int(y2 * _preview_scale_y * (annotated.shape[0] / _preview_orig_h))
                                                class_name = OFFICIAL_CLASS_NAMES_EN.get(class_id, f"class_{class_id}")
                                                conf = float(box.conf)

                                                cv2.rectangle(annotated, (x1, y1), (x2, y2), COLOR_OFFICIAL, 2)
                                                label = f"{class_name} {conf:.2f}"
                                                cv2.rectangle(annotated, (x1, y1 - 25), (x1 + 130, y1), COLOR_OFFICIAL, -1)
                                                cv2.putText(annotated, label, (x1 + 5, y1 - 6),
                                                           cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)

                                    # ===== 绘制信息面板 =====
                                    info_y = 30
                                    cv2.putText(annotated, f"FPS: {_preview_fps:.1f}",
                                               (10, info_y), cv2.FONT_HERSHEY_SIMPLEX,
                                               0.8, (0, 255, 0), 2)
                                    info_y += 30
                                    cv2.putText(annotated, f"Custom: {_preview_custom_time:.1f}ms (GREEN)",
                                               (10, info_y), cv2.FONT_HERSHEY_SIMPLEX,
                                               0.6, COLOR_CUSTOM, 2)
                                    info_y += 25
                                    cv2.putText(annotated, f"Official: {_preview_official_time:.1f}ms (BLUE)",
                                               (10, info_y), cv2.FONT_HERSHEY_SIMPLEX,
                                               0.6, COLOR_OFFICIAL, 2)

                                    custom_count = sum(1 for d in _preview_detections if d.get("source") == "custom")
                                    official_count = sum(1 for d in _preview_detections if d.get("source") == "official")
                                    info_y += 30
                                    cv2.putText(annotated, f"curb: {custom_count}  person/vehicle: {official_count}",
                                               (10, info_y), cv2.FONT_HERSHEY_SIMPLEX,
                                               0.55, (255, 255, 255), 2)

                                    # 提交到预览线程（非阻塞）
                                    self._submit_preview(annotated)
                                except Exception as draw_err:
                                    print(f"[{datetime.now()}] 预览绘制错误: {draw_err}")

                            # ★ 异步提交绘制任务到线程池，不阻塞主循环
                            self._infer_executor.submit(_draw_preview)

                        except Exception as preview_err:
                            print(f"[{datetime.now()}] 预览错误: {preview_err}")

                except Exception as frame_err:
                    # ★ 整体异常保护：任何帧处理错误都不会断开连接
                    import traceback
                    print(f"[{datetime.now()}] ⚠️ 帧处理异常（已跳过）: {type(frame_err).__name__}: {frame_err}")
                    # 只在非预期异常时打印堆栈（常见异常如 CUDA OOM 打印详情）
                    if "CUDA" in str(frame_err) or "OutOfMemory" in str(frame_err) or "RuntimeError" in str(frame_err):
                        traceback.print_exc()

        except websockets.exceptions.ConnectionClosed:
            print(f"[{datetime.now()}] 连接关闭: {client_addr}")
        except Exception as e:
            print(f"[{datetime.now()}] 连接错误: {e}")

        finally:
            # --- 连接断开时保存最终状态 ---
            _save_state()
            print(f"[{datetime.now()}] 连接已断开，状态已保存")

        if SHOW_PREVIEW:
            self._preview_running = False
            cv2.destroyAllWindows()


async def main():
    # 启动小助手 WebSocket 服务端（监听 8768，等待小助手连接）
    assistant = AssistantServer()
    await assistant.start()

    server = DetectionServer(assistant)

    while True:
        try:
            print(f"[{datetime.now()}] 🚀 启动 WebSocket 服务器...")
            async with websockets.serve(
                server.handle_client, HOST, PORT,
                max_size=2**20,        # ★ 1MB：单帧JPEG上限（640x480质量40约30-50KB，留足余量）
                ping_interval=20,      # ★ 20秒心跳（默认20秒，显式设置）
                ping_timeout=10,       # ★ 10秒ping超时
                write_limit=2**20,     # ★ 1MB写缓冲上限
                compression=None,     # ★ 不压缩（JPEG已经是压缩格式，再压缩反而增加延迟）
            ):
                print(f"[{datetime.now()}] ✅ 服务器运行中，等待连接...")
                await asyncio.Future()  # 永不返回
        except KeyboardInterrupt:
            print(f"\n[{datetime.now()}] 用户手动停止，退出")
            break
        except Exception as e:
            import traceback
            print(f"[{datetime.now()}] ❌ 服务器异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            print(f"[{datetime.now()}] 🔄 10秒后自动重启...")
            await asyncio.sleep(10)
        finally:
            await assistant.close()


if __name__ == "__main__":
    # ★ PaddleOCR 改为懒加载：启动时不加载，找店时才加载
    # 避免 PaddleOCR 占用 GPU 显存导致 YOLO 推理变慢
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print(f"\n[{datetime.now()}] 服务器已停止")
            break
        except Exception as e:
            print(f"\n[{datetime.now()}] 服务器异常崩溃: {e}")
            print(f"[{datetime.now()}] 5秒后自动重启...")
            import time
            time.sleep(5)
