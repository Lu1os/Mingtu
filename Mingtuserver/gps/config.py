# -*- coding: utf-8 -*-
"""
配置模块
从环境变量读取配置信息
"""

import os


class Config:
    """应用配置类"""

    # 高德地图API Key
    # 优先从环境变量读取，如果没有则使用默认值（与小助手 AI 共用同一个 key）
    # ★ strip() 去除首尾空白字符（防止 bat 文件中 set 命令带入不可见空格）
    AMAP_API_KEY = os.environ.get("AMAP_API_KEY", "").strip()

    # 服务端配置
    HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
    PORT = int(os.environ.get("SERVER_PORT", "5000"))
    DEBUG = os.environ.get("SERVER_DEBUG", "false").lower() == "true"

    # 高德地图API基础地址
    AMAP_GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
    AMAP_WALKING_URL = "https://restapi.amap.com/v3/direction/walking"
    AMAP_POI_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
    AMAP_POI_AROUND_URL = "https://restapi.amap.com/v3/place/around"

    # 导航相关阈值配置
    # 接近转折点的提前提示距离（米）
    TURN_APPROACH_DISTANCE = 30
    # 到达终点的判定距离（米）
    ARRIVAL_DISTANCE = 20
    # 下一个step转折点的提示距离（米）
    NEXT_STEP_APPROACH_DISTANCE = 20

    @classmethod
    def validate(cls):
        """验证必要配置是否完整，不完整则抛出异常"""
        # 诊断日志：启动时打印 key 前10后4位，方便排查"key没读到"的问题
        if cls.AMAP_API_KEY:
            print(f"[配置] 高德 API Key 已加载: {cls.AMAP_API_KEY[:8]}...{cls.AMAP_API_KEY[-4:]}")
        else:
            print("[配置] 高德 API Key 为空！请检查环境变量或 run.bat 中的配置")
        if not cls.AMAP_API_KEY:
            raise ValueError(
                "错误：未设置环境变量 AMAP_API_KEY。\n"
                "请通过以下方式设置：\n"
                "  Linux/Mac: export AMAP_API_KEY='your_key_here'\n"
                "  Windows:   set AMAP_API_KEY=your_key_here\n"
                "或使用 run.bat 启动（推荐）"
            )
