"""
明途小助手 AI 服务端（中央协调节点）

架构：
  App ←→ 小助手 AI（本服务端，端口 8766 + 8767）— 唯一的播报媒介
  App ──→ GPS AI（端口 5000，HTTP REST）— App 只上传，不接收返回
                ↕ 视觉 AI（端口 8768，WebSocket）

数据流：
  导航中：App →(GPS+指南针, 3秒)→ 小助手 AI（WebSocket）→(转发)→ GPS AI（WebSocket）
          GPS AI →(导航指令)→ 小助手 AI（WebSocket）→(排队播报)→ App
  非导航：App →(GPS数据)→ 小助手 AI（逆地理编码获取城市）
          App →(语音指令)→ 小助手 AI → DeepSeek / 高德 → 回复

导航流程：
  1. App 唤醒时发送 gps_update 给小助手 AI（逆地理编码获取城市）
  2. 用户说"我要去蜜雪冰城" → 小助手 AI 调用高德 POI 搜索 → 让用户确认
  3. 用户确认 → 小助手 AI 调用 GPS AI POST /api/navigation/start
  4. 小助手 AI 通知 App navigation_started → App 开始每 3 秒上传 GPS 给 GPS AI
  5. GPS AI 收到位置更新 → 计算导航指令 → 主动推送给小助手 AI
  6. 小助手 AI 排队播报导航指令给 App（统一优先级管理）
  7. 到达目的地 → GPS AI 通知小助手 AI → 小助手 AI 通知 App 导航结束

端口：
  8766 - App WebSocket（App ↔ 小助手 AI）
  8767 - GPS AI WebSocket（GPS AI → 小助手 AI，接收导航指令推送）

职责：
  1. 接收 App 语音指令，调用 DeepSeek 生成回复
  2. 接收 App GPS 数据，逆地理编码获取城市信息
  3. 导航意图检测 → POI 搜索 → 用户确认 → 调用 GPS AI 开始导航
  4. 接收 GPS AI 推送的导航指令，排队播报（统一优先级管理）
  5. 接收视觉 AI 的障碍物检测结果
  6. 多路消息优先级判断：障碍物(高) > 导航(中) > 闲聊(低)
"""

import asyncio
import json
import time
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

import httpx
import websockets

# ============================================================
#  日志
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mingtu")


# ============================================================
#  配置
# ============================================================

class Config:
    # 本服务端口
    ASSISTANT_PORT: int = 8766          # App WebSocket（App ↔ 小助手 AI）
    GPS_AI_WS_PORT: int = 8767          # GPS AI WebSocket（GPS AI → 小助手 AI）

    # GPS AI REST API 地址（Flask 服务，同一台机器）
    GPS_AI_BASE_URL: str = "http://127.0.0.1:5000"

    # 视觉 AI WebSocket 地址
    VISION_AI_URL: str = "ws://127.0.0.1:8768"

    # DeepSeek API
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1/chat/completions"
    DEEPSEEK_MODEL: str = "deepseek-chat"
    DEEPSEEK_TIMEOUT: float = 60.0

    # 高德地图 API Key（与 GPS AI 共用同一个 key）
    AMAP_API_KEY: str = ""

    # 高德地图 API 地址
    AMAP_REGEO_URL: str = "https://restapi.amap.com/v3/geocode/regeo"
    AMAP_POI_SEARCH_URL: str = "https://restapi.amap.com/v3/place/text"
    AMAP_POI_AROUND_URL: str = "https://restapi.amap.com/v3/place/around"
    AMAP_WEATHER_URL: str = "https://restapi.amap.com/v3/weather/weatherInfo"

    # 会话历史最大轮数
    MAX_HISTORY_TURNS: int = 10

    # 心跳间隔（秒）
    HEARTBEAT_INTERVAL: int = 30

    # 重连间隔（秒）
    RECONNECT_INTERVAL: int = 5

    # 语音播报冷却时间（秒），防止消息轰炸
    # ★ 优化：盲人用户需要更多时间消化语音信息，6秒冷却
    VOICE_COOLDOWN: float = 6.0

    # GPS 数据过期时间（秒），超过此时间认为 GPS 数据过期
    GPS_DATA_EXPIRE: float = 300

    # POI 搜索半径（米）
    POI_SEARCH_RADIUS: int = 5000

    SYSTEM_PROMPT: str = """你是明途一位温暖贴心的视障人士导航小助手你叫小途用户可以随时叫你你就像一个值得信赖的朋友陪在身边

重要规则：
1. 你已经接通了导航系统可以帮用户导航到目的地
2. 当用户说要去某个地方时直接回复好的然后等待系统搜索结果不要追问细节
3. 不要问用户哪个校区哪个门之类的问题系统会自动搜索附近地点让用户选择
4. 天气查询已开通系统会自动获取当前位置天气你不需要回答天气问题
5. 回复要简洁明了因为会通过语音播报给用户每次回复控制在25字以内
6. 不要使用任何标点符号或表情符号
7. 语气要温暖自然像朋友聊天一样亲切不要机械
8. 如果用户遇到危险优先提醒安全
9. 不要重复用户说过的话
10. 用户说谢谢或辛苦了要自然回应不要机械
11. 绝对不要编造或假设任何检测结果障碍物信息或导航指令这些由专门系统负责你只负责聊天和导航搜索

{location_context}

示例：
用户：导航到山西大学
你：好的正在帮你搜索山西大学

用户：我要去蜜雪冰城
你：好的帮你找蜜雪冰城

用户：今天天气怎么样
你：已经帮你查好天气了

用户：谢谢
你：不客气随时都在

用户：你好
你：你好呀有什么可以帮你的
"""


# ============================================================
#  消息优先级
# ============================================================

class Priority(IntEnum):
    """消息优先级，数值越大越优先"""
    LOW = 0       # 闲聊 / AI 回复
    MEDIUM = 1    # 导航提示
    HIGH = 2      # 障碍物 / 安全警告
    URGENT = 3    # 紧急危险


@dataclass
class QueuedMessage:
    """排队等待发送给 App 的消息"""
    text: str
    priority: Priority
    timestamp: float = field(default_factory=time.time)
    source: str = ""  # 来源标识：deepseek / gps / vision / system
    expect_reply: bool = False  # 是否需要用户回复（播报完后自动进入聆听）


# ============================================================
#  会话管理
# ============================================================

class ClientSession:
    """单个 App 客户端的会话"""

    def __init__(self, websocket, session_id: str):
        self.websocket = websocket
        self.session_id = session_id
        self.history: list[dict] = []
        self.connected_at: float = time.time()
        self.last_active: float = time.time()
        self._user_speaking_until: float = 0  # 用户说话中，此时间之前暂停视觉播报
        self._detection_mode_before_speak: str = ""  # 用户说话前的检测模式（用于恢复）
        self._is_continuous_detecting: bool = False  # 当前是否处于持续检测模式
        # 待发送消息队列（用 list + Condition 替代 asyncio.Queue，支持遍历清空）
        self._outbox_list: list[QueuedMessage] = []
        self._outbox_event = asyncio.Event()
        self._outbox_lock = asyncio.Lock()
        # 上次发送消息的时间（用于冷却控制）
        self.last_send_time: float = 0.0

        # ---- GPS 相关 ----
        self.gps_longitude: Optional[float] = None
        self.gps_latitude: Optional[float] = None
        self.gps_heading: Optional[float] = None
        self.gps_accuracy: float = 0.0
        self.gps_city: str = ""
        self.gps_district: str = ""
        self.gps_address: str = ""
        self.gps_update_time: float = 0.0  # 上次 GPS 更新时间

        # ---- 导航状态 ----
        self.is_navigating: bool = False
        self.pending_confirmation: bool = False
        self.pending_destination: str = ""
        self.pending_poi_list: list[dict] = field(default_factory=list)  # POI 候选列表
        self._gps_poll_task: Optional[asyncio.Task] = None

        # 导航指令去重
        self._last_nav_instruction: str = ""
        self._last_nav_instruction_time: float = 0.0
        self._nav_initial_distance: float = 0.0  # 导航开始时的总距离（米）
        self._nav_last_remaining: float = 0.0  # 上次剩余距离（米）

        # ★ 斑马线引导状态（导航联动）
        self._nav_turn_direction: str = ""  # 当前接近的转弯方向（"左转"/"右转"/"左前方"/"右前方"）
        self._nav_turn_notified: bool = False  # 是否已通知视觉AI进入引导模式

    def update_gps(self, longitude: float, latitude: float, heading: Optional[float] = None, accuracy: float = 0.0):
        """更新 GPS 数据"""
        self.gps_longitude = longitude
        self.gps_latitude = latitude
        self.gps_heading = heading
        self.gps_accuracy = accuracy
        self.gps_update_time = time.time()
        log.info(
            "[%s] GPS 更新: (%.6f, %.6f) heading=%s accuracy=%.0fm",
            self.session_id, longitude, latitude,
            f"{heading:.0f}°" if heading is not None else "None",
            accuracy
        )

    def is_gps_valid(self) -> bool:
        """检查 GPS 数据是否有效且未过期"""
        if self.gps_longitude is None or self.gps_latitude is None:
            return False
        if time.time() - self.gps_update_time > Config.GPS_DATA_EXPIRE:
            return False
        return True

    def get_location_context(self) -> str:
        """生成位置上下文信息，注入到 DeepSeek system prompt"""
        if not self.is_gps_valid():
            return "当前位置信息未知请先确认用户所在城市"
        parts = []
        if self.gps_city:
            parts.append(f"城市{self.gps_city}")
        if self.gps_district:
            parts.append(f"区域{self.gps_district}")
        if self.gps_address:
            parts.append(f"详细地址{self.gps_address}")
        if parts:
            return "用户当前位置信息 " + " ".join(parts) + " 请优先在附近搜索目的地"
        return ""

    def add_history(self, role: str, content: str):
        self.history.append({"role": role, "content": content})
        if len(self.history) > Config.MAX_HISTORY_TURNS * 2:
            self.history = self.history[-(Config.MAX_HISTORY_TURNS * 2):]

    def touch(self):
        self.last_active = time.time()

    def reset_navigation_state(self):
        """重置导航相关状态（不重置 is_navigating，由调用方控制）"""
        self.pending_confirmation = False
        self.pending_destination = ""
        self.pending_poi_list = []
        # ★ 新增：停止 GPS 轮询任务
        if hasattr(self, '_gps_poll_task') and self._gps_poll_task and not self._gps_poll_task.done():
            self._gps_poll_task.cancel()
        self._gps_poll_task = None

    def full_reset_navigation(self):
        """完全重置导航状态（包括 is_navigating）"""
        self.is_navigating = False
        self.reset_navigation_state()


# ============================================================
#  DeepSeek LLM 客户端
# ============================================================

class DeepSeekClient:
    """DeepSeek API 调用封装"""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=Config.DEEPSEEK_TIMEOUT)

    async def classify_intent(self, user_text: str) -> str:
        """
        轻量级意图分类（max_tokens=20，速度快成本低）
        返回意图标签：detect_once / detect_start / detect_stop / intersection_mode / find_store_mode / navigate / navigate_stop / weather / chat
        """
        try:
            resp = await self.client.post(
                Config.DEEPSEEK_BASE_URL,
                headers={
                    "Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": Config.DEEPSEEK_MODEL,
                    "messages": [
                        {"role": "system", "content": (
                            "你是一个意图分类器，只回复一个标签，不要回复其他任何内容\n"
                            "标签列表：\n"
                            "detect_once - 用户想识别/检测/看看前方有什么物体（一次）\n"
                            "detect_start - 用户想开始/开启持续识别检测（不包括路口模式和找店模式）\n"
                            "detect_stop - 用户想停止/关闭识别检测\n"
                            "intersection_mode - 用户想开启路口模式/过马路模式（如\"开启路口模式\"\"过马路模式\"\"路口检测\"）\n"
                            "find_store_mode - 用户明确要开启找店模式（如\"开启找店模式\"\"找店模式\"\"打开找店\"）注意只有明确说开启找店模式才用这个标签\n"
                            "navigate - 用户想去某个地方/导航到某处/出发去某处（如\"导航到XX\"\"我要去XX\"\"去XX\"\"到XX\"\"前往XX\"\"带我到XX\"\"回XX\"）\n"
                            "navigate_stop - 用户想停止导航/取消导航/结束导航\n"
                            "weather - 用户问天气/气温/下雨等\n"
                            "chat - 普通聊天/问候/其他所有情况\n"
                            "★ 重要区分规则：\n"
                            "- 用户说\"帮我找XX在哪\"\"找一下XX\"\"XX在哪里\"等，属于普通聊天（chat），不是 find_store_mode 也不是 navigate\n"
                            "- find_store_mode 只用于明确说\"开启找店模式\"\"找店模式\"的情况\n"
                            "- navigate 只用于\"导航到XX\"\"我要去XX\"\"去XX\"等明确要去某地的表达\n"
                            "- \"帮我找瑞幸咖啡在哪\"是 chat，\"导航到瑞幸咖啡\"是 navigate，\"开启找店模式\"是 find_store_mode"
                        )},
                        {"role": "user", "content": user_text}
                    ],
                    "temperature": 0.0,
                    "max_tokens": 20,
                },
            )
            if resp.status_code != 200:
                return "chat"
            label = resp.json()["choices"][0]["message"]["content"].strip().lower()
            # 容错：只取第一个词
            label = label.split()[0] if label else "chat"
            # 映射到合法标签
            valid = {"detect_once", "detect_start", "detect_stop", "intersection_mode", "find_store_mode", "navigate", "navigate_stop", "weather", "chat"}
            return label if label in valid else "chat"
        except Exception as e:
            log.warning("意图分类失败: %s, fallback to chat", e)
            return "chat"

    async def chat(self, user_text: str, history: list[dict],
                   location_context: str = "") -> str:
        try:
            system_prompt = Config.SYSTEM_PROMPT.format(
                location_context=location_context
            )
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(history)
            messages.append({"role": "user", "content": user_text})

            resp = await self.client.post(
                Config.DEEPSEEK_BASE_URL,
                headers={
                    "Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": Config.DEEPSEEK_MODEL,
                    "messages": messages,
                    "temperature": 0.5,
                    "max_tokens": 150,
                },
            )

            if resp.status_code != 200:
                log.error("DeepSeek HTTP %s: %s", resp.status_code, resp.text[:200])
                return "抱歉我暂时无法理解请再说一次"

            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

        except httpx.TimeoutException:
            log.error("DeepSeek 请求超时")
            return "抱歉网络超时请再说一次"
        except Exception as e:
            log.error("DeepSeek 调用异常: %s", e)
            return "抱歉网络异常请再说一次"

    async def close(self):
        await self.client.aclose()


# ============================================================
#  高德地图服务（逆地理编码 + POI 搜索）
# ============================================================

