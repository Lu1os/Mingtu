# -*- coding: utf-8 -*-
"""
Flask主应用
提供导航相关的REST API接口

架构：
  App ──(GPS+指南针, WebSocket)──→ 小助手 AI ──(WebSocket转发)──→ GPS AI（本服务端，端口 5000）
  GPS AI ──(导航指令, WebSocket)──→ 小助手 AI（端口 8767）
  小助手 AI ──(统一播报)──→ App

GPS数据流：
  App → 小助手 AI（WebSocket 8766）→ GPS AI（WebSocket 8767，小助手AI转发）
  GPS AI → 小助手 AI（WebSocket 8767，主动推送导航指令）→ App（WebSocket 8766）

REST API 仅用于导航启动/停止/状态查询，GPS位置更新通过 WebSocket 接收。
"""

import sys
import os
import json
import asyncio
import threading
import time

# 确保可以导入同目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request, jsonify
from flask_cors import CORS
from config import Config
from amap_service import amap_service
from navigation_engine import navigation_engine, parse_polyline


def bearing(lat1, lon1, lat2, lon2):
    """方位角计算（度）"""
    import math
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1)*math.sin(phi2) - math.sin(phi1)*math.cos(phi2)*math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ==================== GPS AI → 小助手 AI WebSocket 客户端 ====================

