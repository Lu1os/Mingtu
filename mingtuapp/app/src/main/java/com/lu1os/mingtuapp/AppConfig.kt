package com.lu1os.mingtuapp

import android.content.Context
import android.content.SharedPreferences

/**
 * 全局配置文件
 * 所有服务器地址统一管理
 *
 * ★ IP 地址从 SharedPreferences 读取，可在设置页面修改
 * ★ 默认 IP 为空，首次使用需在设置中输入电脑 IP
 */
object AppConfig {

    private const val PREFS_NAME = "mingtu_settings"
    private const val KEY_SERVER_IP = "server_ip"

    /** 默认 IP（仅在用户未设置时使用） */
    const val DEFAULT_SERVER_IP = "127.0.0.1"

    // --- 端口号（固定不变）---
    const val ASSISTANT_WS_PORT = 8766   // 小助手 AI WebSocket 端口
    const val VISION_WS_PORT = 8765      // 视觉 AI WebSocket 端口
    const val GPS_HTTP_PORT = 5000       // GPS AI HTTP 端口

    // ==================== 动态 IP 管理 ====================

    /**
     * 获取当前服务器 IP 地址
     * 优先从 SharedPreferences 读取，未设置时返回默认值
     */
    fun getServerIp(context: Context): String {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getString(KEY_SERVER_IP, DEFAULT_SERVER_IP) ?: DEFAULT_SERVER_IP
    }

    /**
     * 保存服务器 IP 地址到 SharedPreferences
     */
    fun setServerIp(context: Context, ip: String) {
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_SERVER_IP, ip)
            .apply()
    }

    /**
     * 获取完整的小助手 AI WebSocket 地址
     */
    fun getAssistantWsUrl(context: Context): String {
        return "ws://${getServerIp(context)}:$ASSISTANT_WS_PORT"
    }

    /**
     * 获取完整的视觉 AI WebSocket 地址
     */
    fun getVisionWsUrl(context: Context): String {
        return "ws://${getServerIp(context)}:$VISION_WS_PORT"
    }

    /**
     * 获取完整的 GPS AI HTTP 地址
     */
    fun getGpsHttpUrl(context: Context): String {
        return "http://${getServerIp(context)}:$GPS_HTTP_PORT"
    }
}
