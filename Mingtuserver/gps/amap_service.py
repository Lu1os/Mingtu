# -*- coding: utf-8 -*-
"""
高德地图API封装模块
提供地理编码和步行路径规划功能
"""

import requests
from config import Config


class AmapService:
    """高德地图Web服务API封装"""

    def __init__(self):
        self.api_key = Config.AMAP_API_KEY
        self.geocode_url = Config.AMAP_GEOCODE_URL
        self.walking_url = Config.AMAP_WALKING_URL
        self.poi_search_url = Config.AMAP_POI_SEARCH_URL

    def _make_request(self, url, params):
        """
        发送HTTP请求到高德API的通用方法
        """
        try:
            # ★ 调试：打印实际使用的 API Key（前8后4位）
            actual_key = params.get("key", "")
            if actual_key:
                print(f"[高德API] 使用 Key: {actual_key[:8]}...{actual_key[-4:]} (长度={len(actual_key)})")
            else:
                print(f"[高德API] ⚠️ Key 为空！params={list(params.keys())}")

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            # 高德API status="1"表示成功
            if data.get("status") != "1":
                error_info = data.get("info", "未知错误")
                print(f"[高德API错误] {error_info}")
                return None

            return data

        except requests.exceptions.Timeout:
            print("[高德API错误] 请求超时")
            return None
        except requests.exceptions.ConnectionError:
            print("[高德API错误] 网络连接失败")
            return None
        except requests.exceptions.RequestException as e:
            print(f"[高德API错误] 请求异常: {e}")
            return None
        except ValueError:
            print("[高德API错误] 返回数据解析失败")
            return None

    def geocode(self, address):
        """
        地理编码：将地址文本转换为经纬度坐标

        Args:
            address: 地址文本，如"北京市朝阳区望京SOHO"

        Returns:
            dict: 包含 longitude, latitude, formatted_address 的字典
                  找不到时返回 None
        """
        if not address or not address.strip():
            print("[地理编码] 地址不能为空")
            return None

        params = {
            "key": self.api_key,
            "address": address.strip(),
            "output": "JSON"
        }

        data = self._make_request(self.geocode_url, params)
        if not data:
            return None

        geocodes = data.get("geocodes", [])
        if not geocodes:
            print(f"[地理编码] 未找到地址: {address}")
            return None

        # 取第一个匹配结果
        geo = geocodes[0]
        location = geo.get("location", "")  # 格式: "经度,纬度"

        if not location:
            print(f"[地理编码] 地址无坐标信息: {address}")
            return None

        try:
            lon, lat = location.split(",")
            result = {
                "longitude": lon,
                "latitude": lat,
                "formatted_address": geo.get("formatted_address", address)
            }
            print(f"[地理编码] {address} -> ({lon}, {lat})")
            return result

        except (ValueError, AttributeError):
            print(f"[地理编码] 坐标格式解析失败: {location}")
            return None

    def plan_walking_route(self, origin_lon, origin_lat, dest_lon, dest_lat):
        """
        步行路径规划：根据起止坐标规划步行路线

        Args:
            origin_lon: 起点经度
            origin_lat: 起点纬度
            dest_lon: 终点经度
            dest_lat: 终点纬度

        Returns:
            dict: 包含 distance, duration, steps 的路线信息字典
                  失败时返回 None
        """
        origin = f"{origin_lon},{origin_lat}"
        destination = f"{dest_lon},{dest_lat}"

        params = {
            "key": self.api_key,
            "origin": origin,
            "destination": destination,
            "output": "JSON"
        }

        data = self._make_request(self.walking_url, params)
        if not data:
            return None

        route = data.get("route", {})
        paths = route.get("paths", [])

        if not paths:
            print("[路径规划] 未找到可行路线")
            return None

        # 取第一条路线
        path = paths[0]
        steps = []

        for step in path.get("steps", []):
            step_info = {
                "instruction": step.get("instruction", ""),
                "action": step.get("action", ""),
                "distance": step.get("distance", "0"),
                "duration": step.get("duration", "0"),
                "road": step.get("road", ""),
                "orientation": step.get("orientation", ""),
                # 保存polyline坐标点序列，用于后续定位
                "polyline": step.get("polyline", "")
            }
            steps.append(step_info)

        result = {
            "distance": path.get("distance", "0"),
            "duration": path.get("duration", "0"),
            "steps": steps
        }

        print(f"[路径规划] 总距离: {result['distance']}米, 步骤数: {len(steps)}")
        return result


    def search_poi(self, keywords, city=None):
        """
        POI关键字搜索：根据关键字搜索兴趣点

        当地理编码返回的坐标无法规划步行路线时（如学校、公园内部），
        可以用POI搜索找到该地点的入口坐标作为导航终点。

        Args:
            keywords: 搜索关键字，如"山西大学"
            city: 限定城市，如"太原"（可选）

        Returns:
            list: POI列表，每个元素为字典，包含：
                  - name: POI名称
                  - address: 地址
                  - longitude: 经度
                  - latitude: 纬度
                  - entrance_longitude: 入口经度（可能为None）
                  - entrance_latitude: 入口纬度（可能为None）
                  失败时返回空列表
        """
        if not keywords or not keywords.strip():
            print("[POI搜索] 关键字不能为空")
            return []

        params = {
            "key": self.api_key,
            "keywords": keywords.strip(),
            "offset": "5",
            "page": "1",
            "extensions": "all",
            "output": "JSON"
        }

        # 如果指定了城市，加入城市参数
        if city and city.strip():
            params["city"] = city.strip()
            params["citylimit"] = "true"

        data = self._make_request(self.poi_search_url, params)
        if not data:
            return []

        pois = data.get("pois", [])
        if not pois:
            print(f"[POI搜索] 未找到结果: {keywords}")
            return []

        results = []
        for poi in pois:
            location = poi.get("location", "")
            if not location:
                continue

            # ★ 优化：过滤子建筑/内部设施/无关类型
            # 只保留名称与搜索关键词高度相关的结果
            poi_name = poi.get("name", "")
            exclude_keywords = [
                # 内部设施
                "家属区", "家属院", "宿舍", "食堂", "停车场",
                "内部", "办公楼", "公寓", "小区",
                # 子院系/研究所
                "学院", "系", "研究所", "研究院", "实验室",
                # 具体建筑（不是用户想去的整体）
                "图书馆", "体育馆", "游泳馆", "医院",
                # 商业无关
                "超市", "便利店", "银行", "邮局",
                # 快递/驿站/协会/社团等无关类型
                "菜鸟驿站", "快递", "驿站", "快递柜",
                "协会", "学会", "社团", "联合会", "工会",
                "服务中心", "服务站", "服务点",
                "健身操", "舞蹈", "合唱", "乐队",
                "后勤", "保卫", "物业", "维修",
                # 社区/文体/广场等非目标地点
                "社区", "文体广场", "文化广场", "活动中心",
                "学术交流中心",
            ]
            excluded = False
            for kw in exclude_keywords:
                if kw in poi_name:
                    excluded = True
                    break
            if excluded:
                continue

            # ★ 蹭地名检测：名称中包含商业关键词，直接排除
            import re
            is_piggyback = False
            commercial_keywords = [
                "酒店", "宾馆", "旅馆", "招待所", "民宿",
                "网吧", "KTV", "酒吧", "烧烤", "火锅", "奶茶", "咖啡",
                "理发", "美容", "足浴", "洗浴", "台球", "登崇阁",
            ]
            for ck in commercial_keywords:
                if ck in poi_name:
                    is_piggyback = True
                    break
            if not is_piggyback:
                if re.search(r'[（(].*' + re.escape(keywords.strip()) + r'.*[）)]', poi_name):
                    is_piggyback = True
            if is_piggyback:
                continue

            try:
                lon, lat = location.split(",")
            except (ValueError, AttributeError):
                continue

            # 尝试获取入口坐标（entr_location字段）
            entrance_lon = None
            entrance_lat = None
            entr_location = poi.get("entr_location", "")
            if entr_location:
                try:
                    entrance_lon, entrance_lat = entr_location.split(",")
                except (ValueError, AttributeError):
                    pass

            results.append({
                "name": poi.get("name", ""),
                "address": poi.get("address", ""),
                "longitude": lon,
                "latitude": lat,
                "entrance_longitude": entrance_lon,
                "entrance_latitude": entrance_lat
            })

        print(f"[POI搜索] '{keywords}' 找到 {len(results)} 个结果")
        for r in results:
            entr = f", 入口({r['entrance_longitude']},{r['entrance_latitude']})" if r['entrance_longitude'] else ""
            print(f"  - {r['name']}: ({r['longitude']},{r['latitude']}){entr}")

        return results


# 创建全局实例，方便其他模块直接使用
amap_service = AmapService()