class AssistantWebSocketClient:
    """
    GPS AI 主动连接小助手 AI 的 WebSocket 客户端
    导航中：收到 App 位置更新 → 计算导航指令 → 推送给小助手 AI
    """

    def __init__(self, uri="ws://127.0.0.1:8767"):
        self.uri = uri
        self.ws = None
        self.connected = False
        self._lock = threading.Lock()
        self._thread = None
        self._running = False
        # 指令去重：避免相同指令短时间内重复推送
        # ★ 优化：降低抑制时间到8秒，方向变化大时不抑制
        self._last_instruction = ""
        self._last_instruction_time = 0.0
        self._instruction_suppress_interval = 15.0  # 坐标不变时15秒去重（避免重复播报）
        # ★ 新增：GPS 坐标不变检测
        self._last_gps_lon = None
        self._last_gps_lat = None
        self._gps_stale_count = 0  # GPS 坐标连续不变的次数
        self._gps_stale_threshold = 10  # 连续10次不变（约10秒）后降低推送频率
        self._last_step_index = -1  # ★ 上次播报的step索引
        self._short_instruction_interval = 6.0  # ★ 简短确认的最小间隔（秒）

    def start(self):
        """启动后台连接线程"""
        self._running = True
        self._thread = threading.Thread(target=self._connect_loop, daemon=True)
        self._thread.start()
        print(f"[GPS→小助手] WebSocket 客户端已启动，目标: {self.uri}")

    def stop(self):
        """停止连接"""
        self._running = False
        self.connected = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3)

    def _connect_loop(self):
        """后台线程：持续尝试连接小助手 AI"""
        import websocket as ws_lib

        while self._running:
            try:
                self.ws = ws_lib.WebSocket()
                self.ws.settimeout(5)
                self.ws.connect(self.uri)
                self.connected = True
                print("[GPS→小助手] WebSocket 已连接 ✅")

                # ★ 问题17修复：重连后主动同步crossing状态给assistant_server
                # 防止断线重连期间的crossing_state=approaching丢失
                self._sync_crossing_state_on_reconnect()

                # 连接成功后保持心跳
                while self._running and self.connected:
                    try:
                        # 接收小助手 AI 的消息（心跳响应等）
                        result = self.ws.recv()
                        if result is None or result == "":
                            break
                        try:
                            data = json.loads(result)
                            msg_type = data.get("type", "")
                            if msg_type == "gps_update":
                                # 小助手 AI 转发的 App GPS 数据
                                longitude = data.get("longitude")
                                latitude = data.get("latitude")
                                heading = data.get("heading")
                                accuracy = data.get("accuracy")
                                if longitude is not None and latitude is not None:
                                    print(f"[GPS←小助手] 收到GPS数据: ({longitude:.6f}, {latitude:.6f}) heading={heading} accuracy={accuracy}")
                                    # 在新线程中处理导航更新（避免阻塞WebSocket接收）
                                    threading.Thread(
                                        target=self._handle_gps_update_from_assistant,
                                        args=(longitude, latitude, heading, accuracy),
                                        daemon=True
                                    ).start()
                            elif msg_type == "pong":
                                pass  # 心跳响应
                            elif msg_type == "navigation_stop":
                                # 小助手 AI 通知停止导航
                                print("[GPS→小助手] 收到停止导航通知")
                                navigation_engine.stop_navigation()
                        except json.JSONDecodeError:
                            pass  # 忽略非 JSON 消息
                    except ws_lib.WebSocketTimeoutException:
                        # 超时，发送心跳
                        try:
                            self.ws.send(json.dumps({"type": "ping"}))
                        except Exception:
                            break
                    except ws_lib.WebSocketConnectionClosedException:
                        break
                    except Exception as e:
                        print(f"[GPS→小助手] 接收消息异常: {e}")
                        break

            except Exception as e:
                self.connected = False
                print(f"[GPS→小助手] 连接失败: {e}，5秒后重试...")

            if self._running:
                time.sleep(5)

        self.connected = False

    def _sync_crossing_state_on_reconnect(self):
        """★ 问题17修复：重连后检查当前crossing状态并同步给assistant_server"""
        try:
            if not hasattr(navigation_engine, '_crossing_points') or not navigation_engine._crossing_points:
                return
            # 获取当前位置（从最近一次GPS更新）
            current_lon = self._last_gps_lon
            current_lat = self._last_gps_lat
            if current_lon is None or current_lat is None:
                print("[GPS→小助手] 重连补偿：无GPS坐标，跳过crossing状态同步")
                return
            # 检查是否在某个crossing_point附近（50米内）
            from navigation_engine import haversine
            min_dist = float('inf')
            for cp in navigation_engine._crossing_points:
                d = haversine(current_lat, current_lon, cp[1], cp[0])
                min_dist = min(min_dist, d)
            if min_dist < 50:
                print(f"[GPS→小助手] ★ 重连补偿：距离最近crossing_point {min_dist:.0f}米，发送approaching")
                self.send_navigation_instruction(
                    "前方需要过马路，请注意安全",
                    str(int(min_dist)),
                    crossing_state="approaching",
                    crossing_warning="前方需要过马路，请注意安全"
                )
            else:
                print(f"[GPS→小助手] 重连补偿：距离最近crossing_point {min_dist:.0f}米，无需同步")
        except Exception as e:
            print(f"[GPS→小助手] 重连补偿异常: {e}")

    def send_navigation_instruction(self, instruction, remaining_distance, arrived=False, heading_changed=False, current_step=0, total_steps=0, crossing_state="", crossing_warning="", heading_deviation=None):
        """
        发送导航指令给小助手 AI
        小助手 AI 收到后排队播报给 App
        ★ 重构去重策略：
          - 相同指令8秒内不重复推送
          - 方向变化≥45°时强制推送，不受去重限制
          - 用户朝向显著变化（≥60°）时强制推送，不受去重限制
        """
        if not self.connected or not self.ws:
            print(f"[GPS→小助手] 未连接，无法发送指令: {instruction}")
            return False

        # 到达目的地的消息始终发送
        if not arrived:
            import re
            now = time.time()
            
            # ★★★ 播报分级策略：根据heading偏差和距离生成不同播报内容 ★★★
            # heading_deviation: 当前朝向与路线方向的偏差角度（None表示无heading数据）
            # 1. 新step开始（step变化）→ 完整指令
            # 2. 距关键点≤50米 → 完整指令+强调
            # 3. 同step中距离更新 + heading偏差<15° → "继续向前"
            # 4. 同step中距离更新 + heading偏差15°~30° → "稍微偏左/右，继续向前"
            # 5. 同step中距离更新 + heading偏差>30° → 不播报简短确认（等偏航检测处理）
            
            is_new_step = False
            if current_step != getattr(self, '_last_step_index', -1):
                is_new_step = True
                self._last_step_index = current_step
                print(f"[GPS→小助手] ★ 新step: {current_step}, 播报完整指令")
            
            # 判断是否接近关键点（转弯、过马路等）
            is_approaching_keypoint = False
            try:
                remaining_m = float(remaining_distance)
                if remaining_m <= 50:
                    is_approaching_keypoint = True
            except (ValueError, TypeError):
                pass
            
            # 判断指令类型（是否包含方向变化关键词）
            has_direction_change = bool(re.search(r'(左转|右转|掉头|过马路|人行横道|到达道路)', instruction))
            
            # 生成简短确认播报
            short_instruction = None
            if not is_new_step and not has_direction_change and not is_approaching_keypoint and not heading_changed:
                # ★ 简短确认统一冷却：无论有没有heading数据，都至少间隔6秒
                suppress_interval = getattr(self, '_short_instruction_interval', 6.0)
                if now - self._last_instruction_time >= suppress_interval:
                    if heading_deviation is not None:
                        abs_dev = abs(heading_deviation)
                        if abs_dev < 15:
                            short_instruction = "继续向前"
                        elif abs_dev < 30:
                            # ★ 根据偏差方向生成具体播报（偏了向右前方/偏了向左前方）
                            if heading_deviation > 0:
                                short_instruction = "偏了向右前方，继续向前"
                            else:
                                short_instruction = "偏了向左前方，继续向前"
                        # heading_deviation >= 30: 不播报简短确认（等偏航检测处理）
                    else:
                        # 没有heading数据时，也播报简短确认
                        short_instruction = "继续向前"
            
            # 如果生成了简短确认，替换instruction
            if short_instruction is not None:
                dev_dir = "偏右" if heading_deviation > 0 else "偏左" if heading_deviation < 0 else "无偏差"
                print(f"[GPS→小助手] ★ 简短确认: \"{short_instruction}\" (heading{dev_dir}{abs(heading_deviation)}°)" if heading_deviation is not None else f"[GPS→小助手] ★ 简短确认: \"{short_instruction}\"")
                # 简短确认使用不同的source，避免替换完整GPS指令
                original_instruction = instruction
                instruction = short_instruction
                # 简短确认不经过去重逻辑，直接发送
                try:
                    msg = {
                        "type": "gps_navigation_instruction",
                        "instruction": instruction,
                        "remaining_distance": str(remaining_distance),
                        "arrived": False,
                        "current_step": current_step,
                        "total_steps": total_steps,
                        "crossing_state": crossing_state,
                        "crossing_warning": crossing_warning,
                        "instruction_type": "short_confirm"  # ★ 标记为简短确认
                    }
                    with self._lock:
                        self.ws.send(json.dumps(msg, ensure_ascii=False))
                    print(f"[GPS→小助手] 已推送简短确认: {instruction} (剩余 {remaining_distance}m)")
                    return True
                except Exception as e:
                    print(f"[GPS→小助手] 发送简短确认失败: {e}")
                    self.connected = False
                    return False
            
            # 接近关键点时，增强播报
            if is_approaching_keypoint and not is_new_step:
                if re.search(r'左转', instruction):
                    instruction = "前方注意左转，" + instruction
                elif re.search(r'右转', instruction):
                    instruction = "前方注意右转，" + instruction
                elif re.search(r'过马路|人行横道', instruction):
                    instruction = "前方注意过马路，" + instruction
                print(f"[GPS→小助手] ★ 关键点强调: \"{instruction}\"")

            instruction_normalized = re.sub(r'\d+米', '', instruction)
            last_normalized = re.sub(r'\d+米', '', self._last_instruction) if self._last_instruction else ""

            # ★ 方向变化检测：方向变化≥45°时强制推送
            dir_pattern = r'(向前方|向左前方|向右前方|向左转|向右转|向左后方|向右后方|向后转)'
            dir_to_angle = {
                "向前方": 0, "向右前方": 45, "向右转": 90,
                "向右后方": 135, "向后转": 180, "向左后方": 225,
                "向左转": 270, "向左前方": 315,
            }
            current_dir = re.search(dir_pattern, instruction_normalized)
            last_dir = re.search(dir_pattern, last_normalized) if last_normalized else None
            force_send = False
            if current_dir and last_dir:
                current_angle = dir_to_angle.get(current_dir.group(1))
                last_angle = dir_to_angle.get(last_dir.group(1))
                if current_angle is not None and last_angle is not None:
                    angle_diff = abs(current_angle - last_angle)
                    if angle_diff > 180:
                        angle_diff = 360 - angle_diff
                    if angle_diff >= 45:
                        # ★ 防振荡：A→B→A 不推送
                        prev_dir = getattr(self, '_prev_direction_angle', None)
                        if prev_dir is not None and current_angle == prev_dir:
                            print(f"[GPS→小助手] 方向振荡（{last_dir.group(1)}→{current_dir.group(1)}→{last_dir.group(1)}），跳过")
                        else:
                            force_send = True
                            print(f"[GPS→小助手] 方向变化{angle_diff}°（{last_dir.group(1)}→{current_dir.group(1)}），强制推送")

            # ★ 简单文本去重（方向变化大或朝向显著变化时不受限制）
            # ★ 米数变化超过20米时也强制推送（用户需要知道走了多远）
            meters_changed = False
            if last_normalized:
                import re as re2
                cur_m = re2.search(r'步行(\d+)米', instruction)
                last_m = re2.search(r'步行(\d+)米', self._last_instruction) if self._last_instruction else None
                if cur_m and last_m:
                    try:
                        if abs(int(cur_m.group(1)) - int(last_m.group(1))) >= 20:
                            meters_changed = True
                    except ValueError:
                        pass

            if (not force_send and not heading_changed and not meters_changed and
                    instruction_normalized == last_normalized and
                    now - self._last_instruction_time < self._instruction_suppress_interval):
                print(f"[GPS→小助手] 指令去重，跳过重复: {instruction}")
                return False
            self._last_instruction = instruction
            self._last_instruction_time = now
            # ★ 保存方向历史（用于防振荡）
            if current_dir and last_dir:
                last_angle_saved = dir_to_angle.get(last_dir.group(1))
                if last_angle_saved is not None:
                    self._prev_direction_angle = last_angle_saved

        try:
            msg = {
                "type": "gps_navigation_instruction",
                "instruction": instruction,
                "remaining_distance": str(remaining_distance),
                "arrived": arrived,
                "current_step": current_step,
                "total_steps": total_steps,
                "crossing_state": crossing_state,
                "crossing_warning": crossing_warning
            }
            with self._lock:
                self.ws.send(json.dumps(msg, ensure_ascii=False))
            print(f"[GPS→小助手] 已推送指令: {instruction} (剩余 {remaining_distance}m)")
            return True
        except Exception as e:
            print(f"[GPS→小助手] 发送指令失败: {e}")
            self.connected = False
            return False

    def _handle_gps_update_from_assistant(self, longitude, latitude, heading=None, accuracy=None):
        """
        处理从小助手 AI 通过 WebSocket 转发来的 GPS 数据
        调用导航引擎更新位置，获取导航指令，然后推送回小助手 AI
        """
        try:
            # ★ 诊断日志：打印进程ID和导航状态，确认是否多进程问题
            import os
            print(f"[GPS←小助手] PID={os.getpid()} is_navigating={navigation_engine.is_navigating}")

            if not navigation_engine.is_navigating:
                print(f"[GPS←小助手] 当前没有进行中的导航，忽略GPS数据")
                return

            # ★ 处理 heading（加权中值滤波）
            # 新值权重更高：最新值权重3，次新值权重2，旧值权重1
            # 转弯时约1秒响应，噪声时能抑制小幅抖动
            heading_val = None
            if heading is not None:
                try:
                    raw_heading = float(heading) % 360
                    # ★ 线程安全：heading_buffer 操作放入锁保护
                    with self._lock:
                        if not hasattr(self, '_heading_buffer'):
                            self._heading_buffer = []
                        self._heading_buffer.append(raw_heading)
                        if len(self._heading_buffer) > 5:
                            self._heading_buffer.pop(0)

                        if len(self._heading_buffer) >= 3:
                            buf = list(self._heading_buffer)  # 拷贝，避免迭代时被修改
                            # 加权：最新值权重3，次新值权重2，旧值权重1
                            weighted = []
                            weights = [1, 1, 2, 2, 3]  # 对应 buf[0]..buf[4]
                            for i, h in enumerate(buf):
                                w = weights[i] if i < len(weights) else 1
                                for _ in range(w):
                                    weighted.append(h)

                            # 计算环形中位数
                            ref = weighted[len(weighted) // 2]
                            offsets = []
                            for h in weighted:
                                offset = h - ref
                                while offset > 180: offset -= 360
                                while offset < -180: offset += 360
                                offsets.append(offset)
                            offsets.sort()
                            heading_val = (ref + offsets[len(offsets) // 2]) % 360
                        else:
                            heading_val = raw_heading
                except (TypeError, ValueError):
                    pass

            # ★ GPS 坐标不变检测：如果坐标长时间不变，降低推送频率
            if self._last_gps_lon is not None and self._last_gps_lat is not None:
                lon_diff = abs(float(longitude) - self._last_gps_lon)
                lat_diff = abs(float(latitude) - self._last_gps_lat)
                # 经纬度变化小于 0.00002（约 2 米）视为没变
                # ★ 室外 GPS 精度 5-15m，两次定位漂移 1-3m 正常
                # 2m 阈值可以过滤掉 GPS 抖动，但不会误判为"坐标不变"
                if lon_diff < 0.00002 and lat_diff < 0.00002:
                    self._gps_stale_count += 1
                    if self._gps_stale_count >= self._gps_stale_threshold:
                        # GPS 连续 10 秒没变化，只每 5 次推送一次（约 5 秒一次）
                        # ★ 同时延长去重间隔（坐标不变时不需要频繁播报）
                        self._instruction_suppress_interval = 15.0
                        if self._gps_stale_count % 5 != 0:
                            print(f"[GPS←小助手] GPS 坐标不变（连续{self._gps_stale_count}次），跳过指令推送（但仍更新导航引擎）")
                            # ★ 修复：仍然更新导航引擎状态，只是不推送指令
                            # 之前直接 return 跳过了 navigation_engine.update_location()
                            # 导致坐标恢复变化时导航引擎状态严重滞后
                            with self._lock:
                                navigation_engine.update_location(
                                    float(longitude), float(latitude), heading_val, accuracy
                                )
                            return
                    else:
                        print(f"[GPS←小助手] GPS 坐标不变（连续{self._gps_stale_count}次）")
                else:
                    if self._gps_stale_count > 0:
                        print(f"[GPS←小助手] GPS 坐标已更新（之前连续{self._gps_stale_count}次不变）")
                        # ★ 坐标变化时缩短去重间隔（及时更新米数）
                        self._instruction_suppress_interval = 5.0
                    self._gps_stale_count = 0

            self._last_gps_lon = float(longitude)
            self._last_gps_lat = float(latitude)

            # 调用导航引擎更新位置
            # ★ 加锁防止多线程并发调用导致状态损坏
            with self._lock:
                result = navigation_engine.update_location(float(longitude), float(latitude), heading_val, accuracy)

            # ★ 计算heading偏差（当前朝向与路线方向的偏差）
            heading_deviation = None
            if heading_val is not None and hasattr(navigation_engine, '_steps'):
                try:
                    step_idx = result.get("current_step", 0)
                    if step_idx < len(navigation_engine._steps):
                        step = navigation_engine._steps[step_idx]
                        polyline_str = step.get("polyline", "")
                        if polyline_str:
                            pts = parse_polyline(polyline_str)
                            if len(pts) >= 2:
                                # ★ 使用平滑后的heading计算偏差（避免GPS震荡导致偏差计算不准）
                                use_heading = result.get("smoothed_heading", heading_val)
                                # 路线方向
                                route_bearing = bearing(pts[0][1], pts[0][0], pts[-1][1], pts[-1][0])
                                # heading偏差（带方向：正=右偏，负=左偏）
                                diff = use_heading - route_bearing
                                if diff > 180:
                                    diff -= 360
                                elif diff < -180:
                                    diff += 360
                                heading_deviation = diff  # 正值=偏右，负值=偏左
                except Exception as e:
                    pass

            # ★ crossing信息优先处理（优先级高于普通导航指令）
            crossing_warning = result.get("crossing_warning", "")
            crossing_state = result.get("crossing_state", "")
            has_crossing = bool(crossing_warning or crossing_state)
            if has_crossing:
                self.send_navigation_instruction(
                    crossing_warning or result.get("current_instruction", ""),
                    result["remaining_distance"],
                    crossing_state=crossing_state,
                    crossing_warning=crossing_warning,
                    heading_deviation=heading_deviation
                )

            # 推送导航指令给小助手 AI
            heading_changed = result.get("heading_changed", False)
            if result["arrived"]:
                self.send_navigation_instruction(
                    "您已到达目的地附近导航已结束", "0", arrived=True
                )
            elif result["current_instruction"] and not has_crossing:
                self.send_navigation_instruction(
                    result["current_instruction"],
                    result["remaining_distance"],
                    heading_changed=heading_changed,
                    current_step=result.get("current_step", 0),
                    total_steps=result.get("total_steps", 0),
                    crossing_state=result.get("crossing_state", ""),
                    crossing_warning=result.get("crossing_warning", ""),
                    heading_deviation=heading_deviation
                )
        except Exception as e:
            print(f"[GPS←小助手] 处理GPS数据异常: {e}")


# 全局 WebSocket 客户端实例
assistant_ws_client = AssistantWebSocketClient()


def create_app():
    """创建并配置Flask应用"""

    app = Flask(__name__)

    # 启用CORS跨域支持
    CORS(app)

    # 验证配置
    try:
        Config.validate()
    except ValueError as e:
        print(f"[配置错误] {e}")

    # 统一响应格式辅助函数
    def success_response(data=None, message="success"):
        resp = {"success": True, "message": message}
        if data is not None:
            resp["data"] = data
        return jsonify(resp)

    def error_response(error_msg, status_code=400):
        return jsonify({
            "success": False,
            "error": error_msg
        }), status_code

    # ==================== 导航 REST 接口 ====================

    @app.route("/api/navigation/start", methods=["POST"])
    def start_navigation():
        """
        开始导航（REST API，供小助手 AI 调用）
        请求体: {"origin": {"longitude": 116.48, "latitude": 39.99}, "destination": "北京市朝阳区xxx"}
        """
        if not Config.AMAP_API_KEY:
            return error_response("服务端未配置高德地图API Key", 500)

        data = request.get_json(silent=True)
        print(f"[调试] 收到导航请求: Content-Type={request.content_type}, data={data}")
        if not data:
            return error_response("请求体不能为空，请发送JSON格式数据")

        origin = data.get("origin")
        if not origin:
            return error_response("缺少起点坐标 origin")

        try:
            origin_lon = float(origin.get("longitude", 0))
            origin_lat = float(origin.get("latitude", 0))
        except (TypeError, ValueError):
            return error_response("起点坐标格式错误，longitude和latitude必须为数字")

        destination = data.get("destination")
        if not destination or not destination.strip():
            return error_response("目的地地址不能为空")

        route = navigation_engine.start_navigation(
            origin_lon, origin_lat, destination.strip()
        )

        # ★ 诊断日志：打印进程ID，确认是否多进程问题
        import os
        print(f"[GPS API] PID={os.getpid()} 导航启动结果: {route is not None}, is_navigating={navigation_engine.is_navigating}")

        # ★ 新导航开始时重置去重状态（防止上一次导航的残留影响）
        if assistant_ws_client:
            assistant_ws_client._last_instruction = ""
            assistant_ws_client._last_instruction_time = 0.0
            assistant_ws_client._instruction_suppress_interval = 5.0
            assistant_ws_client._gps_stale_count = 0
            assistant_ws_client._last_gps_lon = None
            assistant_ws_client._last_gps_lat = None
            assistant_ws_client._heading_buffer = []

        if not route:
            return error_response("导航启动失败，可能是目的地地址无法识别或无法规划步行路线")

        first_instruction = ""
        if route.get("steps"):
            first_instruction = route["steps"][0].get("instruction", "开始导航")

        return success_response({
            "route": {
                "distance": route.get("distance", "0"),
                "duration": route.get("duration", "0"),
                "steps": [
                    {
                        "instruction": s.get("instruction", ""),
                        "action": s.get("action", ""),
                        "distance": s.get("distance", "0"),
                        "duration": s.get("duration", "0"),
                        "road": s.get("road", ""),
                        "orientation": s.get("orientation", ""),
                        "polyline": s.get("polyline", "")
                    }
                    for s in route.get("steps", [])
                ]
            },
            "first_instruction": first_instruction
        })

    @app.route("/api/navigation/update", methods=["POST"])
    def update_location():
        """
        更新用户位置（App 调用）
        App 只上传 GPS+指南针数据，不接收返回。
        GPS AI 计算导航指令后，主动推送给小助手 AI。
        """
        data = request.get_json(silent=True)
        if not data:
            return error_response("请求体不能为空，请发送JSON格式数据")

        try:
            current_lon = float(data.get("longitude", 0))
            current_lat = float(data.get("latitude", 0))
        except (TypeError, ValueError):
            return error_response("坐标格式错误，longitude和latitude必须为数字")

        heading = None
        if data.get("heading") is not None:
            try:
                heading = float(data["heading"])
                heading = heading % 360
            except (TypeError, ValueError):
                pass

        print(f"[App上传] 位置: ({current_lon:.6f}, {current_lat:.6f}) heading={heading}")

        if not navigation_engine.is_navigating:
            print(f"[App上传] 当前没有进行中的导航，忽略")
            return success_response(message="当前没有进行中的导航")

        result = navigation_engine.update_location(current_lon, current_lat, heading)

        # 推送导航指令给小助手 AI（不返回给 App）
        if result["arrived"]:
            assistant_ws_client.send_navigation_instruction(
                "您已到达目的地附近导航已结束", "0", arrived=True
            )
        elif result["current_instruction"]:
            assistant_ws_client.send_navigation_instruction(
                result["current_instruction"],
                result["remaining_distance"],
                current_step=result.get("current_step", 0),
                total_steps=result.get("total_steps", 0)
            )

        # App 不需要返回详细指令，只返回成功状态
        return success_response(message="位置已更新")

    @app.route("/api/navigation/stop", methods=["POST"])
    def stop_navigation():
        """停止导航"""
        navigation_engine.stop_navigation()
        return success_response(message="导航已停止")

    @app.route("/api/navigation/status", methods=["GET"])
    def get_navigation_status():
        """获取当前导航状态"""
        status = navigation_engine.get_status()
        return success_response(status)

    @app.route("/api/navigation/instruction", methods=["GET"])
    def get_current_instruction():
        """获取当前导航指令（不更新位置，用于导航中用户问路）"""
        instruction = navigation_engine.get_current_instruction()
        return success_response({"instruction": instruction})

    # ==================== 地理编码接口 ====================

    @app.route("/api/geocode", methods=["POST"])
    def geocode():
        """地理编码（独立接口）"""
        if not Config.AMAP_API_KEY:
            return error_response("服务端未配置高德地图API Key", 500)

        data = request.get_json(silent=True)
        if not data:
            return error_response("请求体不能为空，请发送JSON格式数据")

        address = data.get("address")
        if not address or not address.strip():
            return error_response("地址不能为空")

        result = amap_service.geocode(address.strip())

        if not result:
            return error_response(f"无法识别地址: {address}")

        return success_response({
            "location": {
                "longitude": result["longitude"],
                "latitude": result["latitude"]
            },
            "formatted_address": result["formatted_address"]
        })

    # ==================== 错误处理 ====================

    @app.errorhandler(404)
    def not_found(error):
        return error_response("请求的资源不存在", 404)

    @app.errorhandler(405)
    def method_not_allowed(error):
        return error_response("请求方法不允许", 405)

    @app.errorhandler(500)
    def internal_error(error):
        return error_response("服务器内部错误", 500)

    @app.errorhandler(Exception)
    def handle_exception(error):
        print(f"[未捕获异常] {type(error).__name__}: {error}")
        return error_response("服务器内部错误", 500)

    return app


# 创建应用实例
app = create_app()


if __name__ == "__main__":
    print("=" * 55)
    print("  明途导航服务端 v2.0 ★ POI过滤+指令接口")
    print("  盲人步行导航辅助系统")
    print("=" * 55)

    if not Config.AMAP_API_KEY:
        print("\n[警告] 未设置环境变量 AMAP_API_KEY")
        print("地理编码和路径规划功能将不可用")
        print("请设置后重启服务\n")

    print(f"REST API:   http://{Config.HOST}:{Config.PORT}")
    print(f"小助手 AI:  ws://127.0.0.1:8767 (GPS AI → 小助手 AI)")
    print()
    print("REST API 接口:")
    print("  POST /api/navigation/start  - 开始导航")
    print("  POST /api/navigation/update  - 更新位置（App上传GPS）")
    print("  POST /api/navigation/stop   - 停止导航")
    print("  GET  /api/navigation/status  - 导航状态")
    print("  POST /api/geocode            - 地理编码")
    print()
    print("数据流:")
    print("  App ──(GPS+指南针)──→ 小助手 AI ──(WebSocket转发)──→ GPS AI")
    print("  GPS AI ──(导航指令)──→ 小助手 AI ──→ App")
    print("=" * 55)

    # 启动 GPS AI → 小助手 AI 的 WebSocket 客户端
    assistant_ws_client.start()

    try:
        app.run(
            host=Config.HOST,
            port=Config.PORT,
            debug=Config.DEBUG
        )
    finally:
        assistant_ws_client.stop()