class AmapClient:
    """高德地图 API 客户端（逆地理编码 + POI 搜索）"""

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15.0)

    async def reverse_geocode(self, longitude: float, latitude: float) -> dict:
        """
        逆地理编码：经纬度 → 地址信息
        返回: {"city": "太原市", "district": "小店区", "address": "...", "formatted_address": "..."}
        """
        try:
            resp = await self.client.get(
                Config.AMAP_REGEO_URL,
                params={
                    "key": Config.AMAP_API_KEY,
                    "location": f"{longitude},{latitude}",
                    "extensions": "base",
                    "output": "JSON",
                },
            )
            data = resp.json()
            if data.get("status") != "1":
                log.warning("[高德] 逆地理编码失败: %s", data.get("info", ""))
                return {}

            regeo = data.get("regeocode", {})
            address_component = regeo.get("addressComponent", {})

            result = {
                "city": address_component.get("city", "") or address_component.get("province", ""),
                "district": address_component.get("district", ""),
                "address": address_component.get("township", ""),
                "formatted_address": regeo.get("formatted_address", ""),
            }
            log.info("[高德] 逆地理编码: %s", result)
            return result

        except Exception as e:
            log.error("[高德] 逆地理编码异常: %s", e)
            return {}

    async def _deepseek_correct_keyword(self, keyword: str) -> str:
        """
        ★ 用 DeepSeek 对搜索关键词进行语义纠错
        解决语音识别错误导致搜不到 POI 的问题
        例如："桃源路站" → "桃园路站"，"许东路" → "徐东路"
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    Config.DEEPSEEK_BASE_URL,
                    headers={
                        "Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": Config.DEEPSEEK_MODEL,
                        "messages": [
                            {"role": "system", "content": (
                                "你是一个地名纠错助手。用户通过语音搜索地点，但语音识别可能有错误。\n"
                                "请纠正地名中的错别字，返回正确的地名。\n"
                                "如果地名没有错误，原样返回。\n"
                                "只返回纠正后的地名，不要任何解释。\n"
                            )},
                            {"role": "user", "content": keyword}
                        ],
                        "temperature": 0.0,
                        "max_tokens": 30,
                    },
                )
                if resp.status_code == 200:
                    corrected = resp.json()["choices"][0]["message"]["content"].strip().rstrip("。？！!?，,、")
                    if corrected and len(corrected) >= 2:
                        log.info("[地名纠错] '%s' → '%s'", keyword, corrected)
                        return corrected
        except Exception as e:
            log.warning("[地名纠错] 失败: %s，使用原始关键词", e)
        return keyword  # 纠错失败，返回原始关键词

    async def search_nearby_poi(self, keyword: str, longitude: float,
                                 latitude: float, radius: int = 5000,
                                 city: str = "") -> list[dict]:
        """
        搜索 POI（优先关键词搜索，过滤不相关结果）
        策略：DeepSeek语义纠错 → 高德搜索 → DeepSeek智能筛选
        """
        try:
            # ★ 第零步：DeepSeek 语义纠错（"桃源路站"→"桃园路站"）
            keyword = await self._deepseek_correct_keyword(keyword)
            if keyword:
                log.info("[POI搜索] DeepSeek纠错后关键词: %s", keyword)

            # ★ 修复：优先用 place/around 周边搜索（以用户坐标为中心）
            # place/text 按城市搜索经常漏掉附近的结果（如搜"华莱士"只返回1个）
            # place/around 以坐标为中心搜索，结果更全面
            all_pois = []

            # 第一步：周边搜索（以用户坐标为中心，半径5公里）
            resp = await self.client.get(
                Config.AMAP_POI_AROUND_URL,
                params={
                    "key": Config.AMAP_API_KEY,
                    "keywords": keyword,
                    "location": f"{longitude},{latitude}",
                    "radius": str(radius),
                    "offset": "25",
                    "page": "1",
                    "extensions": "base",
                    "output": "JSON",
                },
            )
            data = resp.json()
            if data.get("status") == "1":
                all_pois.extend(data.get("pois", []))
                log.info("[高德] 周边搜索 '%s' 返回 %d 个原始结果", keyword, len(all_pois))

            # 第二步：如果周边搜索结果太少，补充 place/text 城市搜索
            if len(all_pois) < 5 and city:
                search_city = city
                resp2 = await self.client.get(
                    Config.AMAP_POI_SEARCH_URL,
                    params={
                        "key": Config.AMAP_API_KEY,
                        "keywords": keyword,
                        "city": search_city,
                        "citylimit": "true",
                        "offset": "25",
                        "page": "1",
                        "extensions": "base",
                        "output": "JSON",
                    },
                )
                data2 = resp2.json()
                if data2.get("status") == "1":
                    new_pois = data2.get("pois", [])
                    # 去重（按名称+坐标）
                    existing = set((p.get("name", ""), p.get("location", "")) for p in all_pois)
                    for p in new_pois:
                        key = (p.get("name", ""), p.get("location", ""))
                        if key not in existing:
                            all_pois.append(p)
                            existing.add(key)
                    log.info("[高德] 城市搜索补充 %d 个结果，总计 %d 个", len(new_pois), len(all_pois))

            if not all_pois:
                log.info("[高德] POI 搜索 '%s' 无结果", keyword)
                return []

            # ★ 第三步：用 DeepSeek 智能筛选，让 AI 理解用户意图
            # 把原始 POI 列表发给 DeepSeek，让它选出最匹配的结果
            filtered = await self._deepseek_filter_poi(all_pois, keyword, longitude, latitude)

            # 只返回半径内的结果，最多5个
            results = []
            for poi in filtered:
                dist = int(poi.get("distance", "99999"))
                if dist <= radius:
                    poi_location = poi.get("location", "")
                    lon_lat = poi_location.split(",") if poi_location else []
                    results.append({
                        "name": poi.get("name", ""),
                        "address": poi.get("address", ""),
                        "distance": poi.get("distance", ""),
                        "type": poi.get("type", ""),
                        "longitude": lon_lat[0] if len(lon_lat) >= 2 else "",
                        "latitude": lon_lat[1] if len(lon_lat) >= 2 else "",
                    })

            log.info("[高德] POI 搜索 '%s' 找到 %d 个结果（DeepSeek过滤后）", keyword, len(results))
            return results[:5]

        except Exception as e:
            log.error("[高德] POI 搜索异常: %s", e)
            return []

    async def _deepseek_filter_poi(self, pois: list[dict], keyword: str,
                                    longitude: float, latitude: float) -> list[dict]:
        """
        ★ V2：DeepSeek 智能筛选 POI
        让 DeepSeek 理解用户意图，从原始列表中选出最匹配的结果。
        返回 JSON 格式的名称列表，按名称从原始数据中匹配提取。

        优势：不再依赖硬编码排除规则，DeepSeek 能理解自然语言语义。
        比如"山西大学"→ 选校区/大门；"山西大学周围的台球厅"→ 选台球厅。
        """
        import json

        # 基础去重
        candidates = []
        seen = set()
        for poi in pois:
            name = poi.get("name", "")
            location = poi.get("location", "")
            if not name or not location:
                continue
            key = (name, location)
            if key in seen:
                continue
            seen.add(key)

            try:
                poi_lon = float(location.split(",")[0])
                poi_lat = float(location.split(",")[1])
                dist = self._haversine(longitude, latitude, poi_lon, poi_lat)
            except (ValueError, IndexError):
                dist = 99999

            candidates.append({
                "name": name,
                "distance": int(dist),
                "type": poi.get("type", ""),
                "poi": poi,
            })

        if not candidates:
            return []

        # 构造候选列表
        poi_list_text = "\n".join(
            f"{i+1}. {c['name']}（{c['distance']}米，类型：{c['type']}）"
            for i, c in enumerate(candidates)
        )

        prompt = f"""用户说想去"{keyword}"，以下是附近搜索到的地点。

请根据用户的搜索意图，选出最可能想去的地方。你是一个智能助手，需要理解用户真正想去的到底是什么。

思考方式：
- 如果用户搜"山西大学"，他大概率想去的是校区主体（如"山西大学坞城校区"）或校门（如"山西大学南门"），而不是某个内部学院或停车场
- 如果用户搜"台球厅"，他想去的就是台球厅，不是学校
- 如果用户搜"国际教育交流学院"，那他就真的想去国际教育交流学院
- 简单来说：理解用户的真实意图，选出最匹配的

最多选5个，按相关度从高到低排序。

地点列表：
{poi_list_text}

请严格按以下JSON格式返回，不要任何其他文字：
{{"selected":["名称1","名称2","名称3"]}}

