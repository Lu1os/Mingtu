package com.lu1os.mingtuapp

import android.content.Context
import android.util.Log
import com.amap.api.location.AMapLocation
import com.amap.api.location.AMapLocationClient
import com.amap.api.location.AMapLocationClientOption
import com.amap.api.location.AMapLocationListener
import com.amap.api.location.AMapLocationQualityReport

/**
 * 高德定位 SDK 封装
 *
 * 替代原生 LocationManager，优势：
 * - 融合定位（GPS + Wi-Fi + 基站 + 传感器），精度更高
 * - 直接返回 GCJ02 坐标，不需要 WGS84→GCJ02 转换
 * - 内置传感器辅助，步行导航方向更准
 * - 首次定位更快（1-3 秒 vs 原生 5-30 秒）
 */
class AmapLocationHelper(private val context: Context) {

    companion object {
        private const val TAG = "AmapLocation"
    }

    private var locationClient: AMapLocationClient? = null
    private var locationOption: AMapLocationClientOption? = null
    private var callback: LocationCallback? = null
    private var isRunning = false

    /**
     * 定位结果回调
     * @param longitude GCJ02 经度（高德坐标系，不需要转换）
     * @param latitude GCJ02 纬度
     * @param accuracy 精度（米）
     * @param bearing 朝向（度，0=北，90=东，180=南，270=西）
     * @param provider 定位来源："gps" 或 "lbs"（网络）
     * @param speed 速度（米/秒）
     */
    interface LocationCallback {
        fun onLocationChanged(
            longitude: Double,
            latitude: Double,
            accuracy: Float,
            bearing: Float,
            provider: String,
            speed: Float
        )
    }

    /**
     * 启动定位
     *
     * @param intervalMs 定位间隔（毫秒），导航中建议 1000，非导航建议 3000
     * @param callback 定位结果回调
     */
    fun startLocation(intervalMs: Long = 1000, callback: LocationCallback) {
        this.callback = callback

        try {
            // ★★★ 高德 SDK V11 隐私合规（必须在所有 SDK 接口调用之前） ★★★
            // 不设置会导致定位回调永远不触发，errorCode=555570
            AMapLocationClient.updatePrivacyShow(context, true, true)
            AMapLocationClient.updatePrivacyAgree(context, true)

            // ★ 调试：打印当前 APK 签名的 SHA1（用于排查高德 auth fail）
            try {
                val pm = context.packageManager
                val info = pm.getPackageInfo(context.packageName, android.content.pm.PackageManager.GET_SIGNATURES)
                val sig = info.signatures!![0]
                val digest = java.security.MessageDigest.getInstance("SHA1")
                val hash = digest.digest(sig.toByteArray())
                val sha1Hex = hash.joinToString(":") { "%02X".format(it) }
                Log.d(TAG, "当前 APK 签名 SHA1: $sha1Hex")
            } catch (e: Exception) {
                Log.w(TAG, "获取签名 SHA1 失败: ${e.message}")
            }

            // 销毁旧的客户端（防止重复创建）
            stopLocation()

            locationClient = AMapLocationClient(context)
            locationOption = AMapLocationClientOption().apply {
                // ★ 高精度模式（GPS + 网络 + 传感器融合）
                locationMode = AMapLocationClientOption.AMapLocationMode.Hight_Accuracy

                // ★ 定位间隔
                interval = intervalMs

                // ★ 连续定位（不是单次）
                isOnceLocation = false

                // ★ 不需要地址信息（省流量，导航只需要坐标）
                isNeedAddress = false

                // ★ 开启传感器辅助定位（用加速度计+陀螺仪辅助判断移动方向和速度）
                // 这对步行导航精度提升很大，尤其是 GPS 信号弱的场景
                isSensorEnable = true

                // ★ GPS 优先（室外精度更高，且GPS是纯本地的不需要网络认证）
                isGpsFirst = true

                // ★ 网络超时
                httpTimeOut = 10000

                // ★ GPS 超时（如果 10 秒内拿不到 GPS，再用网络定位）
                gpsFirstTimeout = 10000
            }

            locationClient?.setLocationOption(locationOption)
            locationClient?.setLocationListener(locationListener)
            locationClient?.startLocation()
            isRunning = true

            Log.d(TAG, "高德定位已启动（间隔${intervalMs}ms，高精度+传感器辅助）")
        } catch (e: Exception) {
            Log.e(TAG, "高德定位启动失败: ${e.message}")
        }
    }

    /**
     * 停止定位
     */
    fun stopLocation() {
        try {
            locationClient?.stopLocation()
            locationClient?.onDestroy()
        } catch (e: Exception) {
            Log.w(TAG, "停止高德定位异常: ${e.message}")
        }
        locationClient = null
        locationOption = null
        isRunning = false
    }

    /**
     * 切换定位间隔（导航开始/结束时调用，避免重建客户端）
     *
     * @param intervalMs 新的定位间隔
     */
    fun setInterval(intervalMs: Long) {
        if (!isRunning) return
        try {
            locationOption?.interval = intervalMs
            locationClient?.setLocationOption(locationOption)
            Log.d(TAG, "定位间隔已切换为 ${intervalMs}ms")
        } catch (e: Exception) {
            Log.w(TAG, "切换定位间隔失败: ${e.message}")
        }
    }

    /**
     * 是否正在定位
     */
    fun isRunning(): Boolean = isRunning

    private val locationListener = AMapLocationListener { location ->
        if (location == null) {
            Log.w(TAG, "定位回调为 null")
            return@AMapLocationListener
        }

        if (location.errorCode == 0) {
            // ★ 定位成功
            // 高德 SDK 直接返回 GCJ02 坐标，不需要 WGS84→GCJ02 转换！
            val lon = location.longitude
            val lat = location.latitude
            val accuracy = location.accuracy  // 精度（米）
            val bearing = location.bearing.toFloat()  // 朝向（度）
            val provider = if (location.locationType == AMapLocation.LOCATION_TYPE_GPS) "gps" else "lbs"
            val speed = location.speed.toFloat()  // 速度（米/秒）

            Log.d(TAG, "定位更新: (%.6f, %.6f) accuracy=%.0fm bearing=%.0f° provider=%s speed=%.1fm/s".format(
                lon, lat, accuracy, bearing, provider, speed
            ))

            callback?.onLocationChanged(lon, lat, accuracy, bearing, provider, speed)
        } else {
            // ★ 定位失败
            Log.w(TAG, "定位失败: errorCode=${location.errorCode} errorInfo=${location.errorInfo} locationType=${location.locationType}")
        }
    }
}
