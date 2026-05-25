# -*- coding: utf-8 -*-
"""
导航引擎模块
核心导航逻辑：路线跟踪、位置匹配、导航指令生成
针对盲人用户优化，提供清晰的语音导航指令
"""

import math
import re
from amap_service import amap_service
from config import Config


def haversine_distance(lon1, lat1, lon2, lat2):
    """
    使用Haversine公式计算两个经纬度坐标之间的距离（单位：米）

    Args:
        lon1: 第一个点的经度
        lat1: 第一个点的纬度
        lon2: 第二个点的经度
        lat2: 第二个点的纬度

    Returns:
        float: 两点之间的距离（米）
    """
    # 地球平均半径（米）
    EARTH_RADIUS = 6371000.0

    # 将角度转为弧度
    lat1_rad = math.radians(float(lat1))
    lat2_rad = math.radians(float(lat2))
    delta_lat = math.radians(float(lat2) - float(lat1))
    delta_lon = math.radians(float(lon2) - float(lon1))

    # Haversine公式
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1_rad) * math.cos(lat2_rad) *
         math.sin(delta_lon / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return EARTH_RADIUS * c


def parse_polyline(polyline_str):
    """
    解析高德地图返回的polyline坐标点字符串

    Args:
        polyline_str: 分号分隔的"经度,纬度"坐标序列
                      例如: "116.481028,39.989643;116.481488,39.989468;..."

    Returns:
        list: 坐标点列表，每个元素为 (longitude, latitude) 元组
    """
    if not polyline_str:
        return []

    points = []
    segments = polyline_str.split(";")

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue
        try:
            lon, lat = segment.split(",")
            points.append((float(lon), float(lat)))
        except (ValueError, AttributeError):
            continue

    return points


def point_to_polyline_distance(point_lon, point_lat, polyline_points):
    """
    计算一个点到polyline折线的最短距离

    Args:
        point_lon: 点的经度
        point_lat: 点的纬度
        polyline_points: polyline坐标点列表 [(lon, lat), ...]

    Returns:
        float: 最短距离（米）
    """
    if not polyline_points:
        return float("inf")

    min_distance = float("inf")

    for i in range(len(polyline_points) - 1):
        # 计算点到每段线段的最短距离
        dist = point_to_segment_distance(
            point_lon, point_lat,
            polyline_points[i][0], polyline_points[i][1],
            polyline_points[i + 1][0], polyline_points[i + 1][1]
        )
        min_distance = min(min_distance, dist)

    # 同时计算到最后一个端点的距离
    last_point = polyline_points[-1]
    dist_to_end = haversine_distance(point_lon, point_lat, last_point[0], last_point[1])
    min_distance = min(min_distance, dist_to_end)

    return min_distance


def point_to_segment_distance(px, py, ax, ay, bx, by):
    """
    计算点P到线段AB的最短距离（简化版本，使用Haversine近似）

    Args:
        px, py: 点P的经纬度
        ax, ay: 线段起点A的经纬度
        bx, by: 线段终点B的经纬度

    Returns:
        float: 最短距离（米）
    """
    # 将经纬度近似为平面坐标计算（短距离内误差可接受）
    # 使用简单的投影：1度纬度约111km, 1度经度约111km*cos(lat)
    avg_lat = math.radians((float(py) + float(ay) + float(by)) / 3)
    cos_lat = math.cos(avg_lat)

    # 转换为近似平面坐标（米）
    scale = 111320.0  # 1度约111.32km
    px_m = float(px) * scale * cos_lat
    py_m = float(py) * scale
    ax_m = float(ax) * scale * cos_lat
    ay_m = float(ay) * scale
    bx_m = float(bx) * scale * cos_lat
    by_m = float(by) * scale

    # 向量AB
    abx = bx_m - ax_m
    aby = by_m - ay_m

    # 向量AP
    apx = px_m - ax_m
    apy = py_m - ay_m

    # 线段长度的平方
    ab_sq = abx * abx + aby * aby
    if ab_sq == 0:
        # A和B是同一个点
        return math.sqrt(apx * apx + apy * apy)

    # 计算投影比例 t = (AP·AB) / (AB·AB)
    t = (apx * abx + apy * aby) / ab_sq

    # 限制t在[0,1]范围内（线段而非直线）
    t = max(0.0, min(1.0, t))

    # 投影点
    proj_x = ax_m + t * abx
    proj_y = ay_m + t * aby

    # 点到投影点的距离
    dx = px_m - proj_x
    dy = py_m - proj_y

    return math.sqrt(dx * dx + dy * dy)


def distance_along_polyline(point_lon, point_lat, polyline_points):
    """
    计算用户在 polyline 上的投影位置到 polyline 起点的沿路距离

    ★ 修复已走距离计算：之前用"到起点的直线距离"，这是错的
    如果用户沿着 polyline 走，到起点的直线距离会先增大后减小
    正确做法：找到用户在 polyline 上的投影点，计算投影点到起点的沿路距离

    Args:
        point_lon: 用户当前经度
        point_lat: 用户当前纬度
        polyline_points: polyline 点列表 [(lon1, lat1), (lon2, lat2), ...]

    Returns:
        float: 用户在 polyline 上已走的距离（米）
    """
    if not polyline_points or len(polyline_points) < 2:
        return 0.0

    # 找到用户最近的线段
    min_dist = float('inf')
    best_segment_idx = 0
    best_t = 0.0  # 投影比例

    for i in range(len(polyline_points) - 1):
        ax, ay = polyline_points[i]
        bx, by = polyline_points[i + 1]

        # 计算用户到这条线段的距离和投影比例
        avg_lat = math.radians((float(point_lat) + float(ay) + float(by)) / 3)
        cos_lat = math.cos(avg_lat)
        scale = 111320.0

        px_m = float(point_lon) * scale * cos_lat
        py_m = float(point_lat) * scale
        ax_m = float(ax) * scale * cos_lat
        ay_m = float(ay) * scale
        bx_m = float(bx) * scale * cos_lat
        by_m = float(by) * scale

        abx = bx_m - ax_m
        aby = by_m - ay_m
        apx = px_m - ax_m
        apy = py_m - ay_m

        ab_sq = abx * abx + aby * aby
        if ab_sq == 0:
            dist = math.sqrt(apx * apx + apy * apy)
            t = 0.0
        else:
            t = (apx * abx + apy * aby) / ab_sq
            t = max(0.0, min(1.0, t))
            proj_x = ax_m + t * abx
            proj_y = ay_m + t * aby
            dx = px_m - proj_x
            dy = py_m - proj_y
            dist = math.sqrt(dx * dx + dy * dy)

        if dist < min_dist:
            min_dist = dist
            best_segment_idx = i
            best_t = t

    # 计算投影点到起点的沿路距离
    # = 前面所有完整线段的长度 + 当前线段上投影点到起点的长度
    distance = 0.0

    # 前面所有完整线段
    for i in range(best_segment_idx):
        ax, ay = polyline_points[i]
        bx, by = polyline_points[i + 1]
        distance += haversine_distance(ax, ay, bx, by)

    # 当前线段上投影点到起点的长度
    ax, ay = polyline_points[best_segment_idx]
    bx, by = polyline_points[best_segment_idx + 1]
    segment_length = haversine_distance(ax, ay, bx, by)
    distance += segment_length * best_t

    return distance


class NavigationEngine:
    """
    导航引擎类
    管理导航状态、路线跟踪和指令生成
    """

    def __init__(self):
        self._is_navigating = False  # 是否正在导航
        self._route = None  # 完整路线信息
        self._steps = []  # 路线步骤列表
        self._current_step_index = 0  # 当前步骤索引
        self._destination_address = ""  # 目的地地址文本
        self._destination_coords = None  # 目的地坐标 (lon, lat)
        self._total_distance = "0"  # 总距离（米）
        # ★ 新增：用户朝向跟踪（用于检测用户是否已转弯）
        self._last_heading = None  # 上次用户朝向
        self._heading_changed_significantly = False  # 朝向是否发生了显著变化（≥60°）
        # ★ 新增：heading滑动窗口平滑（防止GPS heading震荡导致方向文本疯狂翻转）
        self._heading_history = []  # 最近N次heading值
        self._heading_window_size = 5  # 滑动窗口大小（5次≈15秒@3秒/次）
        # ★ 新增：过马路检测
        self._crossing_state = None  # 过马路状态：None/"approaching"/"at_edge"/"crossing"/"crossed"
        self._crossing_points = []  # 过马路转折点坐标列表 [(lon, lat), ...]
        self._crossing_triggered = set()  # 已触发的 crossing_point 索引集合

    @property
    def is_navigating(self):
        """是否正在导航"""
        return self._is_navigating

    def start_navigation(self, origin_lon, origin_lat, destination_address):
        """
        开始导航

        流程：
        1. POI搜索找到精确坐标 → 路线规划（首选，适合"XX学校"、"XX商场"等）
        2. 如果失败 → 地理编码 → 路线规划（备选，适合具体地址文本）
        3. 如果仍失败 → 返回None

        Args:
            origin_lon: 起点经度
            origin_lat: 起点纬度
            destination_address: 目的地地址文本

        Returns:
            dict: 完整路线信息，包含 distance, duration, steps
                  失败时返回 None
        """
        print(f"[导航引擎] 开始导航: {destination_address}")

        # ★ 优化：如果destination已经是坐标格式（"lon,lat"），直接跳过POI搜索
        # 小助手AI现在会直接发送坐标，避免GPS AI重新搜索POI搜到别的城市
        if "," in destination_address:
            try:
                parts = destination_address.split(",")
                direct_lon = float(parts[0].strip())
                direct_lat = float(parts[1].strip())
                if 73 <= direct_lon <= 136 and 3 <= direct_lat <= 54:
                    # 坐标在中国范围内，直接规划路线
                    print(f"[导航引擎] 检测到坐标格式，直接规划路线: ({direct_lon}, {direct_lat})")
                    route = amap_service.plan_walking_route(
                        origin_lon, origin_lat, direct_lon, direct_lat
                    )
                    if route:
                        return self._init_navigation(route, destination_address, direct_lon, direct_lat)
                    else:
                        print("[导航引擎] 坐标路线规划失败")
                        return None
            except (ValueError, IndexError):
                pass  # 不是坐标格式，继续正常流程

        # ========== 第一步：POI搜索（首选）==========
        # POI搜索能精确匹配"XX学校(XX校区)"等名称，返回的坐标更准确
        poi_list = amap_service.search_poi(destination_address)

        if poi_list:
            # 遍历POI结果，逐个尝试路线规划
            for poi in poi_list:
                # 优先使用入口坐标，其次使用中心坐标
                try_lon = poi.get("entrance_longitude") or poi["longitude"]
                try_lat = poi.get("entrance_latitude") or poi["latitude"]
                poi_name = poi.get("name", destination_address)

                print(f"[导航引擎] 尝试POI: {poi_name} ({try_lon}, {try_lat})")

                route = amap_service.plan_walking_route(
                    origin_lon, origin_lat, try_lon, try_lat
                )
                if route:
                    print(f"[导航引擎] 使用POI '{poi_name}' 的坐标规划成功")
                    return self._init_navigation(route, poi_name, try_lon, try_lat)
        else:
            print("[导航引擎] POI搜索无结果，尝试地理编码备选...")

        # ========== 第二步：地理编码备选 ==========
        # 适合具体地址文本，如"北京市朝阳区望京SOHO"
        geo_result = amap_service.geocode(destination_address)

        if geo_result:
            dest_lon = geo_result["longitude"]
            dest_lat = geo_result["latitude"]

            route = amap_service.plan_walking_route(
                origin_lon, origin_lat, dest_lon, dest_lat
            )
            if route:
                return self._init_navigation(route, destination_address, dest_lon, dest_lat)

            print("[导航引擎] 地理编码坐标无法规划路线")

        print("[导航引擎] 导航失败：POI搜索和地理编码均无法规划路线")
        return None

    def _init_navigation(self, route, destination_address, dest_lon, dest_lat):
        """
        初始化导航状态

        Args:
            route: 路线信息字典
            destination_address: 目的地地址文本
            dest_lon: 目的地经度
            dest_lat: 目的地纬度

        Returns:
            dict: 路线信息
        """
        self._route = route
        self._steps = route["steps"]
        self._current_step_index = 0
        self._destination_address = destination_address
        self._destination_coords = (float(dest_lon), float(dest_lat))
        self._total_distance = route["distance"]
        self._is_navigating = True

        # ★ 新增：检测过马路转折点
        self._crossing_state = None
        self._crossing_points = []
        self._crossing_triggered = set()
        self._detect_crossing_points()

        print(f"[导航引擎] 导航已开始，共{len(self._steps)}步，总距离{self._total_distance}米")
        # ★ 打印每个 step 的摘要（调试用）
        for i, step in enumerate(self._steps):
            action = step.get('action', '')
            marker = " ★★★过马路" if "通过人行横道" in action else ""
            print(f"[导航引擎]   step{i}: action={action}{marker}  {step.get('instruction', '')[:50]}  距离={step.get('distance', '?')}m")

        return route

    def _detect_crossing_points(self):
        """
        检测路线中的过马路转折点

        ★ 方案C：仅使用高德API官方action检测
        只识别明确标注为"通过人行横道"等过马路动作的step
        取消方向变化检测，避免普通转弯被误识别为过马路

        转折点 = step polyline 的第一个点（过马路步骤的起点）
        """
        # 仅使用高德官方action检测过马路
        for i, step in enumerate(self._steps):
            action = step.get("action", "")
            # 高德API定义的过马路相关action
            crossing_actions = ("通过人行横道", "到达道路斜对面", "通过过街天桥", "通过地下通道")
            if any(ca in action for ca in crossing_actions):
                polyline_str = step.get("polyline", "")
                points = parse_polyline(polyline_str)
                if points:
                    crossing_point = points[0]  # 过马路步骤的起点
                    # 检查是否已存在
                    already_exists = any(
                        abs(cp[0] - crossing_point[0]) < 0.0001 and
                        abs(cp[1] - crossing_point[1]) < 0.0001
                        for cp in self._crossing_points
                    )
                    if not already_exists:
                        self._crossing_points.append(crossing_point)
                        print(f"[导航引擎] ★ 检测到高德'过马路'步骤 step{i} ({action})，"
                              f"坐标{crossing_point}")

        # ★ 方案C：已移除方向变化检测，避免普通转弯被误触发
        # 原方向变化检测代码已删除

    def update_location(self, current_lon, current_lat, heading=None, accuracy=None):
        """
        更新用户当前位置，返回导航指令

        Args:
            current_lon: 当前经度
            current_lat: 当前纬度
            heading: 手机朝向角度（0-360，0=北，90=东，180=南，270=西），可选
            accuracy: GPS定位精度（米），可选，保留参数兼容性

        Returns:
            dict: 包含 current_instruction, next_instructions,
                  remaining_distance, arrived 的导航信息
        """
        if not self._is_navigating:
            return {
                "current_instruction": "当前没有进行中的导航",
                "next_instructions": [],
                "remaining_distance": "0",
                "arrived": False
            }

        # 检查是否到达终点
        if self._destination_coords:
            dist_to_dest = haversine_distance(
                current_lon, current_lat,
                self._destination_coords[0], self._destination_coords[1]
            )
            if dist_to_dest < Config.ARRIVAL_DISTANCE:
                self._is_navigating = False
                print("[导航引擎] 已到达目的地")
                return {
                    "current_instruction": "您已到达目的地附近",
                    "next_instructions": [],
                    "remaining_distance": "0",
                    "arrived": True
                }

        # 遍历路线steps，找到用户当前所在的step
        current_step = self._find_current_step(current_lon, current_lat)

        # ★ 朝向变化检测：如果用户朝向变化≥60°，标记为显著变化
        # 这意味着用户可能已经转弯，即使 GPS 坐标没变也应该重新评估
        if heading is not None:
            # ★ heading滑动窗口平滑（防止GPS heading震荡导致方向文本疯狂翻转）
            # ★ 修复：当heading与上一次差异>45°时，说明用户在转弯或转圈测试，
            #   此时清空历史窗口，直接使用最新值，避免平滑导致方向完全错误
            if self._last_heading is not None:
                raw_diff = abs(heading - self._last_heading)
                if raw_diff > 180:
                    raw_diff = 360 - raw_diff
                if raw_diff > 45:
                    # heading剧烈变化，清空平滑窗口，直接用最新值
                    self._heading_history = [heading]
                    smoothed_heading = heading
                else:
                    # heading变化正常，加入平滑窗口
                    self._heading_history.append(heading)
                    if len(self._heading_history) > self._heading_window_size:
                        self._heading_history.pop(0)
                    smoothed_heading = self._smooth_heading(self._heading_history)
            else:
                self._heading_history.append(heading)
                if len(self._heading_history) > self._heading_window_size:
                    self._heading_history.pop(0)
                smoothed_heading = self._smooth_heading(self._heading_history)
            
            if self._last_heading is not None:
                heading_diff = abs(smoothed_heading - self._last_heading)
                if heading_diff > 180:
                    heading_diff = 360 - heading_diff
                if heading_diff >= 60:
                    self._heading_changed_significantly = True
                    print(f"[导航引擎] 用户朝向显著变化 {heading_diff:.0f}°（{self._last_heading:.0f}°→{smoothed_heading:.0f}°）")
            self._last_heading = smoothed_heading
            # ★ 使用平滑后的heading生成指令
            heading = smoothed_heading

        # 生成导航指令（传入手机朝向）
        instruction = self._generate_instruction(
            current_step, current_lon, current_lat, heading
        )

        # 生成后续步骤预览（也传入手机朝向）
        next_instructions = self._get_next_instructions(current_step, heading)

        # 计算剩余距离（传入当前坐标，扣除已走距离）
        remaining_distance = self._calculate_remaining_distance(
            current_step, current_lon, current_lat
        )

        # ★ 过马路检测（返回结构化状态）
        crossing_info = self._check_crossing(
            current_lon, current_lat, current_step
        )

        result = {
            "current_instruction": instruction,
            "next_instructions": next_instructions,
            "remaining_distance": remaining_distance,
            "current_step": current_step,
            "total_steps": len(self._steps),
            "arrived": False,
            "heading_changed": self._heading_changed_significantly,
            "smoothed_heading": heading  # ★ 返回平滑后的heading，供GPS AI计算偏差
        }

        # ★ 如果有过马路状态，添加到结果中（结构化，不只是文本）
        if crossing_info:
            result["crossing_warning"] = crossing_info.get("text", "")
            result["crossing_state"] = crossing_info.get("state", "")

        # ★ 返回前重置（否则 heading_changed 永远为 True，去重永久失效）
        self._heading_changed_significantly = False
        return result

    def _consume_heading_change(self):
        """消费朝向变化标记（调用后重置）"""
        changed = self._heading_changed_significantly
        self._heading_changed_significantly = False
        return changed

    @staticmethod
    def _smooth_heading(heading_history):
        """
        使用圆形均值法对heading做滑动窗口平滑
        解决GPS heading震荡导致方向文本疯狂翻转的问题
        
        Args:
            heading_history: heading值列表（0-360度）
            
        Returns:
            float: 平滑后的heading（0-360度）
        """
        if not heading_history:
            return 0.0
        if len(heading_history) == 1:
            return heading_history[0]
        
        import math
        # 圆形均值：分别计算sin和cos的均值，再转回角度
        sin_avg = sum(math.sin(math.radians(h)) for h in heading_history) / len(heading_history)
        cos_avg = sum(math.cos(math.radians(h)) for h in heading_history) / len(heading_history)
        smoothed = math.degrees(math.atan2(sin_avg, cos_avg)) % 360
        return smoothed

    def _check_crossing(self, current_lon, current_lat, current_step_index):
        """
        检查用户是否接近/到达/通过过马路转折点

        状态机：
        - None → "approaching"：用户接近 crossing_point（< 50米）
        - "approaching" → "at_edge"：用户到达 crossing_point（< 10米）
        - "at_edge" → "crossed"：用户过了 crossing_point（距离 > 10米）

        每个 crossing_point 只触发一次（用 _crossing_triggered 集合记录）

        Returns:
            dict or None: {"state": str, "text": str} 或 None
        """
        if not self._crossing_points:
            return None

        for idx, crossing_point in enumerate(self._crossing_points):
            # 跳过已触发的点
            if idx in self._crossing_triggered:
                continue

            dist = haversine_distance(
                current_lon, current_lat,
                crossing_point[0], crossing_point[1]
            )

            # 状态 None → "approaching"：接近 crossing_point（< 50米）
            if self._crossing_state is None and dist < 50:
                self._crossing_state = "approaching"
                self._crossing_triggered.add(idx)
                print(f"[导航引擎] 过马路检测: approaching，距离{dist:.1f}米，"
                      f"crossing_point[{idx}]{crossing_point}")
                return {"state": "approaching", "text": "前方路口，请抬手机左右找斑马线"}

            # 状态 "approaching" → "at_edge"：到达 crossing_point（< 10米）
            if self._crossing_state == "approaching" and dist < 10:
                self._crossing_state = "at_edge"
                print(f"[导航引擎] 过马路检测: at_edge，距离{dist:.1f}米，"
                      f"crossing_point[{idx}]{crossing_point}")
                return {"state": "at_edge", "text": ""}

            # 状态 "at_edge" → "crossed"：过了 crossing_point（距离 > 10米）
            if self._crossing_state == "at_edge" and dist > 10:
                self._crossing_state = "crossed"
                # 计算当前 step 剩余米数
                remaining_meters = 0
                if current_step_index < len(self._steps):
                    step = self._steps[current_step_index]
                    try:
                        total_distance = int(step.get("distance", "0"))
                    except (ValueError, TypeError):
                        total_distance = 0
                    if total_distance > 0:
                        polyline_str = step.get("polyline", "")
                        if polyline_str:
                            polyline_points = parse_polyline(polyline_str)
                            if polyline_points and len(polyline_points) >= 2:
                                walked = distance_along_polyline(
                                    current_lon, current_lat, polyline_points
                                )
                                walked = max(0, min(walked, total_distance))
                                remaining_meters = round(total_distance - walked)
                print(f"[导航引擎] 过马路检测: crossed，距离{dist:.1f}米，"
                      f"剩余{remaining_meters}米")
                return {"state": "crossed", "text": f"已过马路，继续向前方步行{remaining_meters}米"}

        return None

    def get_current_instruction(self):
        """
        获取当前应该播报的导航指令（不更新位置）

        Returns:
            str: 当前导航指令文本
        """
        if not self._is_navigating:
            return "当前没有进行中的导航"

        if self._current_step_index < len(self._steps):
            step = self._steps[self._current_step_index]
            return step.get("instruction", "继续前行")
        else:
            return "您已到达目的地附近"

    def stop_navigation(self):
        """停止导航"""
        self._is_navigating = False
        self._route = None
        self._steps = []
        self._current_step_index = 0
        self._destination_address = ""
        self._destination_coords = None
        self._total_distance = "0"
        self._last_heading = None
        self._heading_changed_significantly = False
        self._heading_history = []  # ★ 重置heading平滑历史
        # ★ 新增：重置过马路检测状态
        self._crossing_state = None
        self._crossing_points = []
        self._crossing_triggered = set()
        print("[导航引擎] 导航已停止")

    def get_status(self):
        """
        获取当前导航状态

        Returns:
            dict: 包含 is_navigating, destination 的状态信息
        """
        return {
            "is_navigating": self._is_navigating,
            "destination": self._destination_address if self._is_navigating else ""
        }

    def _find_current_step(self, current_lon, current_lat):
        """
        根据用户当前位置，找到当前所在的路线步骤

        通过计算用户位置到每个step的polyline的距离来判断用户在哪一步
        ★ 只允许前进或保持，不允许回退（防止 GPS 漂移导致 step 倒退）

        Args:
            current_lon: 当前经度
            current_lat: 当前纬度

        Returns:
            int: 当前步骤索引
        """
        # 计算到当前 step 的距离
        current_step = self._steps[self._current_step_index]
        current_polyline_str = current_step.get("polyline", "")
        current_dist = float("inf")
        if current_polyline_str:
            current_polyline_points = parse_polyline(current_polyline_str)
            if current_polyline_points:
                current_dist = point_to_polyline_distance(
                    current_lon, current_lat, current_polyline_points
                )

        # 计算到下一个 step 的距离（只看 step+1，不跳过中间步骤）
        min_future_dist = float("inf")
        best_future_index = self._current_step_index

        next_index = self._current_step_index + 1
        if next_index < len(self._steps):
            step = self._steps[next_index]
            polyline_str = step.get("polyline", "")
            if polyline_str:
                polyline_points = parse_polyline(polyline_str)
                if polyline_points:
                    dist = point_to_polyline_distance(
                        current_lon, current_lat, polyline_points
                    )
                    min_future_dist = dist
                    best_future_index = next_index

        # ★ 智能跳转逻辑：
        # 只有当下一个 step 明显比当前 step 更近时才跳转
        # 条件1：下一个 step 距离 < 当前 step 距离 - 10米（明显更近）
        # 条件2：当前 step 距离 > 30米 且 下一步更近（用户已走过终点）
        # 条件3：当前 step 已走 > 80% 且 下一步距离 < 当前 step 距离 + 5米（接近终点）
        jumped = False
        if min_future_dist < current_dist - 10:
            # 条件1：明显更近
            jumped = True
        elif min_future_dist < current_dist and current_dist > 30:
            # 条件2：已走过终点
            jumped = True
        else:
            # 条件3：接近终点（已走 > 80%）
            current_polyline_str = current_step.get("polyline", "")
            if current_polyline_str:
                current_polyline_points = parse_polyline(current_polyline_str)
                if current_polyline_points and len(current_polyline_points) >= 2:
                    try:
                        total_dist = int(current_step.get("distance", "0"))
                    except (ValueError, TypeError):
                        total_dist = 0
                    if total_dist > 0:
                        walked = distance_along_polyline(
                            current_lon, current_lat, current_polyline_points
                        )
                        if walked > total_dist * 0.8 and min_future_dist < current_dist + 5:
                            jumped = True

        if jumped:
            old_index = self._current_step_index
            self._current_step_index = best_future_index
            print(f"[导航引擎] step 跳转: {old_index} → {best_future_index} "
                  f"(当前step距离{current_dist:.0f}m, 下一步距离{min_future_dist:.0f}m)")

        return self._current_step_index

    def _generate_instruction(self, step_index, current_lon, current_lat, heading=None):
        """
        生成当前导航指令文本
        如果提供了heading（手机朝向），会将绝对方向转为相对方向

        Args:
            step_index: 当前步骤索引
            current_lon: 当前经度
            current_lat: 当前纬度
            heading: 手机朝向角度（0-360），可选

        Returns:
            str: 导航指令文本
        """
        if step_index >= len(self._steps):
            return "您已到达目的地附近"

        current_step = self._steps[step_index]

        # ★ 修复：如果当前 step 的 instruction 包含方向动作（如"步行231米右转"），
        # 且接近当前 step 终点，优先播报当前 step 的方向动作
        # 之前只看 next_step 的 action，导致"步行231米右转"接近终点时播报"请准备到达"（next_step是到达）
        current_instruction = current_step.get("instruction", "")
        current_action_match = re.search(r'(左转|右转|直行|掉头|靠左|靠右)', current_instruction)

        # 检查是否接近当前 step 终点
        current_polyline = parse_polyline(current_step.get("polyline", ""))
        dist_to_current_end = float("inf")
        if current_polyline and len(current_polyline) >= 2:
            end_point = current_polyline[-1]
            dist_to_current_end = haversine_distance(
                current_lon, current_lat,
                end_point[0], end_point[1]
            )

        # 如果当前 step 有方向动作，且接近终点（< 30米），优先播报当前 step 的动作
        if current_action_match and dist_to_current_end < 30:
            action = current_action_match.group(1)
            dist_int = int(round(dist_to_current_end))
            if heading is not None:
                relative_action = self._absolute_to_relative(action, heading)
                return f"前方约{dist_int}米，请准备{relative_action}"
            return f"前方约{dist_int}米，请准备{action}"

        # 检查是否接近下一个step的转折点
        if step_index + 1 < len(self._steps):
            next_step = self._steps[step_index + 1]
            next_polyline = parse_polyline(next_step.get("polyline", ""))

            if next_polyline:
                # 下一个step的起点就是转折点
                turn_point = next_polyline[0]
                dist_to_turn = haversine_distance(
                    current_lon, current_lat,
                    turn_point[0], turn_point[1]
                )

                if dist_to_turn < Config.TURN_APPROACH_DISTANCE:
                    # 接近转折点，提前提示
                    dist_int = int(round(dist_to_turn))
                    next_action = next_step.get("action", "")
                    next_instruction = next_step.get("instruction", "继续前行")

                    # ★ 修复：如果 action 为空，从 instruction 中提取动作
                    if not next_action:
                        # 高德 instruction 格式如"沿XX路向南步行60米左转"
                        # 提取最后一个方向词作为动作
                        action_match = re.search(r'(左转|右转|直行|到达|掉头|靠左|靠右)', next_instruction)
                        if action_match:
                            next_action = action_match.group(1)
                        else:
                            next_action = "转弯"

                    # 如果有朝向，把动作转为相对方向
                    if heading is not None:
                        relative_action = self._absolute_to_relative(next_action, heading)
                        return f"前方约{dist_int}米，请准备{relative_action}"

                    return (
                        f"前方约{dist_int}米，请准备{next_action}。"
                        f"{next_instruction}"
                    )

        # 正常播报当前步骤指令
        instruction = current_step.get("instruction", "继续前行")
        road = current_step.get("road", "")
        distance = current_step.get("distance", "0")

        # ★ 修复：计算当前 step 中已经走过的距离，播报剩余米数
        # 之前直接用 step.distance（总米数），导致"步行248米"永远不变
        try:
            total_distance = int(distance) if distance.isdigit() else 0
        except (ValueError, TypeError):
            total_distance = 0

        remaining_in_step = total_distance
        if total_distance > 0:
            polyline_str = current_step.get("polyline", "")
            if polyline_str:
                polyline_points = parse_polyline(polyline_str)
                if polyline_points and len(polyline_points) >= 2:
                    # ★ 修复：使用沿 polyline 的投影距离，而不是到起点的直线距离
                    walked = distance_along_polyline(
                        current_lon, current_lat, polyline_points
                    )
                    walked = max(0, min(walked, total_distance))
                    remaining_in_step = round(total_distance - walked)

        # 如果有朝向，将指令中的绝对方向转为相对方向
        if heading is not None:
            instruction = self._convert_instruction_direction(instruction, heading)

        # ★ 替换指令中的固定米数为剩余米数
        # 高德返回的 instruction 包含固定米数如"步行248米右转"
        # 需要替换为实际剩余米数如"步行190米右转"
        if remaining_in_step != total_distance and total_distance > 0:
            # 匹配 "步行XXX米" 或 "走XXX米" 或 "前行XXX米" 等模式
            pattern = r'(步行|走|前行|行进)(\d+)米'
            match = re.search(pattern, instruction)
            if match:
                instruction = instruction.replace(
                    f"{match.group(1)}{match.group(2)}米",
                    f"{match.group(1)}{remaining_in_step}米"
                )

        # ★ 优化：为盲人用户拼接更完整的导航指令
        # 格式："沿XX路向前方步行63米右转" 而不是仅仅 "向前方步行63米右转"
        # 道路名称帮助盲人确认自己在正确的路上
        if road and road not in instruction:
            # 去掉指令开头的"沿XX路"避免重复
            for prefix in [f"沿{road}", f"沿着{road}"]:
                if instruction.startswith(prefix):
                    instruction = instruction[len(prefix):]
                    break
            instruction = f"沿{road}{instruction}"

        # 确保距离信息在指令中（盲人需要知道走多远）
        # ★ 使用剩余米数，不是总米数
        if remaining_in_step > 0 and "米" not in instruction:
            instruction = f"{instruction}{remaining_in_step}米"

        # ★ 优化：如果当前 step 只有"到达"且距离较远，生成更友好的指令
        # 高德 API 有时返回的 step instruction 就是"到达"，没有"直行"信息
        # 盲人用户需要知道还要走多远、什么方向
        action = current_step.get("action", "")
        if (not action or action == "到达") and remaining_in_step > 30:
            instruction = f"继续直行约{remaining_in_step}米到达目的地"

        return instruction

    def _get_next_instructions(self, current_step_index, heading=None):
        """
        获取后续几个步骤的预览指令

        Args:
            current_step_index: 当前步骤索引
            heading: 手机朝向角度（0-360），可选

        Returns:
            list: 后续步骤指令文本列表（最多3个）
        """
        next_instructions = []
        start = current_step_index + 1
        end = min(start + 3, len(self._steps))

        for i in range(start, end):
            step = self._steps[i]
            instruction = step.get("instruction", "继续前行")

            # 如果有朝向，转换方向
            if heading is not None:
                instruction = self._convert_instruction_direction(instruction, heading)

            next_instructions.append(instruction)

        return next_instructions

    @staticmethod
    def _orientation_to_degree(orientation_str):
        """
        将高德返回的方向中文转为角度

        Args:
            orientation_str: 方向字符串，如"南"、"东南"、"北"

        Returns:
            float: 角度值（0-360），无法识别时返回None
        """
        if not orientation_str:
            return None

        mapping = {
            "北": 0, "东北": 45, "东": 90, "东南": 135,
            "南": 180, "西南": 225, "西": 270, "西北": 315
        }

        return mapping.get(orientation_str)

    @staticmethod
    def _absolute_to_relative(action_str, heading):
        """
        将绝对方向动作转为相对方向描述

        Args:
            action_str: 动作字符串，如"左转"、"右转"、"向左前方"
            heading: 手机朝向角度（0-360）

        Returns:
            str: 相对方向描述，如"向左转"、"向右前方走"
        """
        # 这些动作本身就是相对的，不需要转换
        relative_actions = ["左转", "右转", "直行", "到达", "靠左", "靠右",
                           "通过人行横道", "通过过街天桥", "通过地下通道",
                           "通过广场", "到道路斜对面", "往前走", "往后走",
                           "进入右侧道路", "进入左侧道路"]
        for ra in relative_actions:
            if ra in action_str:
                return action_str

        # 含有方向的动作需要转换
        direction_mapping = {
            "北": 0, "东北": 45, "东": 90, "东南": 135,
            "南": 180, "西南": 225, "西": 270, "西北": 315
        }

        for direction_name, direction_deg in direction_mapping.items():
            if direction_name in action_str:
                # 计算相对角度
                relative = (direction_deg - heading + 360) % 360
                # ★ 修复：用 _relative_direction_for_action 获取通顺的动作描述
                relative_desc = NavigationEngine._relative_direction_for_action(relative)
                return f"{relative_desc}{action_str.replace(direction_name, '').strip()}"

        return action_str

    @staticmethod
    def _relative_direction_for_action(relative_angle):
        """
        根据相对角度返回适合拼接在动作后面的方向描述（用于 _absolute_to_relative）

        与 _relative_direction 的区别：
        - _relative_direction 返回 "向前方"、"向右转" 等（带"向"字前缀）
        - 本方法返回 "向前方"、"向右" 等（适合拼接在动作词后面）

        Args:
            relative_angle: 相对角度（0-360）

        Returns:
            str: 方向描述
        """
        angle = relative_angle if relative_angle <= 180 else relative_angle - 360

        if abs(angle) <= 20:
            return "向前方"
        elif 20 < angle <= 70:
            return "向右前方"
        elif 70 < angle <= 110:
            return "向右"
        elif 110 < angle <= 170:
            return "向右后方"
        elif 170 < angle or angle < -170:
            return "向后"
        elif -170 <= angle < -110:
            return "向左后方"
        elif -110 <= angle < -70:
            return "向左"
        elif -70 <= angle < -20:
            return "向左前方"

        return "向前方"

    @staticmethod
    def _relative_direction(relative_angle):
        """
        根据相对角度返回方向描述

        Args:
            relative_angle: 相对角度（0-360）

        Returns:
            str: 方向描述，如"向前方"、"向左前方"、"向左转"
        """
        # 归一化到 -180 ~ 180
        angle = relative_angle if relative_angle <= 180 else relative_angle - 360

        if abs(angle) <= 20:
            return "向前方"
        elif 20 < angle <= 70:
            return "向右前方"
        elif 70 < angle <= 110:
            return "向右转"
        elif 110 < angle <= 170:
            return "向右后方"
        elif 170 < angle or angle < -170:
            return "向后转"
        elif -170 <= angle < -110:
            return "向左后方"
        elif -110 <= angle < -70:
            return "向左转"
        elif -70 <= angle < -20:
            return "向左前方"

        return "向前方"

    def _convert_instruction_direction(self, instruction, heading):
        """
        将导航指令中的绝对方向转为相对方向

        高德 instruction 格式如 "沿XX路向南步行100米左转"
        需要将 "向南" 替换为相对方向如 "向前方"

        Args:
            instruction: 原始指令，如"向南步行100米左转"
            heading: 手机朝向角度（0-360）

        Returns:
            str: 转换后的指令
        """
        if not instruction or heading is None:
            return instruction

        direction_mapping = {
            "北": 0, "东北": 45, "东": 90, "东南": 135,
            "南": 180, "西南": 225, "西": 270, "西北": 315
        }

        # 按方向名称长度从长到短排序，避免"东南"被"东"先匹配
        sorted_directions = sorted(direction_mapping.keys(), key=len, reverse=True)

        for direction_name in sorted_directions:
            # ★ 匹配 "向X" 格式（如"向南"、"向东"）
            # 这是高德 instruction 中方向词的标准格式
            import re
            pattern = r'向' + direction_name
            match = re.search(pattern, instruction)
            if match:
                direction_deg = direction_mapping[direction_name]
                relative = (direction_deg - heading + 360) % 360
                # ★ 使用 _relative_direction_for_action 获取适合嵌入指令的方向词
                # _relative_direction 返回 "向后转" → "沿XX路向后转步行100米左转"（不通顺）
                # _relative_direction_for_action 返回 "向后" → "沿XX路向后步行100米左转"（通顺）
                relative_desc = self._relative_direction_for_action(relative)
                instruction = instruction.replace("向" + direction_name, relative_desc, 1)
                break

        return instruction

    def _calculate_remaining_distance(self, current_step_index, current_lon=None, current_lat=None):
        """
        计算从当前位置到终点的剩余距离

        ★ 修复：扣除当前 step 中用户已经走过的距离
        之前只把每个 step 的 distance 加起来，没有扣除已走距离

        Args:
            current_step_index: 当前步骤索引
            current_lon: 当前经度（可选，用于计算已走距离）
            current_lat: 当前纬度（可选，用于计算已走距离）

        Returns:
            str: 剩余距离（米），文本格式
        """
        remaining = 0

        for i in range(current_step_index, len(self._steps)):
            step = self._steps[i]
            try:
                step_distance = int(step.get("distance", "0"))
            except (ValueError, TypeError):
                step_distance = 0

            if i == current_step_index and current_lon is not None and current_lat is not None:
                # ★ 扣除当前 step 中已经走过的距离
                # ★ 修复：使用沿 polyline 的投影距离，而不是到起点的直线距离
                polyline_str = step.get("polyline", "")
                if polyline_str:
                    polyline_points = parse_polyline(polyline_str)
                    if polyline_points and len(polyline_points) >= 2:
                        walked = distance_along_polyline(
                            current_lon, current_lat, polyline_points
                        )
                        # 已走距离不能超过 step 总距离，也不能为负
                        walked = max(0, min(walked, step_distance))
                        remaining += (step_distance - walked)
                        print(f"[导航引擎] 剩余距离计算: step{current_step_index} 总{step_distance}m - 已走{walked:.0f}m = {step_distance - walked:.0f}m")
                    else:
                        remaining += step_distance
                else:
                    remaining += step_distance
            else:
                remaining += step_distance

        return str(round(remaining))


# 创建全局导航引擎实例
navigation_engine = NavigationEngine()