如果列表中确实没有相关的，返回：{{"selected":[]}}"""

        try:
            resp = await self.client.post(
                Config.DEEPSEEK_BASE_URL,
                headers={
                    "Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": Config.DEEPSEEK_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
            )

            if resp.status_code != 200:
                log.warning("[POI筛选] DeepSeek 请求失败 HTTP %s，使用简单匹配", resp.status_code)
                return self._simple_name_filter(candidates, keyword)

            data = resp.json()
            reply = data["choices"][0]["message"]["content"].strip()
            log.info("[POI筛选] DeepSeek 回复: %s", reply)

            # 解析 JSON
            # DeepSeek 可能在 JSON 前后加 markdown 代码块，需要清理
            reply_clean = reply.strip()
            if reply_clean.startswith("```"):
                reply_clean = reply_clean.split("\n", 1)[1] if "\n" in reply_clean else reply_clean[3:]
            if reply_clean.endswith("```"):
                reply_clean = reply_clean[:-3]
            reply_clean = reply_clean.strip()

            try:
                result = json.loads(reply_clean)
                selected_names = result.get("selected", [])
            except json.JSONDecodeError:
                log.warning("[POI筛选] JSON解析失败，使用简单匹配")
                return self._simple_name_filter(candidates, keyword)

            if not selected_names:
                log.warning("[POI筛选] DeepSeek 返回空列表，使用简单匹配")
                return self._simple_name_filter(candidates, keyword)

            # 按名称从候选中匹配提取
            results = []
            for sel_name in selected_names[:5]:
                for c in candidates:
                    if c["name"] == sel_name:
                        c["poi"]["distance"] = str(c["distance"])
                        results.append(c["poi"])
                        break

            if not results:
                log.warning("[POI筛选] 名称匹配失败，使用简单匹配")
                return self._simple_name_filter(candidates, keyword)

            log.info("[POI筛选] 从 %d 个候选中选出 %d 个: %s",
                     len(candidates), len(results),
                     ", ".join(r.get("name", "") for r in results))
            return results

        except Exception as e:
            log.error("[POI筛选] 异常: %s，使用简单匹配", e)
            return self._simple_name_filter(candidates, keyword)

    def _simple_name_filter(self, candidates: list[dict], keyword: str) -> list[dict]:
        """
        最终 fallback：按名称包含关键词排序，只做最基本的去重
        """
        results = []
        for c in candidates:
            name = c["name"]
            c["poi"]["distance"] = str(c["distance"])
            results.append(c["poi"])

        # 名称包含关键词的排前面，然后按距离
        results.sort(key=lambda x: (
            0 if keyword in x.get("name", "") else 1,
            int(x.get("distance", "99999"))
        ))
        return results[:5]

    def _filter_poi_results(self, pois: list[dict], keyword: str) -> list[dict]:
        """
        过滤 POI 搜索结果，去掉不相关的内部设施和子建筑
        ★ 优化：名称精准匹配优先，过滤无关类型（快递、协会、社团等）
        """
        # 不想要的名称关键词（子建筑、内部设施、无关类型）
        exclude_keywords = [
            # 内部设施
            "家属区", "家属院", "宿舍", "食堂", "停车场",
            "内部", "办公楼", "公寓", "小区",
            # 子院系/研究所
            "学院", "系", "研究所", "研究院", "实验室",
            # 具体建筑
            "图书馆", "体育馆", "游泳馆", "医院",
            # 商业无关
            "超市", "便利店", "银行", "邮局",
            # 快递/驿站/协会/社团等无关类型
            "菜鸟驿站", "快递", "驿站", "快递柜",
            "协会", "学会", "社团", "联合会", "工会",
            "服务中心", "服务站", "服务点",
            "健身操", "舞蹈", "合唱", "乐队",
            "后勤", "保卫", "物业", "维修",
            # ★ 新增：社区/文体/广场等非目标地点
            "社区", "文体广场", "文化广场", "活动中心",
            "学术交流中心",  # 内部设施，不是学校主体
        ]

        # ★ 蹭地名排除：名称中包含关键词但只是蹭地名的商业场所
        # 不管商业后缀在名称的什么位置，只要出现就排除
        # "远洋酒店(太原南站山西大学店)" → 含"酒店" → 排除
        # "山西大学学术交流中心登崇阁酒店" → 含"酒店" → 排除
        # "蜜雪冰城(山西大学店)" → 含括号+关键词 → 排除
        exclude_commercial_keywords = [
            "酒店", "宾馆", "旅馆", "招待所", "民宿",
            "网吧", "KTV", "酒吧", "烧烤", "火锅", "奶茶", "咖啡",
            "理发", "美容", "足浴", "洗浴", "台球",
            "登崇阁",  # 具体酒店品牌名
        ]

        # 不想要的类型编码（高德 POI type）
        exclude_types = [
            "141201",  # 科教文化场所-科研机构
            "141202",  # 科教文化场所-文化团体
            "141203",  # 科教文化场所-科技馆
            "141204",  # 科教文化场所-图书馆
            "141205",  # 科教文化场所-展览馆
            "141206",  # 科教文化场所-博物馆
            "141207",  # 科教文化场所-会展中心
            "141208",  # 科教文化场所-美术馆
            "141209",  # 科教文化场所-文化活动中心
            "150100",  # 商务住宅-住宅区
            "150200",  # 商务住宅-商务办公楼
            "150300",  # 商务住宅-产业园区
        ]

        filtered = []
        for poi in pois:
            name = poi.get("name", "")
            poi_type = poi.get("type", "")
            type_code = poi_type.split(";")[0] if poi_type else ""

            # 排除不想要的名称
            excluded = False
            for kw in exclude_keywords:
                if kw in name:
                    excluded = True
                    break
            if excluded:
                continue

            # 排除类型编码不匹配的
            if type_code in exclude_types:
                continue

            # ★ 蹭地名检测：名称中包含商业关键词，直接排除
            # 不管关键词在名称的什么位置，只要出现就排除
            # "远洋酒店(太原南站山西大学店)" → 含"酒店" → 排除
            # "山西大学学术交流中心登崇阁酒店" → 含"酒店"+"学术交流中心" → 排除
            # "蜜雪冰城(山西大学店)" → 含括号+关键词 → 排除
            import re
            is_piggyback = False
            # 检查1：名称中直接包含商业关键词（酒店/宾馆/KTV等）
            for ck in exclude_commercial_keywords:
                if ck in name:
                    is_piggyback = True
                    break
            # 检查2：关键词在括号内（如"蜜雪冰城(山西大学店)"）
            if not is_piggyback:
                if re.search(r'[（(].*' + re.escape(keyword) + r'.*[）)]', name):
                    is_piggyback = True
            if is_piggyback:
                continue

            # ★ 核心改动：名称精准匹配优先
            # 完全包含关键词的优先级最高
            if keyword in name:
                poi["_name_match"] = 0  # 精准匹配
                filtered.append(poi)
                continue

            # 部分匹配：去掉"大学""学院"等后缀再匹配
            keyword_parts = keyword.replace("大学", "").replace("学院", "").replace("学校", "")
            if len(keyword_parts) >= 2 and keyword_parts in name:
                poi["_name_match"] = 1  # 部分匹配
                filtered.append(poi)
                continue

            # 类型为学校相关，也保留但优先级低
            if type_code.startswith("1412") or "学校" in poi_type or "校区" in name:
                poi["_name_match"] = 2  # 类型匹配
                filtered.append(poi)
                continue

        return filtered

    @staticmethod
    def _haversine(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
        """计算两点之间的距离（米）"""
        import math
        R = 6371000.0
        lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
        d_lat = math.radians(lat2 - lat1)
        d_lon = math.radians(lon2 - lon1)
        a = (math.sin(d_lat / 2) ** 2 +
             math.cos(lat1_r) * math.cos(lat2_r) * math.sin(d_lon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c

    async def get_weather(self, city: str) -> str:
        """
        查询天气（高德天气 API）
        返回: 天气描述文本，如"太原市今天晴，气温12到25度"
        """
        try:
            # 高德天气 API 需要城市 adcode，先用城市名查 adcode
            resp = await self.client.get(
                "https://restapi.amap.com/v3/config/district",
                params={
                    "key": Config.AMAP_API_KEY,
                    "keywords": city,
                    "subdistrict": "0",
                    "output": "JSON",
                },
            )
            data = resp.json()
            if data.get("status") != "1" or not data.get("districts"):
                return f"抱歉无法获取{city}的天气信息"

            adcode = data["districts"][0].get("adcode", "")
            if not adcode:
                return f"抱歉无法获取{city}的天气信息"

            # 查询实时天气
            resp2 = await self.client.get(
                Config.AMAP_WEATHER_URL,
                params={
                    "key": Config.AMAP_API_KEY,
                    "city": adcode,
                    "extensions": "base",
                    "output": "JSON",
                },
            )
            data2 = resp2.json()
            if data2.get("status") != "1" or not data2.get("lives"):
                return f"抱歉无法获取{city}的天气信息"

            live = data2["lives"][0]
            weather = live.get("weather", "")
            temperature = live.get("temperature", "")
            wind_direction = live.get("winddirection", "")
            wind_power = live.get("windpower", "")
            humidity = live.get("humidity", "")
            report_time = live.get("reporttime", "")

            result = f"{city}今天{weather}气温{temperature}度"
            if wind_power and wind_power != "≤3":
                result += f"{wind_direction}风{wind_power}级"
            if humidity:
                result += f"湿度{humidity}%"
            log.info("[高德] 天气查询: %s", result)
            return result

        except Exception as e:
            log.error("[高德] 天气查询异常: %s", e)
            return "抱歉天气查询失败请稍后再试"

    async def close(self):
        await self.client.aclose()


# ============================================================
#  GPS AI REST API 客户端
# ============================================================

class GpsAiClient:
    """GPS AI 的 HTTP REST 客户端"""

    def __init__(self):
        self.base_url = Config.GPS_AI_BASE_URL
        self.client = httpx.AsyncClient(timeout=30.0)

    async def start_navigation(self, origin_lon: float, origin_lat: float,
                                destination: str) -> Optional[dict]:
        """
        调用 GPS AI 开始导航
        POST /api/navigation/start
        请求体: {"origin": {"longitude": ..., "latitude": ...}, "destination": "..."}
        """
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/navigation/start",
                json={
                    "origin": {
                        "longitude": origin_lon,
                        "latitude": origin_lat,
                    },
                    "destination": destination,
                },
            )
            data = resp.json()

            if data.get("success"):
                log.info("[GPS AI] 导航启动成功: %s", destination)
                return data.get("data", {})
            else:
                log.error("[GPS AI] 导航启动失败: %s", data.get("error", ""))
                return None

        except Exception as e:
            log.error("[GPS AI] 调用异常: %s", e)
            return None

    async def update_location(self, longitude: float, latitude: float,
                              heading: Optional[float] = None) -> Optional[dict]:
        """
        调用 GPS AI 更新用户位置，获取导航指令
        POST /api/navigation/update
        请求体: {"longitude": ..., "latitude": ..., "heading": ...}
        返回: {"current_instruction": "...", "remaining_distance": "...", "arrived": bool}
        """
        try:
            body = {
                "longitude": longitude,
                "latitude": latitude,
            }
            if heading is not None:
                body["heading"] = heading

            resp = await self.client.post(
                f"{self.base_url}/api/navigation/update",
                json=body,
            )
            data = resp.json()

            if data.get("success"):
                return data.get("data", {})
            else:
                error = data.get("error", "")
                # "当前没有进行中的导航" 不算错误，静默处理
                if "没有进行中的导航" not in error:
                    log.warning("[GPS AI] 位置更新失败: %s", error)
                return None

        except Exception as e:
            log.error("[GPS AI] 位置更新异常: %s", e)
            return None

    async def stop_navigation(self) -> bool:
        """调用 GPS AI 停止导航"""
        try:
            resp = await self.client.post(
                f"{self.base_url}/api/navigation/stop",
            )
            data = resp.json()
            return data.get("success", False)
        except Exception as e:
            log.error("[GPS AI] 停止导航异常: %s", e)
            return False

    async def get_navigation_status(self) -> Optional[dict]:
        """获取 GPS AI 导航状态"""
        try:
            resp = await self.client.get(
                f"{self.base_url}/api/navigation/status",
            )
            data = resp.json()
            if data.get("success"):
                return data.get("data", {})
            return None
        except Exception as e:
            log.error("[GPS AI] 获取状态异常: %s", e)
            return None

    async def get_current_instruction(self) -> Optional[str]:
        """
        获取 GPS AI 当前导航指令（不更新位置，只获取当前应该播报的指令）
        GET /api/navigation/instruction
        """
        try:
            resp = await self.client.get(
                f"{self.base_url}/api/navigation/instruction",
            )
            data = resp.json()
            if data.get("success"):
                return data.get("data", {}).get("instruction", "")
            return None
        except Exception as e:
            log.error("[GPS AI] 获取当前指令异常: %s", e)
            return None

    async def close(self):
        await self.client.aclose()


# ============================================================
#  视觉 AI WebSocket 客户端（保持不变）
# ============================================================

class VisionAIClient:
    """与视觉 AI 的 WebSocket 长连接客户端"""

    def __init__(self, url: str):
        self.name = "视觉 AI"
        self.url = url
        self.ws = None
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self.on_message: Optional[callable] = None
        self.on_crossing_exit: Optional[callable] = None  # ★ 路口模式退出回调

    async def start(self):
        """启动后台连接（自动重连）"""
        self._running = True
        self._reconnect_task = asyncio.create_task(self._connect_loop())
        log.info("%s 客户端已启动，目标: %s", self.name, self.url)

    async def stop(self):
        self._running = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self.ws:
            await self.ws.close()
        log.info("%s 客户端已停止", self.name)

    async def _connect_loop(self):
        """自动重连循环"""
        while self._running:
            try:
                ws = await websockets.connect(self.url)
                self.ws = ws
                log.info("%s 已连接", self.name)

                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        # ★ 转发特殊消息给 App（路口模式退出等）
                        msg_type = data.get("type", "")
                        if msg_type == "crossing_mode_exit":
                            # ★ 路口模式退出通知：转发给所有 App
                            if self.on_crossing_exit:
                                asyncio.create_task(self.on_crossing_exit())
                        if self.on_message:
                            await self.on_message(data)
                    except json.JSONDecodeError:
                        log.warning("%s 收到非 JSON 消息: %s", self.name, raw[:100])

            except Exception as e:
                self.ws = None
                if self._running:
                    log.warning("%s 连接断开: %s，%d 秒后重连",
                                self.name, e, Config.RECONNECT_INTERVAL)
                    await asyncio.sleep(Config.RECONNECT_INTERVAL)

    @property
    def is_connected(self) -> bool:
        try:
            return self.ws is not None
        except Exception:
            return False

    async def send_command(self, command: str, **kwargs):
        """发送命令给视觉 AI（如切换识别模式）"""
        if not self.is_connected:
            log.warning("%s 未连接，无法发送命令", self.name)
            return False
        try:
            msg = {"command": command, **kwargs}
            await self.ws.send(json.dumps(msg, ensure_ascii=False))
            log.info("%s → 发送命令: %s", self.name, msg)
            return True
        except Exception as e:
            log.error("%s 发送命令失败: %s", self.name, e)
            return False


# ============================================================
#  中央协调器
# ============================================================

class AssistantCoordinator:
    """
    中央协调器：管理所有连接，路由消息，判断优先级
    """

    def __init__(self):
        self.llm = DeepSeekClient()
        self.amap = AmapClient()
        self.gps_ai = GpsAiClient()
        self.vision_ai = VisionAIClient(Config.VISION_AI_URL)
        self.sessions: dict[str, ClientSession] = {}
        self._session_counter = 0
        self._gps_ai_ws: Optional[object] = None  # GPS AI 的 WebSocket 连接

    # ---------- 生命周期 ----------

    async def start(self):
        """启动协调器及所有 AI 服务连接"""
        self.vision_ai.on_message = self._on_vision_message
        self.vision_ai.on_crossing_exit = self._on_vision_crossing_exit  # ★ 路口模式退出回调
        await self.vision_ai.start()
        # ★ 问题19修复：启动健康检查任务
        self._health_check_task = asyncio.create_task(self._health_check_loop())
        log.info("中央协调器已启动")

    async def stop(self):
        if hasattr(self, '_health_check_task'):
            self._health_check_task.cancel()
        await self.vision_ai.stop()
        await self.gps_ai.close()
        await self.amap.close()
        await self.llm.close()
        log.info("中央协调器已停止")

    # ★ 路口模式退出处理
    async def _on_vision_crossing_exit(self):
        """视觉AI发送的路口模式退出通知：通知所有会话退出路口模式"""
        log.info("★ 收到视觉AI路口模式退出通知，通知所有会话")
        for session in list(self.sessions.values()):
            if getattr(session, '_in_crossing_mode', False):
                session._in_crossing_mode = False
                # 清空队列中的视觉消息（QueuedMessage 是 dataclass，用属性访问）
                async with session._outbox_lock:
                    session._outbox_list = [
                        msg for msg in session._outbox_list
                        if getattr(msg, 'source', '') not in ("vision", "vision_app")
                    ]

    # ★ 问题19修复：定期健康检查任务
    async def _health_check_loop(self):
        """定期检查 vision_ai 和 gps_ai 的连接状态"""
        self._vision_disconnected_since = None  # 视觉AI断开时间
        self._gps_disconnected_since = None  # GPS AI断开时间
        while True:
            try:
                await asyncio.sleep(30)  # 每30秒检查一次
                for session in list(self.sessions.values()):
                    if not session.is_navigating:
                        continue
                    # 检查视觉AI连接
                    try:
                        if self.vision_ai and not self.vision_ai.is_connected:
                            if self._vision_disconnected_since is None:
                                self._vision_disconnected_since = time.time()
                            elif time.time() - self._vision_disconnected_since > 60:
                                await self._enqueue_message(
                                    session, "视觉检测已断开，请注意周围安全", Priority.HIGH, "system"
                                )
                                self._vision_disconnected_since = time.time()  # 重置，避免重复播报
                        else:
                            self._vision_disconnected_since = None
                    except Exception:
                        pass
                    # 检查GPS AI连接（HTTP客户端，通过请求检测）
                    try:
                        import httpx as _hc_httpx
                        _hc_client = _hc_httpx.AsyncClient(timeout=5.0)
                        try:
                            _hc_resp = await _hc_client.get(f"{Config.GPS_AI_BASE_URL}/api/navigation/status")
                            if _hc_resp.status_code != 200:
                                raise Exception(f"HTTP {_hc_resp.status_code}")
                            self._gps_disconnected_since = None
                        finally:
                            await _hc_client.aclose()
                    except Exception:
                        if self._gps_disconnected_since is None:
                            self._gps_disconnected_since = time.time()
                        elif time.time() - self._gps_disconnected_since > 30:
                            await self._enqueue_message(
                                session, "导航服务连接中断，正在尝试重连", Priority.HIGH, "system"
                            )
                            self._gps_disconnected_since = time.time()  # 重置，避免重复播报
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("健康检查异常: %s", e)
                await asyncio.sleep(30)

    # ---------- App 连接管理 ----------

    def _create_session(self, websocket) -> ClientSession:
        self._session_counter += 1
        sid = f"app_{self._session_counter}"
        session = ClientSession(websocket, sid)
        self.sessions[sid] = session
        log.info("新 App 连接: %s (来自 %s)", sid, websocket.remote_address)
        return session

    def _remove_session(self, session: ClientSession):
        self.sessions.pop(session.session_id, None)
        log.info("App 断开: %s", session.session_id)

    # ---------- App 消息处理 ----------

    async def handle_app_message(self, session: ClientSession, raw: str):
        """处理来自 App 的消息"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("JSON 解析失败: %s", raw[:100])
            return

        msg_type = data.get("type", "")
        text = data.get("text", "")

        log.info("[%s] 收到消息 type=%s, text=%s",
                 session.session_id, msg_type, str(text)[:100])

        if msg_type == "user_input" and text:
            await self._handle_user_input(session, text)

        elif msg_type == "user_confirm" and text:
            await self._handle_user_confirm(session, text)

        elif msg_type == "gps_update":
            # App 发送 GPS 数据（经纬度 + 朝向），用于逆地理编码和导航前确认
            await self._handle_gps_update(session, data)

        elif msg_type == "gps_status":
            # App 回复 GPS 状态（GPS 不可用时）
            available = data.get("available", False)
            if not available and session.pending_destination:
                log.info("[%s] App 回复 GPS 不可用，继续等待定位", session.session_id)

        elif msg_type == "navigation_stopped":
            # App 通知导航已停止（用户主动停止或到达目的地）
            await self._handle_navigation_stopped(session)

        elif msg_type == "vision_result":
            # App 直接转发视觉 AI 结果（备用通道）
            await self._enqueue_message(session, text, Priority.HIGH, "vision_app")

        elif msg_type == "crossing_mode_exit":
            # ★ 视觉AI通知路口模式退出：恢复GPS播报
            log.info("[%s] ★ 收到路口模式退出通知（视觉AI发送）", session.session_id)
            session._in_crossing_mode = False
            # ★ 清空队列中旧的视觉消息，避免干扰GPS播报（QueuedMessage 是 dataclass，用属性访问）
            async with session._outbox_lock:
                session._outbox_list = [
                    msg for msg in session._outbox_list
                    if getattr(msg, 'source', '') not in ("vision", "vision_app")
                ]

        elif msg_type == "clear_history":
            session.history.clear()
            session.full_reset_navigation()
            await self._enqueue_message(session, "好的，重新开始吧", Priority.LOW, "system")

        elif msg_type == "ping":
            await session.websocket.send(json.dumps({"type": "pong"}, ensure_ascii=False))

        else:
            log.warning("[%s] 未知消息类型: %s", session.session_id, msg_type)

    # ---------- GPS 数据处理 ----------

    async def _handle_gps_update(self, session: ClientSession, data: dict):
        """处理 App 发来的 GPS 更新"""
        try:
            longitude = float(data.get("longitude", 0))
            latitude = float(data.get("latitude", 0))
            heading = None
            if data.get("heading") is not None:
                heading = float(data["heading"]) % 360
            accuracy = float(data.get("accuracy", 0))

            if longitude == 0 and latitude == 0:
                log.warning("[%s] GPS 坐标为 (0,0)，忽略", session.session_id)
                return

            had_gps = session.is_gps_valid()
            session.update_gps(longitude, latitude, heading, accuracy)

            # ★ 新增：导航中，自动转发 GPS 数据给 GPS AI
            # 这样 App 不需要直连 GPS AI，所有通信都通过小助手 AI 中转
            if session.is_navigating:
                asyncio.create_task(self._forward_gps_to_gps_ai(session, longitude, latitude, heading, accuracy))

            # 逆地理编码获取城市信息（异步，不阻塞）
            asyncio.create_task(self._update_location_info(session))

            # 如果之前有导航意图但没 GPS，现在 GPS 到了，自动触发 POI 搜索
            if not had_gps and session.pending_destination and not session.pending_confirmation:
                destination = session.pending_destination
                log.info("[%s] GPS 到达，自动处理待导航目的地: %s", session.session_id, destination)
                # 等逆地理编码完成后再搜索（给一点时间）
                await asyncio.sleep(0.5)
                if session.is_gps_valid():
                    await self._handle_navigation_request(session, destination)

        except (TypeError, ValueError) as e:
            log.warning("[%s] GPS 数据格式错误: %s", session.session_id, e)

    async def _forward_gps_to_gps_ai(self, session: ClientSession, longitude: float,
                                       latitude: float, heading: float = None,
                                       accuracy: float = None):
        """
        ★ 修改：通过 WebSocket 转发 App 的 GPS 数据给 GPS AI
        导航中 App 每 1 秒发一次 gps_update 给小助手 AI，
        小助手 AI 通过 WebSocket 转发给 GPS AI，
        GPS AI 计算导航指令后通过同一个 WebSocket 推送回来。
        """
        try:
            if self._gps_ai_ws is None:
                log.warning("[%s] GPS AI WebSocket 未连接，无法转发 GPS 数据", session.session_id)
                return

            body = {
                "type": "gps_update",
                "longitude": longitude,
                "latitude": latitude,
            }
            if heading is not None:
                body["heading"] = heading
            if accuracy is not None:
                body["accuracy"] = accuracy

            await self._gps_ai_ws.send(json.dumps(body, ensure_ascii=False))
            log.info("[%s] GPS 数据已通过 WebSocket 转发给 GPS AI: (%.6f, %.6f) heading=%s",
                     session.session_id, longitude, latitude,
                     f"{heading:.0f}°" if heading is not None else "None")
        except Exception as e:
            log.warning("[%s] 转发 GPS 给 GPS AI 异常: %s", session.session_id, e)
            self._gps_ai_ws = None

    async def _gps_poll_loop(self, session: ClientSession):
        """
        ★ 新增：GPS 轮询循环
        导航中每 3 秒向 App 发送 request_gps_update，
        App 收到后回传 gps_update，小助手 AI 收到后转发给 GPS AI。
        这样 GPS 轮询完全由服务端驱动，不依赖 App 端的定时器。
        """
        log.info("[%s] GPS 轮询已启动（每 1 秒）", session.session_id)
        try:
            while session.is_navigating:
                try:
                    # 向 App 请求 GPS 数据
                    await session.websocket.send(
                        json.dumps({"type": "request_gps_update"}, ensure_ascii=False)
                    )
                except Exception:
                    break  # WebSocket 断开
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        log.info("[%s] GPS 轮询已停止", session.session_id)

    async def _pending_gps_poll(self, session: ClientSession, timeout: float = 30.0):
        """
        ★ 临时 GPS 轮询：导航意图已识别但 GPS 无效时，每2秒向 App 请求一次 GPS。
        直到 GPS 到达（pending_destination 被处理）或超时（30秒）。
        ★ 修复：超时后不放弃导航意图，保留 pending_destination，等用户下次说话时重试
        """
        log.info("[%s] 临时 GPS 轮询启动（超时 %.0f 秒）", session.session_id, timeout)
        start = time.time()
        try:
            while time.time() - start < timeout:
                await asyncio.sleep(2)
                # 如果 GPS 已到达、已开始导航、或 POI 列表已展示，停止轮询
                if not session.pending_destination or session.is_navigating or session.pending_confirmation:
                    log.info("[%s] 临时 GPS 轮询停止（GPS 已到达或已开始导航）", session.session_id)
                    return
                try:
                    await session.websocket.send(
                        json.dumps({"type": "request_gps_update"}, ensure_ascii=False)
                    )
                except Exception:
                    log.warning("[%s] 临时 GPS 轮询：发送请求失败，停止", session.session_id)
                    return
        except asyncio.CancelledError:
            pass
        # 超时
        if session.pending_destination and not session.is_navigating:
            log.warning("[%s] 临时 GPS 轮询超时（%.0f 秒），GPS 仍未到达", session.session_id, timeout)
            await self._enqueue_message(session, "定位超时，请确认位置信息已开启后重试", Priority.LOW, "system")
            # ★ 修复：不清空 pending_destination，保留导航意图
            # 用户下次发送GPS更新时，会自动继续处理导航
            # session.pending_destination = ""  # ← 删除这行

    async def _handle_gps_navigation_instruction(self, session: ClientSession, data: dict):
        """
        处理 GPS AI 直接推送的导航指令（通过 WebSocket 端口 8767）
        ★ 重构去重策略：
          1. 只保留简单的文本去重（完全相同的指令10秒内不重复）
          2. 方向变化≥45°时强制播报，不受去重限制
          3. 去掉"方向稳定性去重"——之前这个逻辑导致用户偏了90°也不提醒
        """
        instruction = data.get("instruction", "")
        remaining_distance = data.get("remaining_distance", "0")
        arrived = data.get("arrived", False)

        # ★ crossing_warning：GPS AI 推送的人行横道/路口警告
        # 不打断当前播报，但以 HIGH 优先级排到队列前面
        crossing_warning = data.get("crossing_warning")
        crossing_state = data.get("crossing_state", "")

        if crossing_state == "approaching":
            # ★ 进入路口模式：过滤GPS方向指引，通知视觉AI
            log.info("[%s] ★ 进入路口模式（crossing_state=approaching）", session.session_id)
            session._in_crossing_mode = True
            session._crossing_mode_enter_time = time.time()
            # 通知视觉AI进入斑马线引导模式
            await self.vision_ai.send_command("nav_turn_approaching", direction="过马路", remaining=0)
            # ★ 问题2修复：approaching时无论crossing_warning是否为空都必须播报
            await self._enqueue_message(
                session,
                crossing_warning or "前方需要过马路，请停下抬手机找斑马线",
                Priority.HIGH, "gps"
            )
        elif crossing_state == "crossed":
            # ★ 退出路口模式：恢复正常GPS频率
            log.info("[%s] ★ 退出路口模式（crossing_state=crossed）", session.session_id)
            session._in_crossing_mode = False
            await self.vision_ai.send_command("nav_turn_passed")
            if crossing_warning:
                await self._enqueue_message(session, crossing_warning, Priority.HIGH, "gps")
        elif crossing_state == "at_edge":
            # 到达路口边缘，不播报（视觉AI会引导）
            session._in_crossing_mode = True
        elif crossing_warning:
            log.info("[%s] 收到 crossing_warning: %s", session.session_id, crossing_warning)
            await self._enqueue_message(session, crossing_warning, Priority.HIGH, "gps")

        if arrived:
            # 到达目的地
            log.info("[%s] 用户已到达目的地（GPS AI 通知）", session.session_id)
            session.is_navigating = False
            # ★ 清除斑马线引导状态
            session._nav_turn_direction = ""
            session._nav_turn_notified = False
            # ★ 到达时清空队列，防止旧的导航指令还在排队播报
            async with session._outbox_lock:
                session._outbox_list.clear()
            await self.gps_ai.stop_navigation()
            await self._enqueue_message(
                session, "到啦，目的地就在你附近，导航结束了", Priority.MEDIUM, "gps"
            )
            # ★ 修复：到达时也通知 App 停止导航
            # 之前只发 TTS 消息，App 不知道导航已结束，isNavigating 仍为 true
            # 导致唤醒无法恢复，用户无法开始新导航
            try:
                await session.websocket.send(json.dumps({
                    "type": "navigation_stopped",
                    "text": "到啦，目的地就在你附近，导航结束了"
                }, ensure_ascii=False))
            except Exception:
                pass
        elif instruction:
            import re
            instruction_normalized = re.sub(r'\d+米', '', instruction)
            now = time.time()

            # ★ 方向变化检测：提取当前指令和上次指令的方向词，计算角度差
            dir_pattern = r'(向前方|向左前方|向右前方|向左转|向右转|向左后方|向右后方|向后转)'
            # 方向词到角度的映射
            dir_to_angle = {
                "向前方": 0, "向右前方": 45, "向右转": 90,
                "向右后方": 135, "向后转": 180, "向左后方": 225,
                "向左转": 270, "向左前方": 315,
            }
            current_dir = re.search(dir_pattern, instruction_normalized)
            last_dir = re.search(dir_pattern, session._last_nav_instruction) if session._last_nav_instruction else None

            force_broadcast = False
            if current_dir and last_dir:
                current_angle = dir_to_angle.get(current_dir.group(1))
                last_angle = dir_to_angle.get(last_dir.group(1))
                if current_angle is not None and last_angle is not None:
                    # 计算两个方向之间的最小角度差
                    angle_diff = abs(current_angle - last_angle)
                    if angle_diff > 180:
                        angle_diff = 360 - angle_diff
                    # ★ 方向变化≥45°，强制播报！这是安全底线
                    if angle_diff >= 45:
                        # ★ 防振荡：如果当前方向和上上次播报的方向相同，说明是 A→B→A 振荡
                        # 中值滤波滞后会导致方向突变时短暂回到旧方向，不应播报
                        prev_dir = getattr(session, '_prev_nav_direction', None)
                        if prev_dir and current_angle == prev_dir:
                            log.info("[%s] 方向振荡检测（%s→%s→%s），跳过",
                                     session.session_id,
                                     last_dir.group(1), current_dir.group(1),
                                     last_dir.group(1))
                        else:
                            force_broadcast = True
                            log.info("[%s] 方向变化%d°（%s→%s），强制播报",
                                     session.session_id, angle_diff,
                                     last_dir.group(1), current_dir.group(1))

            # ★ 简单文本去重：完全相同的指令10秒内不重复（但方向变化≥45°不受此限制）
            # ★ 米数变化超过20米时也强制推送
            meters_changed = False
            if session._last_nav_instruction:
                cur_m = re.search(r'步行(\d+)米', instruction)
                last_m = re.search(r'步行(\d+)米', session._last_nav_instruction_raw) if hasattr(session, '_last_nav_instruction_raw') else None
                if cur_m and last_m:
                    try:
                        if abs(int(cur_m.group(1)) - int(last_m.group(1))) >= 20:
                            meters_changed = True
                    except ValueError:
                        pass

            is_duplicate = (
                not force_broadcast and not meters_changed and
                instruction_normalized == session._last_nav_instruction and
                now - session._last_nav_instruction_time < 10.0
            )

            if is_duplicate:
                log.info("[%s] 指令去重，跳过: %s", session.session_id, instruction)
            else:
                # ★ 保存方向历史（用于防振荡检测）
                if current_dir:
                    last_angle_saved = dir_to_angle.get(last_dir.group(1)) if last_dir else None
                    if last_angle_saved is not None:
                        session._prev_nav_direction = last_angle_saved

                session._last_nav_instruction = instruction_normalized
                session._last_nav_instruction_time = now
                session._last_nav_instruction_raw = instruction  # ★ 保存原始指令（含米数）

                # ★ 计算已走距离：用初始总距离 - 当前剩余距离
                # ★ 修复：用第一次收到的 remaining_distance 作为真实初始距离
                # ★ 修复：当 remaining 比上次增加超过50米时（GPS精度突变），
                #   重置 initial distance，避免"已走170米"的误报
                try:
                    remaining = float(remaining_distance)
                    if remaining > 0:
                        # 第一次收到 remaining 时记录为真实初始距离
                        if session._nav_initial_distance <= 0:
                            session._nav_initial_distance = remaining
                            session._nav_last_remaining = remaining
                        # ★ GPS 精度突变检测：remaining 比上次增加超过50米
                        # 说明 GPS 从室内（accuracy=100m）走到室外（accuracy=8m），
                        # 坐标突然精确了，remaining 跳变。此时应重置初始距离。
                        elif (session._nav_last_remaining > 0 and
                              remaining > session._nav_last_remaining + 50):
                            log.info(f"[{session.session_id}] GPS精度突变: remaining {session._nav_last_remaining:.0f}m → {remaining:.0f}m，重置初始距离")
                            session._nav_initial_distance = remaining
                            session._nav_last_remaining = remaining
                        initial = session._nav_initial_distance
                        walked = int(initial - remaining)
                        # ★ 修复：已走距离小于10米时不播报，避免GPS漂移导致"已走1米"等无意义播报
                        # GPS精度在室内通常30-80米，微小漂移（1-9米）不代表真实移动
                        if walked >= 10:
                            instruction = f"已走{walked}米 {instruction}"
                    # 更新上次剩余距离
                    session._nav_last_remaining = remaining
                except (ValueError, TypeError):
                    pass

                log.info("[%s] 导航指令（GPS AI 推送）: %s (剩余 %s 米)",
                         session.session_id, instruction, remaining_distance)

                # ★ 斑马线引导联动：检测转弯接近，通知视觉AI引导用户走向斑马线
                await self._check_turn_zebra_guidance(session, instruction_normalized, remaining_distance)

                # ★ 用户说话中：GPS 消息也不入队，避免打断用户对话
                if time.time() < session._user_speaking_until:
                    log.info("[%s] 用户说话中，跳过 GPS 指令", session.session_id)
                # ★ 路口模式下：GPS方向指引降频到每10秒一次（避免干扰找斑马线）
                elif getattr(session, '_in_crossing_mode', False):
                    last_gps_in_crossing = getattr(session, '_last_gps_in_crossing_time', 0)
                    if now - last_gps_in_crossing < 10.0:
                        log.info("[%s] 路口模式，GPS方向指引降频跳过（距上次%.1f秒）",
                                 session.session_id, now - last_gps_in_crossing)
                    else:
                        session._last_gps_in_crossing_time = now
                        # ★ 方向变化强制播报使用独立source，避免被后续GPS消息替换
                        gps_source = "gps_force" if force_broadcast else "gps"
                        await self._enqueue_message(session, instruction, Priority.LOW, gps_source)
                else:
                    # ★ 方向变化强制播报使用独立source，避免被后续GPS消息替换
                    gps_source = "gps_force" if force_broadcast else "gps"
                    await self._enqueue_message(session, instruction, Priority.MEDIUM, gps_source)

            # 额外发送导航数据给 App（不播报，只传数据，无论是否去重都发送）
            try:
                nav_data_msg = {
                    "type": "navigation_data",
                    "instruction": instruction,
                    "remaining_distance": remaining_distance,
                    "arrived": False,
                    "current_step": data.get("current_step", 0),
                    "total_steps": data.get("total_steps", 0),
                }
                await session.websocket.send(
                    json.dumps(nav_data_msg, ensure_ascii=False)
                )
            except Exception as e:
                log.error("[%s] 发送导航数据失败: %s", session.session_id, e)

    async def _update_location_info(self, session: ClientSession):
        """逆地理编码更新城市信息"""
        if not session.is_gps_valid():
            return

        # ★ 逆地理编码缓存：位置变化小于100米时不重新查询（避免频繁调用高德API）
        if (hasattr(session, '_last_geo_lon') and session._last_geo_lon and
            hasattr(session, '_last_geo_lat') and session._last_geo_lat):
            from math import sqrt
            dlon = abs(session.gps_longitude - session._last_geo_lon)
            dlat = abs(session.gps_latitude - session._last_geo_lat)
            # 粗略距离估算（1度约111km）
            dist_m = sqrt((dlon * 111000) ** 2 + (dlat * 111000) ** 2)
            if dist_m < 100:
                log.debug("[%s] 位置变化%.0f米，跳过逆地理编码", session.session_id, dist_m)
                return  # 不需要更新城市信息

        result = await self.amap.reverse_geocode(
            session.gps_longitude, session.gps_latitude
        )

        if result:
            session.gps_city = result.get("city", "")
            session.gps_district = result.get("district", "")
            session.gps_address = result.get("formatted_address", "")
            log.info("[%s] 位置信息: %s %s",
                     session.session_id, session.gps_city, session.gps_district)
            session._last_geo_lon = session.gps_longitude
            session._last_geo_lat = session.gps_latitude

            # ★ 位置信息仅用于后台上下文（注入DeepSeek prompt），不主动播报
            # 之前开屏会播报"当前位置XX市XX区"，用户反馈完全没必要

    # ---------- 用户输入处理 ----------

    async def _handle_user_input(self, session: ClientSession, text: str):
        """处理用户语音输入"""
        session.touch()

        # ★ 问题8修复：闲聊后主动获取最新导航指令
        if text == "GET_LATEST_INSTRUCTION" and session.is_navigating:
            log.info("[%s] 收到GET_LATEST_INSTRUCTION请求，查询最新导航指令", session.session_id)
            try:
                instruction = await self.gps_ai.get_current_instruction()
                if instruction:
                    await self._enqueue_message(session, instruction, Priority.MEDIUM, "gps")
            except Exception as e:
                log.warning("[%s] 获取最新导航指令失败: %s", session.session_id, e)
            return

        # 如果正在等待确认（POI 列表），检查用户选择
        if session.pending_confirmation and session.pending_poi_list:
            await self._handle_poi_selection(session, text)
            return

        # ★ 意图分类：让 DeepSeek 判断用户想干什么，然后调用对应功能
        # 替代原来的关键词匹配，支持任意自然语言表达
        intent = await self.llm.classify_intent(text)
        log.info("[%s] 意图分类: %s (原文: %s)", session.session_id, intent, text)

        # ★ 意图分类失败（网络异常）时用关键词兜底，不要直接当 chat
        if intent == "chat":
            intent = self._keyword_fallback(text)
            if intent != "chat":
                log.info("[%s] 关键词兜底命中: %s (原文: %s)", session.session_id, intent, text)

        # ★ 手动路口模式控制（测试用，优先于意图分类）
        if "开启路口模式" in text or "打开路口模式" in text or "进入路口模式" in text:
            session._in_crossing_mode = True
            session._crossing_mode_enter_time = time.time()
            # ★ 同时切换视觉AI到 navigation 模式，否则 build_speak_text 不会被调用
            session._is_continuous_detecting = True
            await self.vision_ai.send_command("set_mode", mode="navigation")
            await self.vision_ai.send_command("nav_turn_approaching", direction="过马路", remaining=0)
            await self._enqueue_message(session, "路口模式已开启，请抬手机左右找斑马线", Priority.HIGH, "system")
            return
        if "关闭路口模式" in text or "退出路口模式" in text or "取消路口模式" in text:
            session._in_crossing_mode = False
            session._is_continuous_detecting = False
            await self.vision_ai.send_command("nav_turn_passed")
            await self.vision_ai.send_command("set_mode", mode="off")
            await self._enqueue_message(session, "路口模式已关闭", Priority.HIGH, "system")
            return

        if intent == "detect_once":
            # 识别一次：清除静音状态，让视觉结果能正常播报
            session._user_speaking_until = 0
            await self.vision_ai.send_command("set_mode", mode="once")
            await self._enqueue_message(session, "好的，正在帮你识别前方物体", Priority.HIGH, "system")
            return

        if intent == "detect_start":
            # 持续识别（测试用）：清除静音状态
            session._user_speaking_until = 0
            session._is_continuous_detecting = True
            await self.vision_ai.send_command("set_mode", mode="continuous")
            await self._enqueue_message(session, "已开始持续识别", Priority.HIGH, "system")
            return

        if intent == "detect_stop":
            # 停止识别
            session._is_continuous_detecting = False
            await self.vision_ai.send_command("set_mode", mode="off")
            await self._enqueue_message(session, "已停止识别", Priority.HIGH, "system")
            return

        if intent == "intersection_mode":
            # ★ 开启路口模式：切换到 navigation 模式 + 发送 nav_turn_approaching
            session._user_speaking_until = 0
            await self.vision_ai.send_command("set_mode", mode="navigation")
            await self.vision_ai.send_command("nav_turn_approaching", direction="过马路", remaining=0)
            await self._enqueue_message(session, "路口模式已开启，请抬手机左右找斑马线", Priority.HIGH, "system")
            return

        if intent == "find_store_mode":
            # ★ 开启找店模式：启动视觉AI的OCR找店功能
            session._user_speaking_until = 0
            await self.vision_ai.send_command("start_find_mode", target="店铺", keywords="店|超市|餐厅|药房|银行|便利店|咖啡|奶茶|面包|水果|理发|快递|药店")
            await self._enqueue_message(session, "找店模式已开启，请将摄像头对准路边店铺", Priority.HIGH, "system")
            return

        if intent == "navigate_stop":
            # ★ 停止导航：清空队列（停止正在排队的播报）
            async with session._outbox_lock:
                session._outbox_list.clear()
            session.is_navigating = False
            await self.gps_ai.stop_navigation()
            # 通知 App 停止导航
            try:
                await session.websocket.send(json.dumps({
                    "type": "navigation_stopped",
                    "text": "好的，导航已停止"
                }, ensure_ascii=False))
            except Exception:
                pass
            await self._enqueue_message(session, "好的，导航已停止", Priority.HIGH, "system")
            return

        if intent == "weather":
            # 天气查询
            city = session.gps_city if session.gps_city else "太原市"
            weather_reply = await self.amap.get_weather(city)
            session.add_history("user", text)
            session.add_history("assistant", weather_reply)
            await self._enqueue_message(session, weather_reply, Priority.LOW, "deepseek")
            return

        # ★ 用户开始说话（非识别/天气命令）：清空队列中低优先级消息，保留安全相关的高优先级消息
        # （用户和LLM对话时不需要视觉/GPS消息打断，但障碍物警告等安全消息必须保留）
        async with session._outbox_lock:
            cleared_count = len(session._outbox_list)
            session._outbox_list = [msg for msg in session._outbox_list if msg.priority in (Priority.HIGH, Priority.URGENT)]
            cleared_count -= len(session._outbox_list)
        if cleared_count > 0:
            log.info("[%s] 用户说话，清空队列中 %d 条待发消息", session.session_id, cleared_count)
        session._user_speaking_until = time.time() + 15.0

        # ★ 持续检测时用户说话：临时暂停视觉检测，避免视觉消息打断对话
        # 记住当前模式，等 DeepSeek 回复完后再恢复
        if session._is_continuous_detecting and session._detection_mode_before_speak == "":
            session._detection_mode_before_speak = "continuous"
            await self.vision_ai.send_command("set_mode", mode="off")
            log.info("[%s] 用户说话，临时暂停持续检测", session.session_id)

        # ★ 导航中用户问路，直接从 GPS AI 获取当前指令，不让 DeepSeek 编造
        if session.is_navigating and self._is_navigation_query(text):
            log.info("[%s] 导航中问路，从 GPS AI 获取当前指令", session.session_id)
            try:
                current_instruction = await self.gps_ai.get_current_instruction()
                if current_instruction and current_instruction != "当前没有进行中的导航":
                    await self._enqueue_message(session, current_instruction, Priority.HIGH, "gps")
                    return
            except Exception as e:
                log.warning("[%s] 获取 GPS AI 当前指令失败: %s", session.session_id, e)

        # 获取位置上下文
        location_context = session.get_location_context()

        # 调用 DeepSeek（navigate 和 chat 都走这里）
        ai_reply = await self.llm.chat(text, session.history.copy(), location_context)

        session.add_history("user", text)
        session.add_history("assistant", ai_reply)

        log.info("[%s] DeepSeek 回复: %s", session.session_id, ai_reply)

        # ★ 检测找店意图（优先于导航意图）
        if self._is_find_store_intent(text):
            target = self._extract_find_target(text)
            if target:
                log.info("[%s] 检测到找店意图，目标: %s", session.session_id, target)
                # ★ DeepSeek 地名纠错（"桃源路地铁站"→"桃园路地铁站"）
                corrected_target = await self.amap._deepseek_correct_keyword(target)
                if corrected_target != target:
                    log.info("[%s] 找店目标纠错: '%s' → '%s'", session.session_id, target, corrected_target)
                    target = corrected_target
                await self._enqueue_message(session, f"好的，正在帮你找{target}，请慢慢转动手机环顾四周", Priority.LOW, "system")
                # ★ 用 DeepSeek 转换店名为搜索词（肯德基→[肯德基,KFC]）
                search_keywords = await self._get_store_search_keywords(session, target)
                log.info("[%s] 店名搜索词: %s → %s", session.session_id, target, search_keywords)
                # 发送 start_find_mode 命令给视觉AI
                await self.vision_ai.send_command("start_find_mode", target=target, keywords=search_keywords)
                session._user_speaking_until = max(session._user_speaking_until, time.time() + 15.0)
                return

        # 检测是否包含导航意图
        if self._is_navigation_intent(text, ai_reply):
            destination = await self._extract_destination_with_llm(session, text)
            if destination:
                if session.is_gps_valid():
                    log.info("[%s] 检测到导航意图，目的地: %s", session.session_id, destination)
                    await self._handle_navigation_request(session, destination)
                    return
                else:
                    log.warning("[%s] 检测到导航意图但 GPS 数据无效，等待 GPS", session.session_id)
                    await self._enqueue_message(session, ai_reply, Priority.LOW, "deepseek")
                    await self._enqueue_message(
                        session, "正在定位，请稍等一下", Priority.LOW, "system"
                    )
                    session.pending_destination = destination
                    session._user_speaking_until = max(session._user_speaking_until, time.time() + 20.0)
                    # ★ 主动向 App 请求 GPS 数据（非导航状态下 App 不会主动发）
                    try:
                        await session.websocket.send(
                            json.dumps({"type": "request_gps_update"}, ensure_ascii=False)
                        )
                        log.info("[%s] 已向 App 请求 GPS 数据", session.session_id)
                    except Exception as e:
                        log.warning("[%s] 请求 GPS 数据失败: %s", session.session_id, e)
                    # ★ 启动临时 GPS 轮询（每2秒请求一次，直到 GPS 到达或超时）
                    asyncio.create_task(self._pending_gps_poll(session))
                    # 恢复持续检测
                    if session._detection_mode_before_speak == "continuous":
                        session._detection_mode_before_speak = ""
                        await self.vision_ai.send_command("set_mode", mode="continuous")
                    return

        # 普通回复
        await self._enqueue_message(session, ai_reply, Priority.LOW, "deepseek")
        # ★ DeepSeek 回复已入队，延长静音时间确保回复播完后再恢复视觉消息
        session._user_speaking_until = max(session._user_speaking_until, time.time() + 20.0)
        # ★ 恢复持续检测（如果之前是持续检测模式）
        if session._detection_mode_before_speak == "continuous":
            session._detection_mode_before_speak = ""  # 清除标记
            await self.vision_ai.send_command("set_mode", mode="continuous")
            log.info("[%s] 对话结束，恢复持续检测", session.session_id)

    async def _extract_destination_with_llm(self, session: ClientSession, user_text: str) -> str:
        """
        让DeepSeek从用户自然语言中提取目的地，同时纠错
        ★ 替代原来的正则提取 _extract_destination()
        解决：
        1. "徐东路的华莱士" → "许东路 华莱士"（错别字纠错）
        2. "许东街的华莱士" → "华莱士"（去掉修饰词）
        3. "山西大学坞城校区南门" → "山西大学坞城校区"（简化）
        """
        try:
            location_context = ""
            if session.gps_city:
                location_context = f"用户当前在{session.gps_city}"

            prompt = (
                f"{location_context}。用户说：{user_text}\n"
                "请提取用户想去的目的地，只返回地名关键词，不要任何解释。\n"
                "要求：\n"
                "1. 纠正可能的错别字（如\"徐东路\"→\"许东路\"）\n"
                "2. 去掉\"的\"\"到\"\"去\"等修饰词\n"
                "3. 如果有具体店名，只返回店名（如\"华莱士\"）\n"
                "4. 如果是地名+店名，返回\"店名 路名\"（如\"华莱士 许东路\"）\n"
                "例子：\n"
                "- 到徐东路的华莱士 → 华莱士 许东路\n"
                "- 导航到许东街的华莱士 → 华莱士 许东路\n"
                "- 山西大学 → 山西大学\n"
                "- 山西大学坞城校区南门 → 山西大学坞城校区\n"
                "- 附近的肯德基 → 肯德基\n"
            )
            result = await self.llm.chat(prompt, [], "")
            destination = result.strip().rstrip("。？！!?，,、")
            if destination and len(destination) >= 2:
                log.info("[%s] DeepSeek提取目的地: '%s' → '%s'",
                         session.session_id, user_text, destination)
                return destination
        except Exception as e:
            log.warning("[%s] DeepSeek提取目的地失败: %s", session.session_id, e)
            # fallback 到正则提取
            return self._extract_destination(user_text)
        return ""

    async def _simplify_destination_with_llm(self, session: ClientSession, destination: str) -> str:
        """
        让DeepSeek从用户描述中提取核心POI搜索关键词
        例如："许东街的华莱士" → "华莱士"
              "山西大学坞城校区南门" → "山西大学坞城校区"
        """
        try:
            prompt = (
                f"用户想去这个地方：{destination}\n"
                "请提取最适合在地图上搜索的核心地名或店名，只返回关键词，不要任何解释。\n"
                "例子：\n"
                "- 许东街的华莱士 → 华莱士\n"
                "- 山西大学坞城校区南门 → 山西大学坞城校区\n"
                "- 附近的肯德基 → 肯德基\n"
                "- 龙堡街和许东路交叉口 → 许东路\n"
            )
            result = await self.llm.chat(prompt, [], "")
            simplified = result.strip().rstrip("。？！!?，,、")
            if simplified and len(simplified) >= 2 and len(simplified) < len(destination):
                return simplified
        except Exception as e:
            log.warning("[%s] DeepSeek简化目的地失败: %s", session.session_id, e)
        return ""

    async def _handle_navigation_request(self, session: ClientSession, destination: str):
        """
        处理导航请求：
        1. 搜索附近 POI
        2. 生成确认问题让用户选择
        3. 如果只有一个结果，直接确认
        """
        # ★ 支持DeepSeek返回的"店名 路名"格式
        # 例如 "华莱士 许东路" → 先搜"华莱士"，再用"许东路"过滤结果
        search_keyword = destination
        location_hint = ""
        if " " in destination:
            parts = destination.split(" ", 1)
            search_keyword = parts[0]  # "华莱士"
            location_hint = parts[1]   # "许东路"

        log.info("[%s] 搜索附近 POI: %s%s", session.session_id, search_keyword,
                 f"（位置提示: {location_hint}）" if location_hint else "")

        # ★ 保存搜索关键词，用于追问时重新搜索
        session._nav_keyword = search_keyword

        poi_list = await self.amap.search_nearby_poi(
            search_keyword,
            session.gps_longitude,
            session.gps_latitude,
            Config.POI_SEARCH_RADIUS,
            city=session.gps_city,
        )

        # ★ 如果有位置提示，优先选择名称中包含位置提示的POI
        if location_hint and poi_list:
            hinted_results = [p for p in poi_list if location_hint in p.get("name", "")]
            if hinted_results:
                log.info("[%s] 位置提示 '%s' 过滤: %d/%d 个结果匹配",
                         session.session_id, location_hint, len(hinted_results), len(poi_list))
                poi_list = hinted_results

        # ★ 距离过滤：超过3公里的结果不推荐给盲人用户
        # 盲人步行导航，3公里以上的结果没有实际意义
        nearby_results = [p for p in poi_list if int(p.get("distance", "99999")) <= 3000]
        if nearby_results:
            poi_list = nearby_results
        elif poi_list:
            # 所有结果都超过3公里，只保留最近的3个，但提示用户距离较远
            poi_list = sorted(poi_list, key=lambda x: int(x.get("distance", "99999")))[:3]
            log.info("[%s] 所有POI结果均超过3公里，保留最近的 %d 个", session.session_id, len(poi_list))

        if not poi_list:
            # ★ 修复：第一次搜索没结果时，让DeepSeek提取核心关键词再搜一次
            # 用户说"许东街的华莱士"，正则提取出"许东街的华莱士"搜不到
            # DeepSeek能理解核心目的地是"华莱士"
            log.info("[%s] 第一次POI搜索无结果，尝试DeepSeek提取关键词: %s", session.session_id, destination)
            simplified = await self._simplify_destination_with_llm(session, destination)
            if simplified and simplified != destination:
                log.info("[%s] DeepSeek简化目的地: '%s' → '%s'", session.session_id, destination, simplified)
                poi_list = await self.amap.search_nearby_poi(
                    simplified,
                    session.gps_longitude,
                    session.gps_latitude,
                    Config.POI_SEARCH_RADIUS,
                    city=session.gps_city,
                )
                if poi_list:
                    log.info("[%s] 简化后搜索到 %d 个结果", session.session_id, len(poi_list))
                    # 搜索成功，继续后续流程
                else:
                    log.info("[%s] 简化后仍然无结果", session.session_id)
                    no_result_reply = f"附近没找到{destination}，你换个说法试试看"
                    await self._enqueue_message(session, no_result_reply, Priority.LOW, "system")
                    return
            else:
                no_result_reply = f"附近没找到{destination}，你换个说法试试看"
                await self._enqueue_message(session, no_result_reply, Priority.LOW, "system")
                return

        # ★ 智能匹配：检查是否有精确匹配的结果
        # 条件：POI名称完整包含用户说的目的地，且长度差不超过2个字
        # 防止"山西大学"(4字)匹配到"山西大学坞城校区"(8字)
        exact_match = None
        for poi in poi_list:
            poi_name = poi.get("name", "")
            # 精确匹配：POI名称包含用户说的全部关键词，且长度接近（差≤2字）
            if destination in poi_name and len(poi_name) - len(destination) <= 2:
                exact_match = poi
                break
            # 或者POI名称被用户说的完全包含（用户说的更具体，差≤2字）
            if poi_name in destination and len(destination) - len(poi_name) <= 2:
                exact_match = poi
                break

        if exact_match:
            # ★ 修复：检查是否有多个同名/近似名的POI
            # 如果有多个同名结果（如多个"许东路"），必须让用户选择，不能直接跳过
            exact_name = exact_match.get("name", "")
            same_name_count = sum(
                1 for p in poi_list if p.get("name", "") == exact_name
            )
            similar_count = sum(
                1 for p in poi_list
                if exact_name in p.get("name", "") or p.get("name", "") in exact_name
            )

            if same_name_count == 1 and similar_count == 1:
                # 唯一精确匹配，可以直接确认
                poi_name = exact_name
                dist = exact_match.get("distance", "")
                confirm_msg = f"找到{poi_name}，"
                if dist:
                    confirm_msg += f"离你大概{dist}米，"
                confirm_msg += "确认去这里吗"

                session.pending_confirmation = True
                session.pending_destination = poi_name
                session.pending_poi_list = poi_list
                await self._enqueue_message(session, confirm_msg, Priority.MEDIUM, "system",
                                            expect_reply=True)
                return
            else:
                # 多个同名/近似名结果，让用户选择（带距离区分）
                choices = []
                for i, poi in enumerate(poi_list[:3], 1):
                    name = poi.get("name", "")
                    d = poi.get("distance", "")
                    if d:
                        choices.append(f"第{i}个{name}{d}米")
                    else:
                        choices.append(f"第{i}个{name}")

                total = len(poi_list)
                select_msg = f"找到{total}个{exact_name}，"
                # ★ 用逗号分隔选项，避免尾部空格，App TTS 也更自然
                select_msg += "，".join(choices)
                if total > 3:
                    select_msg += "，还有更多"
                select_msg += "，你要去哪个"

                session.pending_confirmation = True
                session.pending_destination = destination
                session.pending_poi_list = poi_list
                await self._enqueue_message(session, select_msg, Priority.MEDIUM, "system",
                                            expect_reply=True)
                return

        if len(poi_list) == 1:
            # 只有一个结果，直接确认
            poi = poi_list[0]
            poi_name = poi.get("name", destination)
            dist = poi.get("distance", "")

            confirm_msg = f"找到{poi_name}，"
            if dist:
                confirm_msg += f"离你大概{dist}米，"
            confirm_msg += "确认去这里吗"

            session.pending_confirmation = True
            session.pending_destination = poi_name
            session.pending_poi_list = poi_list
            await self._enqueue_message(session, confirm_msg, Priority.MEDIUM, "system",
                                        expect_reply=True)
            return

        # 多个结果且没有精确匹配，让用户选择（最多播报3个）
        choices = []
        for i, poi in enumerate(poi_list[:3], 1):
            name = poi.get("name", "")
            choices.append(f"第{i}个{name}")

        total = len(poi_list)
        select_msg = f"找到{total}个可能的地方，"
        select_msg += "，".join(choices)
        if total > 3:
            select_msg += "，还有更多"
        select_msg += "，你要去哪个"

        session.pending_confirmation = True
        session.pending_destination = destination
        session.pending_poi_list = poi_list
        await self._enqueue_message(session, select_msg, Priority.MEDIUM, "system",
                                    expect_reply=True)

    async def _handle_poi_selection(self, session: ClientSession, text: str):
        """处理用户对 POI 列表的选择"""
        poi_list = session.pending_poi_list
        text_clean = text.strip().rstrip("。？！!?，,、")

        # 尝试匹配用户选择
        selected_poi = None

        # 优先级1：名称匹配 — 用户直接说了某个 POI 的名称
        for poi in poi_list:
            poi_name = poi.get("name", "")
            if poi_name and poi_name in text_clean:
                selected_poi = poi
                log.info("[%s] 名称匹配: '%s' in '%s'", session.session_id, poi_name, text_clean)
                break

        # 优先级2：部分名称匹配 — 用户说了关键词（如"坞城"）
        if not selected_poi:
            for poi in poi_list:
                poi_name = poi.get("name", "")
                # 提取 POI 名称中的关键词（去掉通用后缀）
                for keyword in ["坞城", "东山", "大东", "主校区", "新校区", "北校区", "南校区"]:
                    if keyword in poi_name and keyword in text_clean:
                        selected_poi = poi
                        log.info("[%s] 关键词匹配: '%s'", session.session_id, keyword)
                        break
                if selected_poi:
                    break

        # 优先级3：数字匹配 — "第1个"、"1"、"第一个"
        if not selected_poi:
            for i, poi in enumerate(poi_list):
                num_keywords = [f"第{i+1}个", f"第{i+1}", str(i+1)]
                for kw in num_keywords:
                    if kw in text_clean:
                        selected_poi = poi
                        break
                if selected_poi:
                    break

        # 匹配确认词
        # ★ 修复：当系统播报了"确认去这里吗"（精确匹配唯一结果），用户说"确认"应该直接开始导航
        # 之前只有 len(poi_list)==1 时才匹配确认词，但精确匹配时 poi_list 可能有多个结果
        # 判断依据：pending_destination 不为空（说明系统已经推荐了具体目的地）
        if not selected_poi:
            confirmed = any(w in text for w in ["是", "对", "好的", "嗯", "确认", "没错", "是的", "去", "就这个", "行", "可以"])
            if confirmed:
                # 有明确推荐的目的地（精确匹配），直接确认
                if session.pending_destination and session.pending_destination in [p.get("name", "") for p in poi_list]:
                    selected_poi = next((p for p in poi_list if p.get("name", "") == session.pending_destination), None)
                    log.info("[%s] 确认词匹配，使用推荐目的地: %s", session.session_id, session.pending_destination)
                # 只有一个候选时，也直接确认
                elif len(poi_list) == 1:
                    selected_poi = poi_list[0]

        # 匹配否定词
        cancelled = any(w in text for w in ["不", "不是", "取消", "算了", "不要", "换一个"])
        if cancelled:
            log.info("[%s] 用户取消导航", session.session_id)
            session.full_reset_navigation()
            cancel_reply = "好的，已取消，请问还有什么可以帮您"
            await self._enqueue_message(session, cancel_reply, Priority.LOW, "system")
            return

        if not selected_poi:
            # ★ 识别追问语境：用户在POI选择阶段说"还有别的吗""换一批"等
            followup_keywords = ["还有", "别的", "其他", "换一批", "更多", "下一个", "再看看", "有没有"]
            is_followup = any(kw in text for kw in followup_keywords)
            if is_followup:
                log.info("[%s] 用户追问'%s'，重新搜索POI", session.session_id, text)
                # 用原始关键词重新搜索
                original_keyword = session._nav_keyword if hasattr(session, '_nav_keyword') else None
                if original_keyword and session.is_gps_valid():
                    await self._handle_navigation_request(session, original_keyword)
                else:
                    retry_msg = "请告诉我您想去哪里"
                    await self._enqueue_message(session, retry_msg, Priority.LOW, "system",
                                                expect_reply=True)
                return

            # ★ 修复：用户可能说了新的目的地（如"华莱士"），而不是在选择POI
            # 检查用户输入是否包含导航意图，如果是，清除旧状态重新搜索
            # ★ 修复：移除"找"字，避免与 find_store 意图冲突
            nav_keywords = ["去", "到", "导航", "前往", "带我", "怎么走", "路线", "回"]
            has_nav_intent = any(kw in text for kw in nav_keywords)
            # 即使没有导航关键词，如果输入≥2字且不像选择词（不包含"第X个"等），也可能是目的地
            looks_like_destination = (
                len(text_clean) >= 2 and
                not any(c in text_clean for c in ["第", "个", "确认", "是", "对", "不", "取消", "算了", "还有", "别的", "其他"])
            )

            if has_nav_intent or (looks_like_destination and len(text_clean) >= 2):
                log.info("[%s] 用户输入'%s'不匹配POI选项，视为新的导航请求", session.session_id, text)
                session.full_reset_navigation()
                # 重新走导航流程
                destination = await self._extract_destination_with_llm(session, text)
                if destination and session.is_gps_valid():
                    await self._handle_navigation_request(session, destination)
                    return

            # 没有匹配到，重新让用户选择
            retry_msg = "没听清您选了哪个，请再说一次，比如第1个或第2个"
            await self._enqueue_message(session, retry_msg, Priority.LOW, "system",
                                        expect_reply=True)
            return

        # 用户选择了某个 POI，开始导航
        poi_name = selected_poi.get("name", "")
        log.info("[%s] 用户选择: %s，开始导航", session.session_id, poi_name)

        # ★ 新增：检查 GPS 数据是否有效，无效则等待
        if not session.is_gps_valid():
            log.warning("[%s] GPS 数据无效，等待 GPS 到达后再导航", session.session_id)
            await self._enqueue_message(
                session, "正在定位，请稍等一下", Priority.MEDIUM, "system"
            )
            session.pending_destination = poi_name
            session.pending_confirmation = True
            session.pending_poi_list = poi_list
            # 10 秒后如果 GPS 还没到，提示用户
            asyncio.create_task(self._wait_for_gps_then_navigate(session, poi_name, poi_list))
            return

        # 检查 GPS 精度，精度差时提醒用户（可能室内拿不到 GPS 卫星信号）
        if session.gps_accuracy > 50:
            warn_msg = f"温馨提示，现在定位不太准，大概{int(session.gps_accuracy)}米的误差，到室外空旷的地方会好一些"
            await self._enqueue_message(session, warn_msg, Priority.MEDIUM, "system")

        # 调用 GPS AI 开始导航
        # ★ 修复：发送坐标而不是名称，避免GPS AI重新搜索POI搜到别的城市
        poi_lon = selected_poi.get("longitude", "")
        poi_lat = selected_poi.get("latitude", "")
        if poi_lon and poi_lat:
            # 有坐标，直接用坐标导航
            nav_result = await self.gps_ai.start_navigation(
                session.gps_longitude,
                session.gps_latitude,
                f"{float(poi_lon)},{float(poi_lat)}",
            )
        else:
            # 没有坐标，fallback到名称（GPS AI会自己搜索）
            nav_result = await self.gps_ai.start_navigation(
                session.gps_longitude,
                session.gps_latitude,
                poi_name,
            )

        if nav_result:
            session.reset_navigation_state()  # 先清理旧状态
            session.is_navigating = True  # 再设为导航中

            route = nav_result.get("route", {})
            distance = route.get("distance", "0")
            duration = route.get("duration", "0")
            first_instruction = nav_result.get("first_instruction", "")
            steps = route.get("steps", [])

            # 记录初始总距离，用于计算已走距离
            try:
                session._nav_initial_distance = float(distance)
                session._nav_last_remaining = float(distance)
            except (ValueError, TypeError):
                session._nav_initial_distance = 0.0
                session._nav_last_remaining = 0.0

            # 通知 App 开始导航（App 收到后开始每 3 秒发送 gps_update 给 GPS AI）
            # ★ 优化：将出发播报合并到 navigation_started 消息中
            # 之前分两条发送（navigation_started + 队列出发达播报），导致App端TTS被连续调用
            # 现在合并为一条，减少TTS调用次数，让导航指令更快播报
            distance_int = int(distance) if str(distance).isdigit() else 0
            if distance_int >= 1000:
                dist_text = f"{distance_int / 1000:.1f}公里"
            else:
                dist_text = f"{distance_int}米"
            start_msg = {
                "type": "navigation_started",
                "text": f"出发，全程{dist_text}，放心跟着走",
                "total_distance": distance,
                "total_duration": duration,
                "steps": steps,
                "destination": poi_name,
            }
            try:
                await session.websocket.send(
                    json.dumps(start_msg, ensure_ascii=False)
                )
                log.info("[%s] → App: navigation_started", session.session_id)
            except Exception as e:
                log.error("[%s] 发送导航开始消息失败: %s", session.session_id, e)

            # ★ 不再单独播报"出发全程X米"——已合并到 navigation_started 中
            # 这样第一条导航指令可以更快通过队列发送给App
            # ★ 通知视觉 AI 切换到导航模式
            await self.vision_ai.send_command("set_mode", mode="navigation")
            # ★ 导航模式下清除持续检测状态
            session._is_continuous_detecting = False
            session._detection_mode_before_speak = ""
            # ★ 启动 GPS 轮询（服务端每 3 秒向 App 请求 GPS 数据）
            session._gps_poll_task = asyncio.create_task(
                self._gps_poll_loop(session)
            )
        else:
            # 导航启动失败
            fail_reply = "抱歉，没找到能走的路，换个地方试试吧"
            session.full_reset_navigation()
            await self._enqueue_message(session, fail_reply, Priority.LOW, "system")

    async def _wait_for_gps_then_navigate(self, session: ClientSession, poi_name: str, poi_list: list):
        """等待 GPS 数据到达后自动开始导航"""
        for i in range(10):
            await asyncio.sleep(1)
            if session.is_gps_valid():
                log.info("[%s] GPS 到达，自动开始导航到 %s", session.session_id, poi_name)
                # 直接调用导航启动（跳过 POI 选择）
                session.pending_confirmation = False
                await self._start_navigation_with_poi(session, poi_name, poi_list)
                return
        # 10 秒后 GPS 还没到
        log.warning("[%s] 等待 GPS 超时，提示用户", session.session_id)
        await self._enqueue_message(
            session, "无法获取您的位置，请确认已开启定位权限并到空旷处", Priority.MEDIUM, "system"
        )
        session.full_reset_navigation()

    async def _start_navigation_with_poi(self, session: ClientSession, poi_name: str, poi_list: list):
        """用指定的 POI 开始导航（从 _handle_poi_selection 中提取的公共逻辑）"""
        if session.gps_accuracy > 50:
            warn_msg = f"温馨提示，现在定位不太准，大概{int(session.gps_accuracy)}米的误差，到室外空旷的地方会好一些"
            await self._enqueue_message(session, warn_msg, Priority.MEDIUM, "system")

        nav_result = await self.gps_ai.start_navigation(
            session.gps_longitude,
            session.gps_latitude,
            poi_name,
        )

        if nav_result:
            session.reset_navigation_state()  # 先清理旧状态
            session.is_navigating = True  # 再设为导航中

            route = nav_result.get("route", {})
            distance = route.get("distance", "0")
            duration = route.get("duration", "0")
            first_instruction = nav_result.get("first_instruction", "")
            steps = route.get("steps", [])

            # 记录初始总距离，用于计算已走距离
            try:
                session._nav_initial_distance = float(distance)
                session._nav_last_remaining = float(distance)
            except (ValueError, TypeError):
                session._nav_initial_distance = 0.0
                session._nav_last_remaining = 0.0

            # ★ 问题11修复：播报总距离和预计步行时间
            try:
                dist_m = float(distance)
                if dist_m >= 1000:
                    dist_text = f"{dist_m/1000:.1f}公里"
                else:
                    dist_text = f"{int(dist_m)}米"
                walk_minutes = max(1, int(dist_m / 80))  # 按每分钟80米估算
                nav_start_text = f"导航已开始，全程约{dist_text}，预计步行{walk_minutes}分钟，请跟随语音指引"
            except (ValueError, TypeError):
                nav_start_text = f"导航已开始，总距离{distance}米".strip()

            start_msg = {
                "type": "navigation_started",
                "text": nav_start_text,
                "total_distance": distance,
                "total_duration": duration,
                "steps": steps,
                "destination": poi_name,
            }
            try:
                await session.websocket.send(
                    json.dumps(start_msg, ensure_ascii=False)
                )
                log.info("[%s] → App: navigation_started", session.session_id)
            except Exception as e:
                log.error("[%s] 发送导航开始消息失败: %s", session.session_id, e)

            # ★ 通知视觉 AI 切换到导航模式
            await self.vision_ai.send_command("set_mode", mode="navigation")
            # ★ 新增：启动 GPS 轮询（服务端每 3 秒向 App 请求 GPS 数据）
            session._gps_poll_task = asyncio.create_task(
                self._gps_poll_loop(session)
            )

            distance_int = int(distance) if str(distance).isdigit() else 0
            if distance_int >= 1000:
                dist_text = f"{distance_int / 1000:.1f}公里"
            else:
                dist_text = f"{distance_int}米"
            voice_msg = f"出发，全程{dist_text}，放心跟着走"
            await self._enqueue_message(session, voice_msg, Priority.MEDIUM, "gps")
        else:
            fail_reply = "抱歉，没找到能走的路，换个地方试试吧"
            session.full_reset_navigation()
            await self._enqueue_message(session, fail_reply, Priority.LOW, "system")

    async def _handle_user_confirm(self, session: ClientSession, text: str):
        """处理用户确认/否定（旧流程兼容，现在主要走 POI 选择）"""
        if not session.pending_confirmation:
            # 没有在等待确认，当普通输入处理
            await self._handle_user_input(session, text)
            return

        # 委托给 POI 选择处理
        await self._handle_poi_selection(session, text)

    async def _handle_navigation_stopped(self, session: ClientSession):
        """处理 App 通知导航已停止"""
        log.info("[%s] 导航已停止", session.session_id)
        session.full_reset_navigation()
        # ★ 清除斑马线引导状态
        session._nav_turn_direction = ""
        session._nav_turn_notified = False
        # 通知 GPS AI 停止导航
        await self.gps_ai.stop_navigation()
        # ★ 通知视觉 AI 切换回关闭模式（但不关闭摄像头推流！）
        # 只是把视觉识别模式设为off，App端摄像头推流保持，WebSocket连接不断
        # 这样后续用户说"开启持续识别"时，App仍在推帧，可以立即开始识别
        await self.vision_ai.send_command("set_mode", mode="off")
        # ★ 不再发送 camera_control(off)，避免App断开摄像头推流和WebSocket连接
        # 原来的 camera_control(off) 会导致：1)预览窗口消失 2)后续持续识别无法工作
        # ★ 清除持续检测状态
        session._is_continuous_detecting = False
        session._detection_mode_before_speak = ""
        # ★ 清空消息队列中积压的 GPS/视觉消息，让结束消息第一时间播出去
        async with session._outbox_lock:
            old_count = len(session._outbox_list)
            session._outbox_list.clear()
            if old_count > 0:
                log.info("[%s] 导航结束，清空队列中 %d 条积压消息", session.session_id, old_count)
        await self._enqueue_message(
            session, "导航结束啦还有什么需要帮忙的吗", Priority.LOW, "system"
        )

    # ---------- 斑马线引导联动 ----------

    async def _check_turn_zebra_guidance(self, session: ClientSession,
                                          instruction_normalized: str,
                                          remaining_distance: str):
        """
        ★ 导航联动斑马线引导：
        当导航指令包含转弯（左转/右转/左前方/右前方）且剩余距离 < 50米时，
        通知视觉AI进入"斑马线引导模式"，检测斑马线位置并引导用户走过去。
        """
        import re

        # 提取转弯方向
        turn_pattern = r'(向左转|向右转|向左前方|向右前方)'
        turn_match = re.search(turn_pattern, instruction_normalized)

        try:
            remaining = float(remaining_distance)
        except (ValueError, TypeError):
            remaining = 999.0

        TURN_APPROACH_DISTANCE = 25.0  # 距离路口25米内开始引导找斑马线

        if turn_match and remaining < TURN_APPROACH_DISTANCE:
            turn_dir = turn_match.group(1)
            if not session._nav_turn_notified or session._nav_turn_direction != turn_dir:
                # 新的转弯指令或方向变化，通知视觉AI
                session._nav_turn_direction = turn_dir
                session._nav_turn_notified = True
                await self.vision_ai.send_command(
                    "nav_turn_approaching",
                    direction=turn_dir,
                    remaining=remaining_distance
                )
                log.info("[%s] 斑马线引导: 通知视觉AI，%s 距离%.0f米",
                         session.session_id, turn_dir, remaining)
        elif not turn_match and session._nav_turn_notified:
            # 转弯指令消失（变成直行等），通知视觉AI退出引导模式
            session._nav_turn_notified = False
            session._nav_turn_direction = ""
            await self.vision_ai.send_command("nav_turn_passed")
            log.info("[%s] 斑马线引导: 转弯已过，通知视觉AI退出引导模式",
                     session.session_id)

    # ---------- 视觉 AI 消息回调 ----------

    async def _on_vision_message(self, data: dict):
        """收到视觉 AI 的消息"""
        msg_type = data.get("type", "")
        session_id = data.get("session_id", "")
        text = data.get("text", "")

        log.info("[视觉 AI] type=%s, session=%s, text=%s",
                 msg_type, session_id, text[:100])

        # 视觉 AI 的消息发送给所有活跃会话（障碍物警告是全局的）
        for session in list(self.sessions.values()):
            # ★ 找店结果不受用户说话静音期限制（找店是用户主动发起的）
            if msg_type != "find_result" and time.time() < session._user_speaking_until:
                continue

            # ★ 只清空队列中旧的视觉消息（避免播报过时内容）
            # 注意：不清 GPS 消息！GPS 方向信息同样重要，不能被视觉消息吞掉
            async with session._outbox_lock:
                session._outbox_list = [
                    m for m in session._outbox_list if m.source != "vision"
                ]

            if msg_type == "obstacle_warning":
                # ★ 根据内容区分优先级：
                # HIGH: 红绿灯、盲道障碍物（含"躲避"）、路口、斑马线引导
                # MEDIUM: 前方障碍物（不含"躲避"）、盲道状态、偏离斑马线
                is_critical = any(kw in text for kw in [
                    "红灯", "绿灯", "无信号灯", "躲避", "路口", "斑马线在",
                    "已到达斑马线", "站在斑马线", "未检测到红绿灯",
                ])
                priority = Priority.HIGH if is_critical else Priority.MEDIUM
                await self._enqueue_message(session, text, priority, "vision")
            elif msg_type == "detection_result":
                await self._enqueue_message(session, text, Priority.HIGH, "vision")
            elif msg_type == "find_result":
                # ★ 找店结果：高优先级播报方向指引
                success = data.get("success", False)
                target = data.get("target", "")
                if success:
                    await self._enqueue_message(session, text, Priority.HIGH, "vision")
                else:
                    await self._enqueue_message(session, text, Priority.LOW, "system")
            else:
                await self._enqueue_message(session, text, Priority.MEDIUM, "vision")

    # ---------- 消息优先级队列 ----------

    async def _enqueue_message(self, session: ClientSession, text: str,
                                priority: Priority, source: str,
                                expect_reply: bool = False):
        """将消息放入发送队列（抢占式：同来源旧消息被替换，队列上限5条）"""
        if not text or not text.strip():
            return  # ★ 过滤空消息，避免 App 收到空内容卡住

        msg = QueuedMessage(text=text, priority=priority, source=source,
                            expect_reply=expect_reply)
        async with session._outbox_lock:
            # ★ 抢占式替换：如果队列中已有同来源的消息，替换掉旧的
            #    这样 GPS 新指令会替换旧的 GPS 指令，视觉新警告替换旧的视觉警告
            # ★ gps / gps_force 互相替换，避免同一导航指令重复播报
            gps_mutual_sources = {"gps", "gps_force"}
            replaced = False
            for i, existing in enumerate(session._outbox_list):
                if existing.source == source:
                    # 新消息优先级 >= 旧消息优先级 → 替换
                    if priority >= existing.priority:
                        session._outbox_list[i] = msg
                        replaced = True
                        break
                    # 新消息优先级更低 → 不替换（保留高优先级的旧消息）
                    else:
                        return
                # ★ gps 和 gps_force 互相替换，避免重复播报同一导航指令
                elif source in gps_mutual_sources and existing.source in gps_mutual_sources:
                    if priority >= existing.priority:
                        session._outbox_list[i] = msg
                        replaced = True
                        break
                    else:
                        return
            if not replaced:
                session._outbox_list.append(msg)

            # ★ 队列上限：最多保留5条，超出时丢弃优先级最低的
            MAX_QUEUE_SIZE = 5
            if len(session._outbox_list) > MAX_QUEUE_SIZE:
                session._outbox_list.sort(key=lambda m: (-m.priority, m.timestamp))
                dropped = session._outbox_list[MAX_QUEUE_SIZE:]
                session._outbox_list = session._outbox_list[:MAX_QUEUE_SIZE]
                if dropped:
                    log.debug("[%s] 队列满，丢弃 %d 条低优先级消息", session.session_id, len(dropped))

            session._outbox_event.set()

    async def _outbox_worker(self, session: ClientSession):
        """每个会话的消息发送工作线程：按优先级发送，控制冷却"""
        try:
            while True:
                # 等待新消息
                await session._outbox_event.wait()
                async with session._outbox_lock:
                    session._outbox_event.clear()
                    if not session._outbox_list:
                        continue
                    # 按优先级排序，同优先级按时间排序（旧的先发）
                    session._outbox_list.sort(key=lambda m: (-m.priority, m.timestamp))
                    msg = session._outbox_list.pop(0)
                    # 如果队列中还有消息，重新设置事件，避免消息永久滞留
                    if session._outbox_list:
                        session._outbox_event.set()

                # 冷却控制
                # ★ HIGH/URGENT 优先级消息（障碍物警告等）跳过冷却，立即发送
                # ★ LOW/MEDIUM 消息（闲聊、导航提示）才需要等冷却
                if msg.priority < Priority.HIGH:
                    cooldown = 3.0 if session.is_navigating else Config.VOICE_COOLDOWN
                    elapsed = time.time() - session.last_send_time
                    if elapsed < cooldown:
                        await asyncio.sleep(cooldown - elapsed)

                try:
                    payload = json.dumps({
                        "type": "ai_reply",
                        "text": msg.text.strip(),
                        "priority": msg.priority.name,
                        "source": msg.source,
                        "expect_reply": msg.expect_reply,
                    }, ensure_ascii=False)
                    await session.websocket.send(payload)
                    session.last_send_time = time.time()
                    log.info("[%s] → App: [%s] %s",
                             session.session_id, msg.source, msg.text[:80])
                except Exception as e:
                    log.error("[%s] 发送失败: %s", session.session_id, e)
                    # ★ 清理僵尸 session：WebSocket 已断开，移除 session 避免资源泄漏
                    self._remove_session(session)
                    break

        except asyncio.CancelledError:
            pass

    # ---------- 关键词兜底（意图分类网络失败时使用）----------

    @staticmethod
    def _keyword_fallback(text: str) -> str:
        """
        当 DeepSeek 意图分类因网络失败 fallback 到 chat 时，
        用关键词做最后兜底，避免"开始检测"变成闲聊。
        """
        t = text.strip()
        # ★ 停止导航
        if any(k in t for k in ("停止导航", "取消导航", "结束导航", "别导航了", "不用导航")):
            return "navigate_stop"
        # ★ 找店模式（优先于识别，因为"帮我找店"可能被误分类）
        if any(k in t for k in ("开启找店模式", "找店模式", "OCR找店", "打开找店模式")):
            return "find_store_mode"
        # ★ 路口模式
        if any(k in t for k in ("开启路口模式", "打开路口模式", "进入路口模式", "过马路模式")):
            return "intersection_mode"
        # 识别/检测一次
        if any(k in t for k in ("帮我识别", "帮我检测", "帮我看看", "识别一下", "检测一下",
                                  "看看前面", "前方有什么", "前面是什么", "前面是啥",
                                  "前面有啥", "前方是啥", "前方检测")):
            return "detect_once"
        # 天气
        if any(k in t for k in ("天气", "气温", "下雨", "温度", "几度")):
            return "weather"
        return "chat"

    # ---------- 导航意图检测 ----------

    @staticmethod
    def _is_navigation_intent(user_text: str, ai_reply: str) -> bool:
        """
        检测用户是否有导航意图
        ★ 修复：双重检测策略
        1. 用户输入包含导航关键词 → 直接判定
        2. 用户输入不包含关键词，但DeepSeek回复包含导航相关词 → 也判定为导航意图
           解决用户只说地名（如"山西大学"）不带"去""导航"等关键词的问题
        """
        # 用户输入中的导航关键词
        # ★ 修复：移除"找"字，避免与 find_store 意图冲突
        # "帮我找瑞幸咖啡"是找店，不是导航；"导航到XX""去XX"才是导航
        nav_keywords = ["去", "到", "导航", "前往", "带我", "怎么走", "路线", "回"]
        if any(kw in user_text for kw in nav_keywords):
            return True

        # ★ DeepSeek回复中的导航意图信号
        # 当用户只说地名（如"山西大学"），DeepSeek通常会回复"正在帮你搜索/规划/导航"
        ai_nav_signals = ["搜索", "规划路线", "导航", "帮你找", "帮你去", "路线规划", "正在查"]
        if any(signal in ai_reply for signal in ai_nav_signals):
            return True

        return False

    @staticmethod
    def _is_weather_intent(user_text: str) -> bool:
        """检测用户是否在问天气"""
        weather_keywords = ["天气", "气温", "温度", "下雨", "刮风", "下雨吗", "晴天", "阴天"]
        return any(kw in user_text for kw in weather_keywords)

    @staticmethod
    def _is_navigation_query(user_text: str) -> bool:
        """检测导航中用户是否在问路（需要从 GPS AI 获取当前指令）"""
        query_keywords = [
            "往哪", "哪走", "怎么走", "方向", "左转", "右转",
            "直行", "前方", "接下来", "还有多远", "到了吗",
            "往哪里", "该走", "该往", "走哪", "什么方向",
        ]
        return any(kw in user_text for kw in query_keywords)

    @staticmethod
    def _is_find_store_intent(user_text: str) -> bool:
        """检测用户是否在找店/找招牌（触发 OCR 找店模式）"""
        import re
        find_keywords = [
            r"帮我找", r"帮我看看", r"帮我找找", r"找一下",
            r"看看.*在哪", r".*在哪.*店", r"找.*店",
            r"帮我看看.*在哪", r"帮我找找.*在哪",
            r"在哪", r"在哪里", r"在哪儿", r"在哪边",
            r"哪里有", r"附近有", r"附近有没有",
        ]
        text = user_text.strip()
        for pattern in find_keywords:
            if re.search(pattern, text):
                return True
        return False

    @staticmethod
    def _extract_find_target(user_text: str) -> str:
        """从用户输入中提取要找的目标（店名/招牌文字）"""
        import re
        text = user_text.strip()
        text = text.rstrip("。？！!?，,、")

        # 匹配各种口语表达（长前缀优先）
        patterns = [
            r"(?:给我找一下|帮我找一下|帮我找找|帮我找下|帮我找|给我找|帮我看看|找一下|找下|看看)\s*(.+?)(?:在哪|在哪里|在哪儿|在哪边|的|$)",
            r"(.+?)(?:在哪|在哪里|在哪儿|在哪边)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                target = match.group(1).strip()
                # 去掉口语前缀
                for word in ["一下", "一个", "那个", "那个", "下"]:
                    if target.startswith(word):
                        target = target[len(word):]
                # 去掉口语后缀
                for suffix in ["吧", "啊", "嘛", "呢", "一下"]:
                    if target.endswith(suffix):
                        target = target[:-1]
                if len(target) >= 2:
                    return target
        return ""

    async def _get_store_search_keywords(self, session, store_name: str) -> str:
        """用 DeepSeek 将店名转换为 OCR 搜索关键词列表
        
        例如: "肯德基" → "肯德基,KFC" 
              "瑞幸咖啡" → "瑞幸咖啡,luckin coffee,瑞幸"
              "沙县小吃" → "沙县小吃"（店名就是招牌，不需要转换）
        
        DeepSeek 会判断：如果店名和招牌文字一致，就只返回店名本身
        """
        prompt = (
            "你是一个店名转换助手。用户要找一个店铺，但店铺的招牌文字可能和店名不同。\n"
            "请将店名转换为可能出现在招牌上的所有文字，用英文逗号分隔。\n\n"
            "规则：\n"
            "1. 包含中文店名本身\n"
            "2. 包含英文/缩写形式（如肯德基→KFC，瑞幸咖啡→luckin coffee）\n"
            "3. 包含常见简称（如瑞幸咖啡→瑞幸）\n"
            "4. 如果店名本身就是招牌文字（如沙县小吃、兰州拉面），只返回店名本身\n"
            "5. 只返回关键词列表，不要其他文字\n\n"
            f"店名: {store_name}\n"
            "关键词:"
        )
        try:
            import httpx
            import json
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}"},
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.0,
                        "max_tokens": 100,
                    }
                )
                if resp.status_code == 200:
                    result = resp.json()
                    keywords = result["choices"][0]["message"]["content"].strip()
                    # 清理：去掉可能的引号、换行
                    keywords = keywords.replace('"', '').replace("'", '').replace('\n', ',')
                    # 验证：必须包含原始店名
                    if store_name not in keywords:
                        keywords = store_name + "," + keywords
                    log.info("[%s] DeepSeek 店名转换: %s → %s", session.session_id, store_name, keywords)
                    return keywords
                else:
                    log.warning("[%s] DeepSeek 店名转换失败: HTTP %s", session.session_id, resp.status_code)
        except Exception as e:
            log.warning("[%s] DeepSeek 店名转换异常: %s", session.session_id, e)
        
        # 失败时用内置映射兜底
        return self._builtin_store_keywords(store_name)

    @staticmethod
    def _builtin_store_keywords(store_name: str) -> str:
        """内置常见店名→搜索词映射（DeepSeek 失败时兜底）"""
        mapping = {
            "肯德基": "肯德基,KFC",
            "麦当劳": "麦当劳,McDonald's,M",
            "星巴克": "星巴克,Starbucks,STARBUCKS",
            "瑞幸咖啡": "瑞幸咖啡,luckin coffee,瑞幸",
            "必胜客": "必胜客,Pizza Hut",
            "华莱士": "华莱士",
            "汉堡王": "汉堡王,Burger King,BK",
            "德克士": "德克士",
            "沙县小吃": "沙县小吃",
            "兰州拉面": "兰州拉面",
            "蜜雪冰城": "蜜雪冰城",
            "古茗": "古茗",
            "茶百道": "茶百道",
            "喜茶": "喜茶,HEYTEA",
            "奈雪": "奈雪,奈雪的茶",
            "海底捞": "海底捞",
            "沃尔玛": "沃尔玛,Walmart",
            "全家": "全家,FamilyMart",
            "7-11": "7-11,Seven Eleven",
            "便利蜂": "便利蜂",
            "肯德基门": "肯德基门",  # 特殊：如果用户确实要找肯德基门
        }
        # 精确匹配
        if store_name in mapping:
            return mapping[store_name]
        # 模糊匹配
        for key, value in mapping.items():
            if key in store_name or store_name in key:
                return value
        return store_name

    @staticmethod
    def _extract_destination(user_text: str) -> str:
        """从用户输入中提取目的地（正则匹配，更灵活）"""
        import re
        text = user_text.strip()
        # 去掉末尾的标点符号
        text = text.rstrip("。？！!?，,、")

        # 正则匹配：各种口语表达后面的目的地
        # 匹配 "我要去/我想去/给我导航到/帮我导航到/导航到/带我去/去/到/前往" 等后面的内容
        patterns = [
            r"(?:我要?导航去|我要?导航到|给我导航到|帮我导航到|导航去|导航到|我想?去|我要?去|带我去|带我到|帮我找到|找|前往|去|到)\s*(.+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                destination = match.group(1).strip()
                # 去掉目的地后面可能残留的口语词
                for suffix in ["吧", "啊", "嘛", "呢"]:
                    if destination.endswith(suffix):
                        destination = destination[:-1]
                return destination

        # 如果没有匹配到前缀，整句话可能就是目的地（如 "山西大学"）
        # 但太短的话（<2字）不太可能是目的地
        if len(text) >= 2:
            return text
        return text

    # ---------- WebSocket 服务 ----------

    async def handle_client(self, websocket):
        """处理单个 App WebSocket 连接"""
        session = self._create_session(websocket)

        # 启动该会话的消息发送工作线程
        worker = asyncio.create_task(self._outbox_worker(session))

        try:
            async for raw in websocket:
                await self.handle_app_message(session, raw)
        except websockets.exceptions.ConnectionClosed:
            log.info("[%s] 连接关闭", session.session_id)
        finally:
            worker.cancel()
            # 如果正在导航，通知 GPS AI 停止
            if session.is_navigating:
                await self.gps_ai.stop_navigation()
            self._remove_session(session)

    async def handle_gps_ai_client(self, websocket):
        """
        处理 GPS AI 的 WebSocket 连接（端口 8767）
        GPS AI 通过此连接主动推送导航指令给小助手 AI
        ★ 修改：断开后自动等待重连（GPS AI 是客户端，会自动重连）
        """
        log.info("[GPS AI] WebSocket 已连接")
        # ★ 关闭旧的 GPS AI WebSocket 连接，避免资源泄漏
        if self._gps_ai_ws is not None:
            try:
                await self._gps_ai_ws.close()
            except Exception:
                pass
        self._gps_ai_ws = websocket
        try:
            async for raw in websocket:
                try:
                    data = json.loads(raw)
                    msg_type = data.get("type", "")

                    if msg_type == "gps_navigation_instruction":
                        # GPS AI 推送导航指令
                        # 找到当前正在导航的会话，转发指令
                        session = self._get_navigating_session()
                        if session:
                            await self._handle_gps_navigation_instruction(session, data)
                        else:
                            log.warning("[GPS AI] 收到导航指令但没有活跃的导航会话")

                    elif msg_type == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))

                    else:
                        log.debug("[GPS AI] 未知消息类型: %s", msg_type)

                except json.JSONDecodeError:
                    log.warning("[GPS AI] 收到无效 JSON")
        except websockets.exceptions.ConnectionClosed:
            log.warning("[GPS AI] WebSocket 连接断开，等待 GPS AI 重连...")
            self._gps_ai_ws = None
            # GPS AI 是客户端，会自动重连，这里只需要等待

    def _get_navigating_session(self) -> Optional[ClientSession]:
        """获取当前正在导航的会话"""
        for session in self.sessions.values():
            if session.is_navigating:
                return session
        return None


# ============================================================
#  入口
# ============================================================

async def main():
    print("=" * 55)
    print("  明途小助手 AI 服务端 v2.0 ★ GPS轮询+转发+POI过滤")
    print(f"  App 监听:    ws://0.0.0.0:{Config.ASSISTANT_PORT}")
    print(f"  GPS AI 监听: ws://0.0.0.0:{Config.GPS_AI_WS_PORT}")
    print(f"  GPS AI:      {Config.GPS_AI_BASE_URL} (HTTP REST)")
    print(f"  视觉 AI:     {Config.VISION_AI_URL} (WebSocket)")
    print(f"  LLM:         {Config.DEEPSEEK_MODEL}")
    if Config.AMAP_API_KEY:
        print(f"  高德 API:    已配置")
    else:
        print(f"  高德 API:    未配置！请在代码或环境变量中设置 AMAP_API_KEY")
    print("=" * 55)

    coordinator = AssistantCoordinator()
    await coordinator.start()

    # 检测 GPS AI 是否可达
    try:
        import httpx as _httpx_check
        _check_client = _httpx_check.AsyncClient(timeout=5.0)
        try:
            _resp = await _check_client.get(f"{Config.GPS_AI_BASE_URL}/api/navigation/status")
            if _resp.status_code == 200:
                print(f"  GPS AI:     已连接 ({Config.GPS_AI_BASE_URL})")
            else:
                print(f"  GPS AI:     响应异常 HTTP {_resp.status_code}")
        except Exception:
            print(f"  GPS AI:     无法连接 ({Config.GPS_AI_BASE_URL})")
            print(f"              请确认 GPS AI 已启动: python gps_app.py")
        finally:
            await _check_client.aclose()
    except Exception:
        pass

    # 同时启动两个 WebSocket 服务端
    # 8766: App WebSocket（App ↔ 小助手 AI）
    # 8767: GPS AI WebSocket（GPS AI → 小助手 AI，接收导航指令推送）
    try:
        async with websockets.serve(
            coordinator.handle_client,
            "0.0.0.0",
            Config.ASSISTANT_PORT,
        ), websockets.serve(
            coordinator.handle_gps_ai_client,
            "0.0.0.0",
            Config.GPS_AI_WS_PORT,
        ):
            print(f"\n  服务已启动:")
            print(f"     App WebSocket:   ws://0.0.0.0:{Config.ASSISTANT_PORT}")
            print(f"     GPS AI WebSocket: ws://0.0.0.0:{Config.GPS_AI_WS_PORT}")
            print()
            await asyncio.Future()
    finally:
        await coordinator.stop()


if __name__ == "__main__":
    asyncio.run(main())
