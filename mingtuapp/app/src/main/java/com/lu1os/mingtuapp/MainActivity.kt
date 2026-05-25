package com.lu1os.mingtuapp

import android.Manifest
import android.content.Context
import android.content.Intent
import com.lu1os.mingtuapp.view.ListeningOrbView
import android.content.pm.PackageManager
import android.hardware.Sensor
import android.hardware.SensorEvent
import android.hardware.SensorEventListener
import android.hardware.SensorManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.view.KeyEvent
import android.view.View
import android.widget.ImageView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import com.google.common.util.concurrent.ListenableFuture
import org.json.JSONObject
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets
import kotlin.concurrent.thread
import java.util.concurrent.TimeUnit


class MainActivity : AppCompatActivity(),
    SpeechManager.SpeechCallback,
    SpeechTranscriber.TranscriptionCallback {

    private lateinit var viewFinder: androidx.camera.view.PreviewView
    private lateinit var voiceFeedbackToast: TextView
    private lateinit var listeningOrb: ListeningOrbView

    private lateinit var navHome: View
    private lateinit var navSOS: View
    private lateinit var navSettings: View

    private lateinit var ivHome: ImageView
    private lateinit var tvHome: TextView
    private lateinit var ivSOS: ImageView
    private lateinit var tvSOS: TextView
    private lateinit var ivSettings: ImageView
    private lateinit var tvSettings: TextView

    private val mainHandler = Handler(Looper.getMainLooper())
    // WebSocket 重连专用 Handler，不影响 mainHandler 上的其他任务（如开屏播报）
    private val reconnectHandler = Handler(Looper.getMainLooper())

    private lateinit var cameraProviderFuture: ListenableFuture<ProcessCameraProvider>
    private var isCameraProviderReady = false  // ★ 标记 addListener 回调是否已执行
    private var cameraPreview: Preview? = null  // ★ 保存 preview 引用，onResume 时重新绑定用

    // 小助手 AI WebSocket 客户端
    private var webSocketClient: OkHttpClientWebSocketClient? = null

    // ★ 服务器地址改为动态获取（从 SharedPreferences 读取用户设置的 IP）
    // 不再硬编码，每次连接时实时获取最新 IP
    private fun getWebSocketUrl(): String = AppConfig.getAssistantWsUrl(this)
    private fun getGpsAiUrl(): String = AppConfig.getGpsHttpUrl(this)

    // ==================== GPS & 传感器 ====================

    // ★ 高德定位 SDK
    private lateinit var amapLocationHelper: AmapLocationHelper
    private lateinit var sensorManager: SensorManager
    private var magnetometer: Sensor? = null
    private var gravitySensor: Sensor? = null

    // 当前 GPS 数据（高德 SDK 直接返回 GCJ02 坐标，不需要转换）
    private var currentLongitude: Double = 0.0
    private var currentLatitude: Double = 0.0
    private var currentHeading: Float = 0f
    private var gpsAvailable = false
    private var lastGpsAccuracy: Float = 0f

    // 传感器数据（用于计算指南针朝向）
    private var gravityValues = FloatArray(3)
    private var geomagneticValues = FloatArray(3)
    private var hasGravity = false
    private var hasGeomagnetic = false

    // ★ heading 中值滤波窗口：取最近 N 个值的中间值，抗室内磁干扰噪声
    private val headingMedianBuffer = java.util.LinkedList<Float>()
    private val HEADING_MEDIAN_WINDOW = 7  // 窗口大小（奇数，取中位数）

    // ==================== 导航状态 ====================

    private var isNavigating = false
    // ★ 唤醒闲聊标记：用户唤醒后说闲话期间，暂停导航/视觉消息播报
    private var isWakeupChatting = false
    private var isArrivalDetected = false  // ★ 防止本地到达检测重复触发
    private var navigationStartTime = 0L  // ★ 导航开始时间，用于延迟播报GPS信号弱
    private val gpsUpdateHandler = Handler(Looper.getMainLooper())
    private var gpsUpdateRunnable: Runnable? = null

    // ★ 新增：偏航检测
    private var currentDestination = ""          // 保存目的地，用于偏航重新规划
    private var routePoints: List<Pair<Double, Double>> = emptyList()  // 路线所有坐标点（用于偏航检测）
    private var isOffRouteAlerted = false        // 防止重复偏航提示

    // ★ 新增：GPS 信号监控
    private var weakGpsAlerted = false

    // ★ 新增：导航进度播报
    private var totalDistance = 0          // 总距离（米）
    private var lastProgressDistance = 0   // 上次播报时的剩余距离

    // ==================== 权限流程状态 ====================
    // 跟踪权限请求状态，确保所有权限就绪后才初始化功能
    private var cameraPermissionGranted = false
    private var audioPermissionGranted = false
    private var locationPermissionGranted = false

    companion object {
        private const val TAG = "MainActivity"
        private const val REQUEST_CODE_PERMISSIONS = 10
        private const val REQUEST_CODE_LOCATION = 11
        // 只需要运行时请求的危险权限
        // INTERNET 和 ACCESS_NETWORK_STATE 是普通权限，安装时自动授予，不需要请求
        private val REQUIRED_PERMISSIONS = arrayOf(
            Manifest.permission.CAMERA,
            Manifest.permission.RECORD_AUDIO
        )
        private val LOCATION_PERMISSIONS = arrayOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.ACCESS_COARSE_LOCATION
        )
        // 导航中 GPS 位置上传间隔（毫秒），3秒上传一次给 GPS AI
        private const val GPS_UPDATE_INTERVAL_MS = 3000L
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        initViews()
        initLocationAndSensors()

        TtsManager.init(this)

        SpeechTranscriber.init(this)
        SpeechTranscriber.setCallback(this)

        // ★★★ 权限流程优化 ★★★
        // 不再在 onCreate 中直接请求权限并 finish()
        // 改为：检查权限 → 缺少的权限一次性全部请求 → 等待用户在系统弹窗中确认
        // 全部就绪后才初始化功能，不会退到桌面
        requestAllNeededPermissions()

        setupNavigation()
        setNavSelected(0)

        // ★ 问题6修复：设置音量键双击直接聆听回调
        KeyHandler.onDoubleTapForListening = {
            mainHandler.post {
                if (isNavigating || true) {  // 始终允许双击直接聆听
                    Log.d(TAG, "音量键双击，直接进入聆听")
                    SpeechManager.stopWakeup()
                    TtsManager.speak("我在听") {
                        mainHandler.postDelayed({
                            showVoiceFeedback("正在聆听...")
                            SpeechTranscriber.startTranscription()
                        }, 800)
                    }
                }
            }
        }

        // 初始化按键手柄
        KeyHandler.init(this)
        KeyHandler.setMainCallback(mainKeyHandlerCallback)

        // ★ 问题4修复：设置TTS不可用时的通知回调
        TtsManager.onTtsUnavailable = {
            mainHandler.post {
                // 振动通知盲人
                val vibrator = getSystemService(Context.VIBRATOR_SERVICE) as? android.os.Vibrator
                if (vibrator?.hasVibrator() == true) {
                    if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                        vibrator.vibrate(android.os.VibrationEffect.createOneShot(200, android.os.VibrationEffect.DEFAULT_AMPLITUDE))
                    } else {
                        @Suppress("DEPRECATION")
                        vibrator.vibrate(200)
                    }
                }
                // 通过 Accessibility announce 通知屏幕阅读器用户
                try {
                    val am = getSystemService(Context.ACCESSIBILITY_SERVICE) as? android.view.accessibility.AccessibilityManager
                    if (am?.isEnabled == true) {
                        val event = android.view.accessibility.AccessibilityEvent.obtain(android.view.accessibility.AccessibilityEvent.TYPE_ANNOUNCEMENT)
                        event.text.add("语音引擎未就绪，请联网后重启应用")
                        am.sendAccessibilityEvent(event)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Accessibility announce 失败: ${e.message}")
                }
            }
        }
    }

    /**
     * ★★★ 权限流程优化 ★★★
     *
     * 旧逻辑的问题：
     * 1. onCreate 中请求权限，如果被拒绝直接 finish() 退到桌面
     * 2. 系统拉起权限弹窗时 Activity 可能已经 paused/destroyed
     * 3. 用户确认权限后重新进入 app，又触发新一轮权限请求 → 死循环
     *
     * 新逻辑：
     * 1. 在 onCreate 中统一检查所有需要的权限
     * 2. 缺少的权限一次性全部请求（合并为一个 requestPermissions 调用）
     * 3. 用户在系统弹窗中确认后，onRequestPermissionsResult 回调中处理结果
     * 4. 权限全部就绪后才初始化摄像头、语音唤醒等功能
     * 5. 如果用户拒绝权限，弹出对话框解释原因并提供"去设置"按钮，不退出 app
     */
    private fun requestAllNeededPermissions() {
        // 收集所有未授予的权限
        val missingPermissions = mutableListOf<String>()

        for (perm in REQUIRED_PERMISSIONS) {
            if (ContextCompat.checkSelfPermission(this, perm) != PackageManager.PERMISSION_GRANTED) {
                missingPermissions.add(perm)
            }
        }
        for (perm in LOCATION_PERMISSIONS) {
            if (ContextCompat.checkSelfPermission(this, perm) != PackageManager.PERMISSION_GRANTED) {
                missingPermissions.add(perm)
            }
        }

        if (missingPermissions.isEmpty()) {
            // 所有权限都已授予，直接初始化
            Log.d(TAG, "所有权限已授予，直接初始化")
            cameraPermissionGranted = true
            audioPermissionGranted = true
            locationPermissionGranted = true
            onAllPermissionsReady()
        } else {
            // 一次性请求所有缺少的权限
            // ActivityCompat.requestPermissions 会合并相同 requestCode 的权限
            // 系统会依次弹出权限弹窗，用户确认后 Activity 保持在前台
            Log.d(TAG, "需要请求 ${missingPermissions.size} 个权限: ${missingPermissions.joinToString()}")
            ActivityCompat.requestPermissions(
                this,
                missingPermissions.toTypedArray(),
                REQUEST_CODE_PERMISSIONS
            )
        }
    }

    /**
     * 所有权限就绪后初始化功能
     */
    private fun onAllPermissionsReady() {
        Log.d(TAG, "所有权限就绪，开始初始化功能")

        // ★ 启动高德定位（必须在权限就绪后才能调用）
        requestLocationUpdates()

        // 启动摄像头
        startCamera()

        // 延迟播报欢迎语（等 TTS 引擎就绪）
        mainHandler.postDelayed({
            if (TtsManager.isReady()) {
                TtsManager.speak("您好，我是小途，今天有什么可以帮您？")
                showVoiceFeedback("您好，我是小途，今天有什么可以帮您？")
            }
        }, 1500)
    }

    override fun onResume() {
        super.onResume()

        // 从设置页返回时，重置按键手柄到主页模式
        KeyHandler.enterMainNavMode()
        KeyHandler.setMainCallback(mainKeyHandlerCallback)

        try {
            Log.d(TAG, "onResume: 开始初始化语音功能")

            // ★★★ 权限检查：如果没有权限就不初始化语音功能 ★★★
            if (!allPermissionsGranted()) {
                Log.w(TAG, "必要权限未完全授予，跳过语音初始化")
                return
            }

            // ★★★ 从设置返回后自动恢复位置权限 ★★★
            // 用户从系统设置中授予位置权限后返回，onResume 会被触发
            // 此时需要自动启动定位
            if (!locationPermissionGranted && hasLocationPermissions()) {
                Log.d(TAG, "检测到位置权限已授予（可能从设置返回），自动启动定位")
                locationPermissionGranted = true
                requestLocationUpdates()
            }

            Log.d(TAG, "所有必要权限已授予")

            // ★★★ 恢复摄像头流（onPause 时停止了）★★★
            // ★ 只在 addListener 回调已执行（摄像头已就绪）时才恢复
            if (isCameraProviderReady && ::cameraProviderFuture.isInitialized) {
                Log.d(TAG, "onResume: 恢复摄像头流")
                try {
                    val provider = cameraProviderFuture.get()
                    if (provider != null) {
                        CameraStreamManager.init(this as LifecycleOwner, provider)
                        CameraStreamManager.start(this)  // 创建 ImageAnalysis
                        // ★ 重新绑定 preview + imageAnalysis（onPause 时 stop() 解绑了）
                        val imageAnalysis = CameraStreamManager.getImageAnalysis()
                        val preview = cameraPreview
                        if (preview != null && imageAnalysis != null) {
                            provider.bindToLifecycle(
                                this as LifecycleOwner,
                                CameraSelector.DEFAULT_BACK_CAMERA,
                                preview, imageAnalysis
                            )
                        } else if (preview != null) {
                            provider.bindToLifecycle(
                                this as LifecycleOwner,
                                CameraSelector.DEFAULT_BACK_CAMERA,
                                preview
                            )
                        }
                        Log.d(TAG, "onResume: 摄像头流已恢复")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "onResume: 恢复摄像头流失败: ${e.message}")
                }
            }

            // ★★★ 从设置返回后补启动摄像头和欢迎语 ★★★
            // 如果用户之前拒绝了权限，去设置中开启后返回，onResume 会被触发
            // 此时需要补启动之前因权限缺失而跳过的功能
            if (!::cameraProviderFuture.isInitialized) {
                Log.d(TAG, "摄像头未初始化（之前权限被拒绝），现在补启动")
                onAllPermissionsReady()
                return
            }

            if (!SpeechManager.isSdkReady()) {
                Log.d(TAG, "正在初始化SpeechManager...")
                SpeechManager.init(this)
                SpeechManager.setCallback(this@MainActivity)
                Log.d(TAG, "SpeechManager初始化调用完成")
            } else {
                SpeechManager.setCallback(this@MainActivity)
                Log.d(TAG, "SpeechManager已初始化")
            }

            if (SpeechManager.isSdkReady()) {
                Log.d(TAG, "启动唤醒监听...")
                SpeechManager.startWakeup()
                Log.d(TAG, "唤醒监听已启动")
            } else {
                Log.w(TAG, "唤醒SDK未就绪，跳过启动唤醒")
                showVoiceFeedback("语音唤醒未就绪，请检查唤醒词资源文件")
            }

            // 连接 AI 对话 WebSocket
            connectWebSocket()

            // 请求 GPS 定位（onPause 时停止了，需要恢复）
            requestLocationUpdates()

        } catch (e: Exception) {
            Log.e(TAG, "语音管理器初始化异常: ${e.message}")
            e.printStackTrace()
            runOnUiThread {
                Toast.makeText(this, "语音功能初始化失败: ${e.message}", Toast.LENGTH_SHORT).show()
            }
        }
    }

    override fun onPause() {
        super.onPause()
        SpeechManager.stopWakeup()
        SpeechTranscriber.stopTranscription()
        CameraStreamManager.stop()
        stopLocationUpdates()
    }

    override fun onDestroy() {
        super.onDestroy()
        stopNavigation()
        CameraStreamManager.release()
        SpeechManager.release()
        SpeechTranscriber.release()
        TtsManager.destroy()
        disconnectWebSocket()
        KeyHandler.setMainCallback(null)
        try {
            sensorManager.unregisterListener(sensorEventListener)
        } catch (e: Exception) {
            Log.w(TAG, "注销传感器异常: ${e.message}")
        }
    }

    // ==================== 按键手柄 ====================

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (KeyHandler.onKeyEvent(event)) {
            return true  // 拦截音量键
        }
        return super.dispatchKeyEvent(event)
    }

    private val mainKeyHandlerCallback = object : KeyHandler.KeyHandlerCallback {
        override fun onMainNavItemChanged(index: Int, name: String) {
            runOnUiThread {
                setNavSelected(index)
                showVoiceFeedback(name)
            }
        }

        override fun onMainNavConfirmed(index: Int, name: String) {
            runOnUiThread {
                setNavSelected(index)
                when (index) {
                    0 -> {
                        TtsManager.speak("首页")
                        showVoiceFeedback("首页")
                    }
                    1 -> {
                        TtsManager.speak("紧急求助功能开发中")
                        showVoiceFeedback("紧急求助功能开发中")
                    }
                    2 -> {
                        TtsManager.speak("设置")
                        showVoiceFeedback("设置")
                        val intent = Intent(this@MainActivity, SettingsActivity::class.java)
                        startActivity(intent)
                    }
                }
            }
        }

        override fun onSettingsNavItemChanged(index: Int, name: String) {
            // 主页不处理设置页事件
        }

        override fun onSettingsNavEnterControl(index: Int, name: String) {
            // 主页不处理设置页事件
        }

        override fun onSettingsNavExitControl() {
            // 主页不处理设置页事件
        }

        override fun onSettingsExit() {
            // 主页不处理设置页事件
        }

        override fun onSettingsControlAction(isIncrease: Boolean) {
            // 主页不处理设置页事件
        }
    }

    // ==================== GPS & 传感器初始化 ====================

    private fun initLocationAndSensors() {
        // ★ 初始化高德定位 SDK
        amapLocationHelper = AmapLocationHelper(this)

        sensorManager = getSystemService(Context.SENSOR_SERVICE) as SensorManager
        magnetometer = sensorManager.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD)
        gravitySensor = sensorManager.getDefaultSensor(Sensor.TYPE_GRAVITY)

        if (magnetometer != null && gravitySensor != null) {
            sensorManager.registerListener(
                sensorEventListener,
                gravitySensor,
                SensorManager.SENSOR_DELAY_NORMAL
            )
            sensorManager.registerListener(
                sensorEventListener,
                magnetometer,
                SensorManager.SENSOR_DELAY_NORMAL
            )
            Log.d(TAG, "指南针传感器已注册（TYPE_GRAVITY + TYPE_MAGNETIC_FIELD）")
        } else {
            Log.w(TAG, "设备缺少指南针传感器（磁力计或重力传感器）")
        }
    }

    private val sensorEventListener = object : SensorEventListener {
        override fun onSensorChanged(event: SensorEvent) {
            when (event.sensor.type) {
                Sensor.TYPE_GRAVITY -> {
                    System.arraycopy(event.values, 0, gravityValues, 0, 3)
                    hasGravity = true
                }
                Sensor.TYPE_MAGNETIC_FIELD -> {
                    System.arraycopy(event.values, 0, geomagneticValues, 0, 3)
                    hasGeomagnetic = true
                }
            }

            if (hasGravity && hasGeomagnetic) {
                val rotationMatrix = FloatArray(9)
                val orientationValues = FloatArray(3)

                val success = SensorManager.getRotationMatrix(
                    rotationMatrix, null, gravityValues, geomagneticValues
                )
                if (success) {
                    SensorManager.getOrientation(rotationMatrix, orientationValues)
                    var azimuth = Math.toDegrees(orientationValues[0].toDouble()).toFloat()
                    azimuth = (azimuth + 360) % 360

                    // ★ 中值滤波：将原始值放入窗口，取中位数作为当前朝向
                    // 室内指南针受磁干扰，原始值在 0°~360° 之间剧烈跳动
                    // 中值滤波能有效滤除突变噪声，保留真实朝向
                    synchronized(headingMedianBuffer) {
                        headingMedianBuffer.addLast(azimuth)
                        if (headingMedianBuffer.size > HEADING_MEDIAN_WINDOW) {
                            headingMedianBuffer.removeFirst()
                        }
                        if (headingMedianBuffer.size >= 3) {
                            // 计算环形中位数（处理 0°/360° 边界）
                            currentHeading = circularMedian(headingMedianBuffer)
                        } else {
                            currentHeading = azimuth  // 窗口未满时用原始值
                        }
                    }
                }
            }
        }

        override fun onAccuracyChanged(sensor: Sensor?, accuracy: Int) {}
    }

    // ==================== GPS 定位（高德 SDK + 原生后备） ====================

    /**
     * 启动定位（非导航模式，3秒间隔省电）
     */
    private fun requestLocationUpdates() {
        if (!hasLocationPermissions()) {
            ActivityCompat.requestPermissions(this, LOCATION_PERMISSIONS, REQUEST_CODE_LOCATION)
            return
        }

        // ★ 避免重复启动：如果已经在定位中，不重复调用（onCreate 和 onResume 都会调这个方法）
        if (amapLocationHelper.isRunning()) {
            Log.d(TAG, "高德定位已在运行，跳过重复启动")
            return
        }

        amapLocationHelper.startLocation(3000, object : AmapLocationHelper.LocationCallback {
            override fun onLocationChanged(
                longitude: Double, latitude: Double,
                accuracy: Float, bearing: Float,
                provider: String, speed: Float
            ) {
                handleLocationUpdate(longitude, latitude, accuracy, bearing, provider, speed)
            }
        })
    }

    /**
     * 停止定位
     */
    private fun stopLocationUpdates() {
        amapLocationHelper.stopLocation()
        Log.d(TAG, "定位已停止")
    }

    /**
     * ★ 统一处理定位结果（高德 SDK 回调）
     * 高德 SDK 直接返回 GCJ02 坐标，不需要 WGS84→GCJ02 转换！
     * 也不需要卡尔曼滤波，高德 SDK 内置融合算法！
     */
    private fun handleLocationUpdate(
        longitude: Double, latitude: Double,
        accuracy: Float, bearing: Float,
        provider: String, speed: Float
    ) {
        lastGpsAccuracy = accuracy
        currentLongitude = longitude
        currentLatitude = latitude
        gpsAvailable = true

        // 如果高德 SDK 返回了有效的朝向，优先使用（传感器辅助定位更准）
        if (bearing > 0 && bearing < 360) {
            currentHeading = bearing
        }
        // 否则保留指南针传感器的 currentHeading（由 sensorEventListener 更新）

        Log.d(TAG, "位置更新: (%.6f, %.6f) accuracy=%.0fm heading=%.0f° provider=%s".format(
            currentLongitude, currentLatitude, accuracy, currentHeading, provider
        ))
    }

    private fun hasLocationPermissions(): Boolean {
        return LOCATION_PERMISSIONS.all {
            ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
        }
    }

    /**
     * ★ 新增：计算两个经纬度之间的距离（米），用于卡尔曼滤波器重置判断
     */
    private fun haversineDistance(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val R = 6371000.0
        val dLat = Math.toRadians(lat2 - lat1)
        val dLon = Math.toRadians(lon2 - lon1)
        val a = (Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2)) *
                Math.sin(dLon / 2) * Math.sin(dLon / 2))
        val c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a))
        return R * c
    }

    /**
     * ★ 环形中位数计算：处理 0°/360° 边界
     * 将角度转换为单位圆上的点，取中位数对应的点，再转回角度
     */
    private fun circularMedian(angles: List<Float>): Float {
        if (angles.isEmpty()) return 0f
        if (angles.size == 1) return angles[0]

        // 将角度转为弧度
        val radians = angles.map { Math.toRadians(it.toDouble()) }
        // 转为单位圆上的 (x, y) 点
        val points = radians.map { Pair(Math.cos(it), Math.sin(it)) }

        // 找到使到所有点距离之和最小的点（近似：用角度中位数）
        // 简化方案：选一个参考角度，计算所有角度相对于它的偏移，取中位数
        val refAngle = radians[radians.size / 2]

        // 计算每个角度相对于参考角度的偏移（处理环绕）
        val offsets = radians.map { r ->
            var offset = r - refAngle
            // 归到 [-PI, PI]
            while (offset > Math.PI) offset -= 2 * Math.PI
            while (offset < -Math.PI) offset += 2 * Math.PI
            offset
        }.sorted()

        // 取中位数偏移
        val medianOffset = offsets[offsets.size / 2]
        val medianRad = refAngle + medianOffset

        return ((Math.toDegrees(medianRad) % 360) + 360).toFloat() % 360
    }

    /**
     * 发送 GPS 数据到小助手 AI
     * 唤醒时 / 检测到导航意图时调用（让小助手 AI 知道用户在哪）
     */
    private fun sendGpsUpdateToAssistant() {
        if (!gpsAvailable || currentLongitude == 0.0 && currentLatitude == 0.0) {
            Log.w(TAG, "GPS 数据不可用，跳过发送 (gpsAvailable=$gpsAvailable, lon=$currentLongitude, lat=$currentLatitude)")
            return
        }

        if (webSocketClient == null || !webSocketClient!!.isOpen()) {
            Log.w(TAG, "WebSocket 未连接，跳过 GPS 数据发送")
            return
        }

        try {
            val json = JSONObject()
            json.put("type", "gps_update")
            json.put("longitude", currentLongitude)
            json.put("latitude", currentLatitude)
            json.put("heading", currentHeading.toDouble())
            json.put("accuracy", lastGpsAccuracy.toDouble())

            webSocketClient?.send(json.toString())
            Log.d(TAG, "已发送 GPS 到小助手 AI: (%.10f, %.10f) accuracy=%.0fm".format(
                currentLongitude, currentLatitude, lastGpsAccuracy
            ))

        } catch (e: Exception) {
            Log.e(TAG, "发送 GPS 数据异常: ${e.message}")
        }
    }

    // ==================== 导航管理 ====================

    /**
     * 开始导航
     * 收到小助手 AI 的 navigation_started 消息后调用
     * App 直连 GPS AI，每 5 秒上传 GPS+指南针数据
     * GPS AI 返回导航指令 → App 转发给小助手 AI 排队播报
     */
    private fun startNavigation() {
        Log.d(TAG, "========================================")
        Log.d(TAG, "开始导航（App 直连 GPS AI）")
        Log.d(TAG, "========================================")

        isNavigating = true
        isArrivalDetected = false  // ★ 重置到达检测标志
        navigationStartTime = System.currentTimeMillis()  // ★ 记录导航开始时间

        // ★ 导航中切换为 1 秒高频定位
        amapLocationHelper.setInterval(1000)

        // ★ 导航中保持屏幕常亮（防止系统限制 GPS 更新频率）
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // ★ 修改：不再启动 App 端的 GPS 定时器
        // 改为依赖服务端轮询（每 2 秒发送 request_gps_update）
        // App 收到后立即回传 GPS 数据
        Log.d(TAG, "✅ 导航已启动，高德定位已切换为1秒高频模式")
    }

    /**
     * 停止导航
     */
    private fun stopNavigation() {
        if (!isNavigating) return

        Log.d(TAG, "停止导航")
        isNavigating = false

        // ★ 立即停止 TTS 播报，清空队列（防止导航结束后还播"往前走"）
        TtsManager.stop()

        gpsUpdateHandler.removeCallbacksAndMessages(null)
        gpsUpdateRunnable = null

        // ★ 新增：重置导航状态变量
        currentDestination = ""
        routePoints = emptyList()
        isOffRouteAlerted = false
        weakGpsAlerted = false
        totalDistance = 0
        lastProgressDistance = 0

        // 通知 GPS AI 停止导航
        notifyGpsAiStop()

        // 通知小助手 AI 导航已停止
        sendNavigationStoppedToAssistant()

        // ★ 导航结束后取消屏幕常亮
        window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        // 导航结束后恢复 3 秒低频定位（省电）
        requestLocationUpdates()

        // ★ 修复：导航停止后确保唤醒能恢复
        // 之前只在收到 navigation_stopped 消息时才恢复唤醒
        // 但本地到达检测（remainingM <= 15）直接调用 stopNavigation() 不会触发消息
        restartWakeupAfterDelay()

        Log.d(TAG, "导航已停止，已重启普通 GPS 监听")
    }

    // ==================== GPS AI 上传（HTTP REST，fire-and-forget）====================

    /**
     * 上传 GPS+指南针数据给 GPS AI（fire-and-forget）
     * App 只上传，不接收返回。
     * GPS AI 收到后计算导航指令，主动推送给小助手 AI。
     */
    private fun sendGpsUpdateToGpsAi() {
        if (!gpsAvailable || currentLongitude == 0.0 && currentLatitude == 0.0) {
            Log.w(TAG, "GPS 数据不可用，跳过 GPS AI 上传")
            return
        }

        thread {
            try {
                val requestBody = JSONObject().apply {
                    put("longitude", currentLongitude)
                    put("latitude", currentLatitude)
                    put("heading", currentHeading.toDouble())
                    put("accuracy", lastGpsAccuracy.toDouble())
                }

                val responseCode = postJson("${getGpsAiUrl()}/api/navigation/update", requestBody.toString())
                if (responseCode != null && responseCode in 200..299) {
                    Log.d(TAG, "✅ 已上传 GPS 到 GPS AI: (%.6f, %.6f) heading=%s HTTP=%d".format(
                        currentLongitude, currentLatitude,
                        "%.0f°".format(currentHeading),
                        responseCode
                    ))
                } else {
                    Log.e(TAG, "❌ GPS AI 上传失败: HTTP=%s URL=%s".format(
                        responseCode ?: "无响应", "${getGpsAiUrl()}/api/navigation/update"
                    ))
                }
            } catch (e: Exception) {
                Log.e(TAG, "❌ GPS AI 位置上传异常: ${e.message}")
            }
        }
    }

    /**
     * 通知 GPS AI 停止导航
     */
    private fun notifyGpsAiStop() {
        thread {
            try {
                postJson("${getGpsAiUrl()}/api/navigation/stop", "{}")
                Log.d(TAG, "已通知 GPS AI 停止导航")
            } catch (e: Exception) {
                Log.w(TAG, "通知 GPS AI 停止失败: ${e.message}")
            }
        }
    }

    /**
     * 发送 POST JSON 请求到 GPS AI
     * @return HTTP 响应码，失败返回 null
     */
    private fun postJson(urlString: String, jsonBody: String): Int? {
        var connection: HttpURLConnection? = null
        try {
            val url = URL(urlString)
            connection = url.openConnection() as HttpURLConnection
            connection.requestMethod = "POST"
            connection.connectTimeout = 5000
            connection.readTimeout = 5000
            connection.doOutput = true
            connection.doInput = true

            connection.setRequestProperty("Content-Type", "application/json; charset=UTF-8")
            connection.setRequestProperty("Accept", "application/json")

            val outputStream: OutputStream = connection.outputStream
            outputStream.write(jsonBody.toByteArray(StandardCharsets.UTF_8))
            outputStream.flush()
            outputStream.close()

            val responseCode = connection.responseCode
            // 读取响应体（不处理，fire-and-forget）
            try { connection.inputStream.close() } catch (_: Exception) {}
            return responseCode

        } catch (e: Exception) {
            Log.e(TAG, "HTTP 请求失败: ${e.message}")
            return null
        } finally {
            connection?.disconnect()
        }
    }

    // ==================== 通知小助手 AI ====================

    /**
     * 通知小助手 AI 导航已停止
     */
    private fun sendNavigationStoppedToAssistant() {
        if (webSocketClient == null || !webSocketClient!!.isOpen()) return

        try {
            val json = JSONObject()
            json.put("type", "navigation_stopped")
            webSocketClient?.send(json.toString())
            Log.d(TAG, "已通知小助手 AI 导航已停止")
        } catch (e: Exception) {
            Log.e(TAG, "通知导航停止异常: ${e.message}")
        }
    }

    // ==================== SpeechManager 回调（语音唤醒）====================

    override fun onWakeUp() {
        Log.d(TAG, "========================================")
        Log.d(TAG, "语音唤醒成功！")
        Log.d(TAG, "========================================")

        // ★ 强制停止 TTS 播报（清空队列 + 立即停止 AudioTrack + flush缓冲区）
        TtsManager.stop()
        TtsManager.flushAudioTrack()  // ★ flush 残余音频缓冲
        SpeechManager.stopWakeup()

        // ★ 标记唤醒闲聊状态：暂停导航/视觉消息播报
        isWakeupChatting = true

        // ★ 修复：唤醒时重新请求GPS定位
        requestLocationUpdates()
        sendGpsUpdateToAssistant()

        // ★ 延迟 500ms 等待 AudioTrack 完全停止，再说"我在听"
        // stop() 调用了 end() 但 AudioTrack 缓冲可能还有残余音频
        mainHandler.postDelayed({
            // ★ 再次确认停止（防止延迟期间有新消息入队）
            TtsManager.stop()
            // ★ 胶囊和"我在听"同时出现
            listeningOrb.setState(ListeningOrbView.State.LISTENING)
            TtsManager.speak("我在听") {
                mainHandler.postDelayed({
                    Log.d(TAG, "播报完成，启动实时转写...")
                    showVoiceFeedback("正在聆听...")
                    SpeechTranscriber.startTranscription()
                }, 800)
            }
            showVoiceFeedback("我在听")
        }, 500)
    }

    override fun onError(msg: String) {
        Log.e(TAG, "语音唤醒错误: $msg")
        runOnUiThread {
            Toast.makeText(this, "语音错误: $msg", Toast.LENGTH_SHORT).show()
        }
        restartWakeupAfterDelay()
    }

    override fun onSdkInitialized(success: Boolean) {
        Log.d(TAG, "唤醒SDK初始化回调: success=$success")
        if (success) {
            SpeechManager.startWakeup()
        } else {
            showVoiceFeedback("语音唤醒初始化失败")
        }
    }

    // ★ 唤醒检测时的音量回调（让波浪线在转写启动前也能跟声音变化）
    override fun onAudioLevel(level: Float) {
        runOnUiThread { listeningOrb.setAudioLevel(level) }
    }

    // ==================== SpeechTranscriber 回调（实时转写）====================

    override fun onTranscriptionResult(text: String, isFinal: Boolean) {
        Log.d(TAG, "转写结果: text=\"$text\", isFinal=$isFinal")

        if (isFinal && text.isNotEmpty()) {
            handleCommand(text)
        } else if (isFinal && text.isEmpty()) {
            // ★ 修复：VAD静默超时后返回空结果，自动停止转写并恢复唤醒
            // 之前空结果被忽略，isTranscribing仍为true，唤醒无法恢复
            // 要等15秒超时才能恢复，盲人用户等太久
            Log.d(TAG, "VAD静默超时，空结果，自动停止转写恢复唤醒")
            SpeechTranscriber.stopTranscription()
            restartWakeupAfterDelay()
        } else if (!isFinal && text.isNotEmpty()) {
            showVoiceFeedback(text)
        }
    }

    override fun onTranscriptionError(msg: String) {
        Log.e(TAG, "转写错误: $msg")
        runOnUiThread {
            Toast.makeText(this, "转写错误: $msg", Toast.LENGTH_SHORT).show()
            // ★ 问题14修复：转写失败时通过TTS播报提示盲人
            if (TtsManager.isReady()) {
                TtsManager.speak("网络不好，请再说一次") {
                    restartWakeupAfterDelay()
                }
            } else {
                restartWakeupAfterDelay()
            }
        }
    }

    override fun onTranscriptionStarted() {
        Log.d(TAG, "转写已开始")
        runOnUiThread {
            showVoiceFeedback("正在聆听...")
            // ★ 切换到说话波形模式
            listeningOrb.setState(ListeningOrbView.State.SPEAKING)
        }
    }

    override fun onTranscriptionStopped() {
        Log.d(TAG, "转写已停止")
        restartWakeupAfterDelay()
    }

    override fun onAudioLevelChanged(level: Float) {
        runOnUiThread { listeningOrb.setAudioLevel(level) }
    }

    private fun handleCommand(text: String) {
        Log.d(TAG, "处理指令: $text")

        SpeechTranscriber.stopTranscription()
        showVoiceFeedback("正在思考...")

        // ★ 切换到思考动画
        listeningOrb.setState(ListeningOrbView.State.THINKING)

        // 检测是否包含导航意图，如果是则先发送 GPS 数据
        if (isNavigationKeyword(text)) {
            sendGpsUpdateToAssistant()
        }

        sendCommandToBackend(text)
    }

    private fun isNavigationKeyword(text: String): Boolean {
        val keywords = arrayOf("去", "到", "导航", "前往", "带我", "怎么走", "路线", "找")
        return keywords.any { text.contains(it) }
    }

    // ★ 专用的唤醒重启 Runnable，用于精确取消，避免 removeCallbacksAndMessages(null) 误杀其他延迟任务
    private var restartWakeupRunnable: Runnable? = null

    private fun restartWakeupAfterDelay() {
        // 取消之前排队的唤醒重启任务（不影响其他延迟任务）
        restartWakeupRunnable?.let { mainHandler.removeCallbacks(it) }

        restartWakeupRunnable = Runnable {
            Log.d(TAG, "准备重新启动唤醒监听...")
            // ★ 恢复待机动画
            listeningOrb.setState(ListeningOrbView.State.IDLE)
            if (SpeechManager.isSdkReady() && !SpeechTranscriber.isTranscribing()) {
                // 讯飞 SDK 要求先停止唤醒引擎再重新启动，否则报 11201
                SpeechManager.stopWakeup()
                Log.d(TAG, ">>> 重新启动唤醒监听（先停止再启动）<<<")
                // 等待 300ms 让引擎完全停止
                mainHandler.postDelayed({
                    SpeechManager.startWakeup()
                }, 300)
            } else {
                Log.w(TAG, "无法启动唤醒，SDK未就绪或正在转写")
                // 500ms 后再试一次
                mainHandler.postDelayed({
                    if (SpeechManager.isSdkReady() && !SpeechTranscriber.isTranscribing()) {
                        SpeechManager.stopWakeup()
                        mainHandler.postDelayed({ SpeechManager.startWakeup() }, 300)
                    }
                }, 500)
            }
        }
        mainHandler.postDelayed(restartWakeupRunnable!!, 1500)
    }

    // ==================== 小助手 AI WebSocket ====================

    private fun connectWebSocket() {
        if (getWebSocketUrl().isEmpty()) return
        if (webSocketClient != null && webSocketClient!!.isOpen()) return

        disconnectWebSocket()

        try {
            Log.d(TAG, "连接小助手 AI: ${getWebSocketUrl()}")
            webSocketClient = OkHttpClientWebSocketClient(getWebSocketUrl(), object : OkHttpClientWebSocketClient.WebSocketListener {
                override fun onMessage(text: String) {
                    Log.d(TAG, "小助手 AI 收到消息: $text")
                    handleBackendMessage(text)
                }

                override fun onConnected() {
                    Log.d(TAG, "小助手 AI WebSocket 已连接")
                    sendGpsUpdateToAssistant()
                    // ★ 高德 SDK 首次定位可能需要 1-3 秒，连接时可能还没定位成功
                    // 延迟 3 秒再发一次，确保服务端能拿到 GPS 数据
                    mainHandler.postDelayed({ sendGpsUpdateToAssistant() }, 3000)
                    mainHandler.postDelayed({ sendGpsUpdateToAssistant() }, 6000)
                }

                override fun onDisconnected(code: Int, reason: String) {
                    Log.w(TAG, "小助手 AI WebSocket 断开: code=$code, reason=$reason")
                    // ★ 问题5修复：导航中WebSocket断连时，通过HTTP降级获取导航指令
                    if (isNavigating) {
                        Log.w(TAG, "导航中WebSocket断开，尝试HTTP降级获取导航指令")
                        thread {
                            try {
                                val url = URL("${getGpsAiUrl()}/instruction")
                                val conn = url.openConnection() as HttpURLConnection
                                conn.connectTimeout = 3000
                                conn.readTimeout = 3000
                                conn.requestMethod = "GET"
                                val responseCode = conn.responseCode
                                if (responseCode == 200) {
                                    val response = conn.inputStream.bufferedReader().readText()
                                    val json = JSONObject(response)
                                    val instruction = json.optString("instruction", "")
                                    if (instruction.isNotEmpty()) {
                                        mainHandler.post {
                                            TtsManager.speak(instruction) {
                                                // 导航中不重启唤醒
                                            }
                                        }
                                        Log.d(TAG, "HTTP降级获取导航指令成功: $instruction")
                                    }
                                } else {
                                    mainHandler.post {
                                        TtsManager.speak("网络断开，请沿当前方向继续行走") {
                                            // 导航中不重启唤醒
                                        }
                                    }
                                }
                                conn.disconnect()
                            } catch (e: Exception) {
                                Log.e(TAG, "HTTP降级失败: ${e.message}")
                                mainHandler.post {
                                    TtsManager.speak("网络断开，请沿当前方向继续行走") {
                                        // 导航中不重启唤醒
                                    }
                                }
                            }
                        }
                    }
                    scheduleReconnect()
                }

                override fun onError(error: String) {
                    Log.e(TAG, "小助手 AI WebSocket 错误: $error")
                    scheduleReconnect()
                }
            })
            webSocketClient?.connect()
        } catch (e: Exception) {
            Log.e(TAG, "小助手 AI WebSocket 连接异常: ${e.message}")
        }
    }

    private fun scheduleReconnect() {
        reconnectHandler.removeCallbacksAndMessages(null)
        reconnectHandler.postDelayed({
            Log.d(TAG, "尝试自动重连小助手 AI...")
            connectWebSocket()
        }, 3000)
    }

    private fun disconnectWebSocket() {
        try {
            webSocketClient?.disconnect()
            webSocketClient = null
        } catch (e: Exception) {
            Log.w(TAG, "断开小助手 AI 异常: ${e.message}")
        }
    }

    private fun sendCommandToBackend(text: String) {
        if (webSocketClient == null || !webSocketClient!!.isOpen() || getWebSocketUrl().isEmpty()) {
            Log.d(TAG, "小助手 AI 未连接，跳过发送指令")
            TtsManager.speak("网络未连接，无法处理您的请求") {
                restartWakeupAfterDelay()
            }
            return
        }

        try {
            val json = JSONObject()
            json.put("type", "user_input")
            json.put("text", text)

            webSocketClient?.send(json.toString())
            Log.d(TAG, "已发送指令到小助手 AI: $text")

        } catch (e: Exception) {
            Log.e(TAG, "发送指令异常: ${e.message}")
            TtsManager.speak("发送失败，请重试") {
                restartWakeupAfterDelay()
            }
        }
    }

    private fun handleBackendMessage(text: String) {
        try {
            val json = JSONObject(text)
            val type = json.optString("type", "")
            val content = json.optString("text", "")

            when (type) {
                "ai_reply" -> {
                    // 所有播报（AI 回复 / 导航指令 / 障碍物警告）统一走 ai_reply
                    val source = json.optString("source", "deepseek")
                    val priority = json.optString("priority", "LOW")
                    val expectReply = json.optBoolean("expect_reply", false)
                    Log.d(TAG, "收到播报: source=$source, priority=$priority, expectReply=$expectReply, text=$content")

                    // ★ 修复：收到 expect_reply 的消息时，立即停止唤醒监听 + 取消排队的唤醒重启
                    // 防止 TTS 播报的声音触发唤醒，导致播报内容被录入
                    if (expectReply) {
                        SpeechManager.stopWakeup()
                        SpeechTranscriber.stopTranscription()
                        // 取消之前排队的唤醒重启任务
                        restartWakeupRunnable?.let { mainHandler.removeCallbacks(it) }
                        Log.d(TAG, "expect_reply=true，已停止唤醒和录音，取消排队唤醒，等待TTS播报完成")
                    }

                    // ★ HIGH/URGENT 优先级消息（障碍物警告等）：立即打断当前播报，零延迟播报
                    // ★ LOW/MEDIUM 消息（导航指令、闲聊等）：正常排队播报
                    val onDone = {
                        // ★ 闲聊结束后恢复导航播报
                        if (isWakeupChatting) {
                            isWakeupChatting = false
                            Log.d(TAG, "唤醒闲聊结束，恢复导航/视觉消息播报")
                            // ★ 问题8修复：闲聊结束后主动获取最新导航指令
                            if (isNavigating) {
                                Log.d(TAG, "闲聊结束，主动获取最新导航指令")
                                sendCommandToBackend("GET_LATEST_INSTRUCTION")
                            }
                        }
                        if (isNavigating) {
                            // ★ 修复：导航中也需要恢复唤醒监听
                            Log.d(TAG, "导航中TTS播报完成，恢复唤醒监听...")
                            restartWakeupAfterDelay()
                        } else if (expectReply) {
                            // 需要用户回复（如 POI 选择），播报完自动进入聆听
                            // ★ 修复：先播报"我在听"让盲人用户知道系统在录音，再开始转写
                            Log.d(TAG, "expect_reply=true，TTS播报完成，播报'我在听'后进入聆听...")
                            TtsManager.speak("我在听") {
                                mainHandler.postDelayed({
                                    showVoiceFeedback("正在聆听...")
                                    SpeechTranscriber.startTranscription()
                                }, 800)
                            }
                        } else {
                            // 普通回复，重启唤醒监听
                            restartWakeupAfterDelay()
                        }
                    }

                    if (priority == "HIGH" || priority == "URGENT") {
                        Log.d(TAG, "紧急播报，打断当前TTS(保留导航): $content")
                        // ★ 问题3修复：使用 interruptAndSpeakPreserveNav 保留导航指令
                        TtsManager.interruptAndSpeakPreserveNav(content, onDone)
                    } else if (isWakeupChatting) {
                        // ★ 用户唤醒闲聊中：只播报 AI 回复和系统消息，跳过导航/视觉/找店消息
                        // 闲聊结束后（onDone）恢复导航播报
                        if (source == "deepseek" || source == "system") {
                            Log.d(TAG, "唤醒闲聊中，播报AI回复: $content")
                            listeningOrb.setState(ListeningOrbView.State.SPEAKING)  // ★ 胶囊切换到SPEAKING
                            TtsManager.speak(content, source, onDone)
                        } else {
                            Log.d(TAG, "唤醒闲聊中，跳过非AI消息(source=$source): $content")
                            // 不播报，但如果是导航消息，仍然恢复唤醒（不影响）
                        }
                    } else {
                        listeningOrb.setState(ListeningOrbView.State.SPEAKING)  // ★ 胶囊切换到SPEAKING
                        TtsManager.speak(content, source, onDone)
                    }
                    showVoiceFeedback(content)
                }

                "navigation_started" -> {
                    // 小助手 AI 通知开始导航
                    Log.d(TAG, "收到导航开始通知")
                    // ★ 导航开始时停止唤醒，防止TTS播报被唤醒打断
                    SpeechManager.stopWakeup()
                    restartWakeupRunnable?.let { mainHandler.removeCallbacks(it) }

                    // ★ 新增：解析 route steps（含 polyline），用于偏航检测
                    val stepsArray = json.optJSONArray("steps")
                    if (stepsArray != null) {
                        routePoints = mutableListOf()
                        for (i in 0 until stepsArray.length()) {
                            val step = stepsArray.getJSONObject(i)
                            val polyline = step.optString("polyline", "")
                            routePoints = routePoints + parsePolyline(polyline)
                        }
                        isOffRouteAlerted = false
                        Log.d(TAG, "已解析路线坐标点: ${routePoints.size} 个")
                    }
                    // ★ 新增：记录总距离，用于进度播报
                    val totalDistStr = json.optString("total_distance", "0")
                    totalDistance = totalDistStr.toIntOrNull() ?: 0
                    lastProgressDistance = totalDistance
                    weakGpsAlerted = false
                    // ★ 新增：保存目的地名称（用于偏航重新规划）
                    currentDestination = json.optString("destination", "")
                    Log.d(TAG, "导航目的地: $currentDestination, 总距离: ${totalDistance}米")

                    startNavigation()
                    // ★ 问题12修复：导航开始时立即检查GPS信号质量
                    if (lastGpsAccuracy > 25) {
                        mainHandler.postDelayed({
                            if (isNavigating && lastGpsAccuracy > 25) {
                                TtsManager.speak("GPS信号较弱，建议到空旷处再开始导航") {
                                    // 导航中不重启唤醒
                                }
                            }
                        }, 3000)
                    }
                    // 播报 TTS（content 可能含有目的地确认信息，如为空则播通用提示）
                    val navMsg = content.ifEmpty { "导航已开始，请跟随语音指引" }
                    TtsManager.speak(navMsg) {
                        // ★ 导航开始播报完成后，延迟恢复唤醒（给第一条导航指令留时间）
                        mainHandler.postDelayed({
                            if (isNavigating) {
                                Log.d(TAG, "导航开始播报完成，恢复唤醒监听")
                                restartWakeupAfterDelay()
                            }
                        }, 2000)
                    }
                    showVoiceFeedback(navMsg)
                }

                "navigation_data" -> {
                    // ★ 新增：收到导航数据（remaining_distance），用于偏航检测和进度播报
                    val remaining = json.optString("remaining_distance", "0")
                    val arrived = json.optBoolean("arrived", false)
                    val remainingM = remaining.toDoubleOrNull()?.toInt() ?: 0

                    if (arrived) {
                        Log.d(TAG, "导航数据：已到达目的地")
                        return
                    }

                    // ===== 偏航检测 =====
                    // ★ 修复：阈值从25米提高到50米，避免GPS漂移（30-40米）误触发偏航
                    if (routePoints.size >= 2 && !isOffRouteAlerted) {
                        val distToRoute = minDistanceToRoute(currentLatitude, currentLongitude)
                        // ★ 问题13修复：偏航阈值动态调整，考虑GPS精度
                        val offRouteThreshold = maxOf(50, (lastGpsAccuracy * 2).toInt())
                        if (distToRoute > offRouteThreshold) {
                            Log.w(TAG, "⚠️ 偏航警告：偏离路线 ${distToRoute.toInt()} 米")
                            TtsManager.speak("你好像偏了正在重新规划路线") {
                                // 不重启唤醒，导航中继续
                            }
                            isOffRouteAlerted = true
                            rerouteFromCurrentPosition()
                        }
                    }

                    // ===== GPS 信号质量检测 =====
                    // ★ 导航刚开始30秒内不播报GPS信号弱（室内精度本来就差，用户知道）
                    val navElapsed = if (navigationStartTime > 0) (System.currentTimeMillis() - navigationStartTime) / 1000 else 999
                    if (lastGpsAccuracy > 30 && !weakGpsAlerted && navElapsed > 5) {
                        Log.w(TAG, "⚠️ GPS信号较弱（精度 ${lastGpsAccuracy.toInt()} 米）")
                        TtsManager.speak("GPS信号较弱定位可能不准确建议到空旷处") {
                            // 导航中不重启唤醒
                        }
                        weakGpsAlerted = true
                    }
                    if (lastGpsAccuracy <= 15) {
                        weakGpsAlerted = false
                    }

                    // ===== 导航进度播报 =====
                    if (totalDistance > 0) {
                        val walked = totalDistance - remainingM
                        val shouldReport = (walked - (totalDistance - lastProgressDistance)) >= 200
                        val nearDestination = remainingM <= 500 && lastProgressDistance > 500

                        if (shouldReport || nearDestination) {
                            val walkedText = if (walked >= 1000) {
                                "%.1f公里".format(walked / 1000.0)
                            } else {
                                "${walked}米"
                            }
                            val remainText = if (remainingM >= 1000) {
                                "%.1f公里".format(remainingM / 1000.0)
                            } else {
                                "${remainingM}米"
                            }
                            Log.d(TAG, "进度：已步行 $walkedText，距目的地还有 $remainText")
                            TtsManager.speak("已步行${walkedText}距目的地还有${remainText}") {
                                // 导航中不重启唤醒
                            }
                            lastProgressDistance = remainingM
                        }
                    }

                    // ===== 本地到达检测（兜底）=====
                    if (remainingM <= 30 && remainingM > 0) {
                        Log.d(TAG, "即将到达目的地，剩余 ${remainingM} 米")
                    }
                    val currentStep = json.optInt("current_step", 0)
                    val totalSteps = json.optInt("total_steps", 0)

                    // ★ 修复：只有最后一步时 remainingM <= 15 才触发本地到达检测
                    // 之前在 step0 终点（剩余 2m = step1 的距离）就触发了到达
                    val isLastStep = currentStep >= totalSteps - 1
                    // ★ 问题9修复：到达检测阈值动态调整，考虑GPS精度
                    val arrivalThreshold = maxOf(15, (lastGpsAccuracy * 1.5).toInt())
                    if (remainingM <= arrivalThreshold && isLastStep && !isArrivalDetected) {
                        isArrivalDetected = true  // ★ 防止重复触发
                        Log.d(TAG, "已到达目的地附近（本地检测）")
                        // ★ 先停止导航（清空队列中的旧指令），再播报到达
                        stopNavigation()
                        TtsManager.speak("您已到达目的地附近导航结束") {
                            restartWakeupAfterDelay()
                        }
                    }
                }

                "request_gps_update" -> {
                    // ★ 服务端请求 GPS 数据
                    if (gpsAvailable && currentLongitude != 0.0 && currentLatitude != 0.0) {
                        sendGpsUpdateToAssistant()
                    } else {
                        // ★ GPS 不可用时回复状态，让服务端知道 App 还在定位中
                        try {
                            val statusJson = JSONObject()
                            statusJson.put("type", "gps_status")
                            statusJson.put("available", false)
                            webSocketClient?.send(statusJson.toString())
                        } catch (e: Exception) {
                            Log.w(TAG, "发送 GPS 状态失败: ${e.message}")
                        }
                    }
                }

                "navigation_stopped" -> {
                    // 小助手 AI 通知停止导航（用户语音取消，或到达目的地）
                    Log.d(TAG, "收到导航停止通知")
                    stopNavigation()
                    val stopMsg = content.ifEmpty { "导航已结束" }
                    TtsManager.speak(stopMsg) {
                        restartWakeupAfterDelay()
                    }
                    showVoiceFeedback(stopMsg)
                }

                "camera_control" -> {
                    // ★ 问题18修复：处理服务端的摄像头控制指令
                    val cameraMode = json.optString("camera_mode", "")
                    if (cameraMode == "off") {
                        Log.d(TAG, "收到摄像头关闭指令")
                        CameraStreamManager.stop()
                    }
                }

                else -> {
                    Log.d(TAG, "未知消息类型: $type, content: $content")
                }
            }

        } catch (e: Exception) {
            Log.e(TAG, "解析小助手 AI 消息异常: ${e.message}")
        }
    }

    // ==================== UI 相关 ====================

    private fun initViews() {
        viewFinder = findViewById(R.id.viewFinder)
        voiceFeedbackToast = findViewById(R.id.voiceFeedbackToast)
        listeningOrb = findViewById(R.id.listeningOrb)

        navHome = findViewById(R.id.navHome)
        navSOS = findViewById(R.id.navSOS)
        navSettings = findViewById(R.id.navSettings)

        ivHome = findViewById(R.id.ivHome)
        tvHome = findViewById(R.id.tvHome)
        ivSOS = findViewById(R.id.ivSOS)
        tvSOS = findViewById(R.id.tvSOS)
        ivSettings = findViewById(R.id.ivSettings)
        tvSettings = findViewById(R.id.tvSettings)
    }

    private fun setupNavigation() {
        navHome.setOnClickListener {
            setNavSelected(0)
            TtsManager.speak("首页")
            showVoiceFeedback("首页")
            KeyHandler.vibrate()
        }

        navSOS.setOnClickListener {
            setNavSelected(1)
            TtsManager.speak("紧急求助功能开发中")
            showVoiceFeedback("紧急求助功能开发中")
            KeyHandler.vibrate()
        }

        navSettings.setOnClickListener {
            setNavSelected(2)
            val intent = Intent(this, SettingsActivity::class.java)
            startActivity(intent)
            KeyHandler.vibrate()
        }
    }

    private fun setNavSelected(index: Int) {
        resetNavColors()

        when (index) {
            0 -> {
                ivHome.setColorFilter(ContextCompat.getColor(this, R.color.blue_400))
                tvHome.setTextColor(ContextCompat.getColor(this, R.color.blue_400))
            }
            1 -> {
                ivSOS.setColorFilter(ContextCompat.getColor(this, R.color.red_500))
                tvSOS.setTextColor(ContextCompat.getColor(this, R.color.red_500))
            }
            2 -> {
                ivSettings.setColorFilter(ContextCompat.getColor(this, R.color.white))
                tvSettings.setTextColor(ContextCompat.getColor(this, R.color.white))
            }
        }
    }

    private fun resetNavColors() {
        val grayColor = ContextCompat.getColor(this, R.color.gray_400)

        ivHome.setColorFilter(grayColor)
        tvHome.setTextColor(grayColor)
        ivSOS.setColorFilter(grayColor)
        tvSOS.setTextColor(grayColor)
        ivSettings.setColorFilter(grayColor)
        tvSettings.setTextColor(grayColor)
    }

    private fun showVoiceFeedback(text: String) {
        runOnUiThread {
            // ★ 去除首尾空格 + 合并中间连续空格为一个，避免App上方提示出现多余空格
            val cleaned = text.trim().replace(Regex("\\s+"), " ")
            voiceFeedbackToast.text = cleaned

            // ★ 动态设置渐变背景（紫蓝粉色调，跟说话胶囊一致）
            if (voiceFeedbackToast.background == null || voiceFeedbackToast.tag != "gradient_bg") {
                val gd = android.graphics.drawable.GradientDrawable()
                gd.cornerRadius = 60f  // 胶囊圆角
                gd.setColor(android.graphics.Color.parseColor("#1C1C1E"))  // ★ 苹果同款黑色
                voiceFeedbackToast.background = gd
                voiceFeedbackToast.tag = "gradient_bg"
            }

            voiceFeedbackToast.visibility = View.VISIBLE
            voiceFeedbackToast.alpha = 1f

            voiceFeedbackToast.animate()
                .alpha(0f)
                .setStartDelay(1000)
                .setDuration(300)
                .withEndAction {
                    voiceFeedbackToast.visibility = View.GONE
                }
                .start()
        }
    }

    // ==================== 摄像头相关 ====================

    private fun startCamera() {
        cameraProviderFuture = ProcessCameraProvider.getInstance(this)

        cameraProviderFuture.addListener({
            val cameraProvider = cameraProviderFuture.get()

            val preview = Preview.Builder()
                .build()
                .also {
                    it.setSurfaceProvider(viewFinder.surfaceProvider)
                }
            cameraPreview = preview  // ★ 保存引用

            val cameraSelector = CameraSelector.DEFAULT_BACK_CAMERA

            try {
                cameraProvider.unbindAll()

                CameraStreamManager.init(this as LifecycleOwner, cameraProvider)
                isCameraProviderReady = true  // ★ 标记摄像头已就绪
                CameraStreamManager.start(this)  // ★ 创建 ImageAnalysis（不绑定）

                // ★ 将 preview 和 imageAnalysis 一起绑定到 Lifecycle
                // 之前分开绑定会导致后绑定的解绑先绑定的（preview 没了，预览黑屏）
                val imageAnalysis = CameraStreamManager.getImageAnalysis()
                if (imageAnalysis != null) {
                    cameraProvider.bindToLifecycle(
                        this as LifecycleOwner, cameraSelector, preview, imageAnalysis
                    )
                } else {
                    cameraProvider.bindToLifecycle(
                        this as LifecycleOwner, cameraSelector, preview
                    )
                }
                CameraStreamManager.setCallback(object : CameraStreamManager.StreamCallback {
                    override fun onStreamConnected() {
                        Log.d(TAG, "视频流已连接到服务器")
                    }
                    override fun onStreamDisconnected() {
                        Log.w(TAG, "视频流已断开")
                    }
                    override fun onStreamError(msg: String) {
                        Log.e(TAG, "视频流错误: $msg")
                    }
                    override fun onFpsUpdate(fps: Int) {
                        Log.d(TAG, "视频流帧率: ${fps} FPS")
                    }
                    override fun onDetectionResult(detections: String) {
                        Log.d(TAG, "检测结果: $detections")
                    }
                })
                Log.d(TAG, "摄像头启动成功")

            } catch (e: Exception) {
                Log.e(TAG, "Use case binding failed", e)
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun allPermissionsGranted() = REQUIRED_PERMISSIONS.all {
        ContextCompat.checkSelfPermission(baseContext, it) == PackageManager.PERMISSION_GRANTED
    }

    /**
     * ★★★ 权限请求结果回调（优化版）★★★
     *
     * 旧逻辑问题：
     * - 权限被拒绝时直接 finish() 退到桌面
     * - 没有处理"不再询问"的情况
     * - 没有引导用户去系统设置中手动开启权限
     *
     * 新逻辑：
     * - 权限被拒绝时弹出 AlertDialog 解释原因
     * - 提供"去设置"按钮引导用户到系统设置页面
     * - 提供"退出"按钮（可选，但不强制退出）
     * - 权限全部授予后才初始化功能
     */
    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)

        when (requestCode) {
            REQUEST_CODE_PERMISSIONS -> {
                // 检查哪些权限被授予了
                val deniedPermissions = mutableListOf<String>()
                val permanentlyDeniedPermissions = mutableListOf<String>()

                for (i in permissions.indices) {
                    if (grantResults[i] != PackageManager.PERMISSION_GRANTED) {
                        deniedPermissions.add(permissions[i])
                        if (!ActivityCompat.shouldShowRequestPermissionRationale(this, permissions[i])) {
                            permanentlyDeniedPermissions.add(permissions[i])
                        }
                    }
                }

                if (deniedPermissions.isEmpty()) {
                    // 所有权限都授予了
                    Log.d(TAG, "所有权限已授予")
                    cameraPermissionGranted = true
                    audioPermissionGranted = true
                    locationPermissionGranted = hasLocationPermissions()
                    onAllPermissionsReady()
                } else {
                    // ★ 判断是否只有精确位置被拒绝（Android 12+ 第二个弹窗选了"稍后"）
                    val onlyFineLocationDenied = deniedPermissions.size == 1 &&
                            deniedPermissions[0] == Manifest.permission.ACCESS_FINE_LOCATION &&
                            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

                    if (onlyFineLocationDenied) {
                        // ★ 精确位置可能还没弹第二个弹窗（Android 12+ 分两次弹）
                        // 延迟 800ms 再检查，如果已授予就不弹提示
                        Log.w(TAG, "精确位置暂未授予，延迟检查（可能是 Android 12+ 第二个弹窗还没出来）")
                        cameraPermissionGranted = true
                        audioPermissionGranted = true
                        locationPermissionGranted = false
                        // 先初始化其他功能（摄像头、语音等）
                        onAllPermissionsReady()
                        // 延迟检查精确位置
                        mainHandler.postDelayed({
                            if (!hasLocationPermissions()) {
                                // 800ms 后精确位置仍然没有，说明用户确实拒绝了
                                Log.w(TAG, "精确位置权限确认被拒绝，弹提示引导去设置")
                                showFineLocationTipDialog()
                            } else {
                                // 用户在第二个弹窗中点了允许
                                Log.d(TAG, "精确位置权限已授予（第二个弹窗用户点了允许）")
                                locationPermissionGranted = true
                                requestLocationUpdates()
                            }
                        }, 800)
                    } else if (permanentlyDeniedPermissions.isNotEmpty()) {
                        Log.w(TAG, "以下权限被永久拒绝: $permanentlyDeniedPermissions")
                        showPermissionSettingsDialog(
                            permanentlyDeniedPermissions,
                            deniedPermissions
                        )
                    } else {
                        Log.w(TAG, "以下权限被拒绝: $deniedPermissions")
                        showPermissionRationaleDialog(deniedPermissions)
                    }
                }
            }
            REQUEST_CODE_LOCATION -> {
                if (hasLocationPermissions()) {
                    locationPermissionGranted = true
                    requestLocationUpdates()
                } else {
                    Log.w(TAG, "GPS 权限被拒绝")
                    showFineLocationTipDialog()
                }
            }
        }
    }

    /**
     * ★ 精确位置权限友好提示对话框
     * Android 12+ 会弹两个位置权限弹窗，第二个"精确位置"被拒绝后
     * 不弹"权限被拒绝"的吓人对话框，而是友好提示引导去设置
     */
    private fun showFineLocationTipDialog() {
        AlertDialog.Builder(this)
            .setTitle("需要精确位置")
            .setMessage("导航需要精确位置才能正常工作。\n\n" +
                    "请在弹出的设置页面中，将位置权限改为「精确」。")
            .setCancelable(false)
            .setPositiveButton("去设置") { _, _ ->
                val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                    data = Uri.fromParts("package", packageName, null)
                }
                startActivity(intent)
            }
            .setNegativeButton("稍后再说") { _, _ ->
                Toast.makeText(this, "导航功能需要精确位置权限，稍后可在设置中开启", Toast.LENGTH_LONG).show()
            }
            .show()
    }

    /**
     * 显示权限说明对话框（用户拒绝了但未勾选"不再询问"）
     * 提供"重新请求"和"退出"两个选项
     */
    private fun showPermissionRationaleDialog(deniedPermissions: List<String>) {
        val permissionNames = deniedPermissions.map { perm ->
            when (perm) {
                Manifest.permission.CAMERA -> "摄像头"
                Manifest.permission.RECORD_AUDIO -> "麦克风"
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION -> "位置信息"
                else -> perm
            }
        }.joinToString("、")

        AlertDialog.Builder(this)
            .setTitle("需要权限")
            .setMessage("小途需要$permissionNames 权限才能正常工作。\n\n" +
                    "- 摄像头：用于拍摄前方路况\n" +
                    "- 麦克风：用于语音唤醒和语音识别\n" +
                    "- 位置信息：用于导航定位")
            .setCancelable(false)
            .setPositiveButton("授予权限") { _: android.content.DialogInterface, _: Int ->
                // 重新请求被拒绝的权限
                ActivityCompat.requestPermissions(
                    this,
                    deniedPermissions.toTypedArray(),
                    REQUEST_CODE_PERMISSIONS
                )
            }
            .setNegativeButton("退出") { _: android.content.DialogInterface, _: Int ->
                // 用户选择退出，但不是 finish()，而是只显示提示
                // 让 app 继续运行，只是部分功能不可用
                Toast.makeText(this, "部分功能因缺少权限而不可用", Toast.LENGTH_LONG).show()
                // 如果摄像头权限被拒绝，至少不崩溃
                // 如果麦克风权限被拒绝，语音功能不可用
                // 如果位置权限被拒绝，导航功能不可用
            }
            .show()
    }

    /**
     * 显示"去设置"对话框（用户勾选了"不再询问"）
     * 只能引导用户去系统设置中手动开启权限
     */
    private fun showPermissionSettingsDialog(
        permanentlyDenied: List<String>,
        allDenied: List<String>
    ) {
        val permissionNames = allDenied.map { perm ->
            when (perm) {
                Manifest.permission.CAMERA -> "摄像头"
                Manifest.permission.RECORD_AUDIO -> "麦克风"
                Manifest.permission.ACCESS_FINE_LOCATION,
                Manifest.permission.ACCESS_COARSE_LOCATION -> "位置信息"
                else -> perm
            }
        }.joinToString("、")

        AlertDialog.Builder(this)
            .setTitle("需要开启权限")
            .setMessage("小途需要$permissionNames 权限才能正常工作。\n\n" +
                    "请在弹出的设置页面中开启对应权限。")
            .setCancelable(false)
            .setPositiveButton("去设置") { _: android.content.DialogInterface, _: Int ->
                // 打开应用设置页面
                val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                    data = Uri.fromParts("package", packageName, null)
                }
                startActivity(intent)
            }
            .setNegativeButton("稍后再说") { _: android.content.DialogInterface, _: Int ->
                // 用户选择稍后，不退出 app
                Toast.makeText(this, "部分功能因缺少权限而不可用", Toast.LENGTH_LONG).show()
            }
            .show()
    }

    // ==================== 偏航检测工具方法 ====================

    /**
     * 解析高德 polyline 字符串为坐标列表
     * 格式: "lon1,lat1;lon2,lat2;lon3,lat3"
     */
    private fun parsePolyline(polylineStr: String): List<Pair<Double, Double>> {
        val points = mutableListOf<Pair<Double, Double>>()
        if (polylineStr.isEmpty()) return points
        try {
            val segments = polylineStr.split(";")
            for (seg in segments) {
                val coords = seg.split(",")
                if (coords.size == 2) {
                    points.add(Pair(coords[0].toDouble(), coords[1].toDouble()))
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "解析polyline失败: ${e.message}")
        }
        return points
    }

    /**
     * 计算点到线段的最短距离（米）
     */
    private fun distanceToSegment(
        pLat: Double, pLon: Double,
        segStartLon: Double, segStartLat: Double,
        segEndLon: Double, segEndLat: Double
    ): Double {
        val latToMeter = 111320.0
        val lonToMeter = 111320.0 * Math.cos(Math.toRadians(pLat))

        val px = pLon * lonToMeter
        val py = pLat * latToMeter
        val x1 = segStartLon * lonToMeter
        val y1 = segStartLat * latToMeter
        val x2 = segEndLon * lonToMeter
        val y2 = segEndLat * latToMeter

        val dx = x2 - x1
        val dy = y2 - y1
        val lenSq = dx * dx + dy * dy

        if (lenSq == 0.0) {
            val ddx = px - x1
            val ddy = py - y1
            return Math.sqrt(ddx * ddx + ddy * ddy)
        }

        var t = ((px - x1) * dx + (py - y1) * dy) / lenSq
        t = t.coerceIn(0.0, 1.0)

        val projX = x1 + t * dx
        val projY = y1 + t * dy

        val distX = px - projX
        val distY = py - projY
        return Math.sqrt(distX * distX + distY * distY)
    }

    /**
     * 计算当前位置到整条路线的最短距离（米）
     */
    private fun minDistanceToRoute(lat: Double, lon: Double): Double {
        if (routePoints.size < 2) return Double.MAX_VALUE

        var minDist = Double.MAX_VALUE
        for (i in 0 until routePoints.size - 1) {
            val (lon1, lat1) = routePoints[i]
            val (lon2, lat2) = routePoints[i + 1]
            val dist = distanceToSegment(lat, lon, lon1, lat1, lon2, lat2)
            if (dist < minDist) minDist = dist
        }
        return minDist
    }

    /**
     * 偏航后重新规划路线
     * 通过小助手 AI 通知 GPS AI 重新规划
     */
    private fun rerouteFromCurrentPosition() {
        if (currentDestination.isEmpty()) return

        Log.d(TAG, "偏航检测：正在从当前位置重新规划路线...")

        thread {
            try {
                val requestBody = JSONObject().apply {
                    put("origin", JSONObject().apply {
                        put("longitude", currentLongitude)
                        put("latitude", currentLatitude)
                    })
                    put("destination", currentDestination)
                }

                val responseCode = postJson("${getGpsAiUrl()}/api/navigation/start", requestBody.toString())
                if (responseCode != null && responseCode in 200..299) {
                    Log.d(TAG, "偏航重新规划请求已发送")
                    // 注意：重新规划后 GPS AI 会通过 WebSocket 推送新指令
                    // 小助手 AI 不会再次发送 navigation_started，所以需要重置偏航标记
                    // ★ 修复：偏航重置时间从5秒改为60秒，避免短时间内反复重新规划
                    mainHandler.postDelayed({
                        isOffRouteAlerted = false
                    }, 60000)
                } else {
                    Log.e(TAG, "偏航重新规划失败: HTTP $responseCode")
                    isOffRouteAlerted = false
                }
            } catch (e: Exception) {
                Log.e(TAG, "偏航重新规划异常: ${e.message}")
                isOffRouteAlerted = false
            }
        }
    }

    // ==================== WebSocket 客户端（通用）====================

    class OkHttpClientWebSocketClient(
        private val url: String,
        private val listener: WebSocketListener
    ) {
        interface WebSocketListener {
            fun onMessage(text: String)
            fun onConnected()
            fun onDisconnected(code: Int, reason: String)
            fun onError(error: String)
        }

        @Volatile
        private var isConnected = false
        private var webSocket: okhttp3.WebSocket? = null
        private val client = okhttp3.OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.MINUTES)
            .writeTimeout(10, TimeUnit.SECONDS)
            .build()

        fun isOpen(): Boolean = isConnected

        fun connect() {
            try {
                val request = okhttp3.Request.Builder()
                    .url(url)
                    .build()

                webSocket = client.newWebSocket(request, object : okhttp3.WebSocketListener() {
                    override fun onOpen(webSocket: okhttp3.WebSocket, response: okhttp3.Response) {
                        isConnected = true
                        listener.onConnected()
                    }

                    override fun onMessage(webSocket: okhttp3.WebSocket, text: String) {
                        listener.onMessage(text)
                    }

                    override fun onClosing(webSocket: okhttp3.WebSocket, code: Int, reason: String) {
                        webSocket.close(1000, null)
                        isConnected = false
                        listener.onDisconnected(code, reason)
                    }

                    override fun onClosed(webSocket: okhttp3.WebSocket, code: Int, reason: String) {
                        isConnected = false
                        listener.onDisconnected(code, reason)
                    }

                    override fun onFailure(webSocket: okhttp3.WebSocket, t: Throwable, response: okhttp3.Response?) {
                        isConnected = false
                        listener.onError(t.message ?: "连接失败")
                    }
                })
            } catch (e: Exception) {
                isConnected = false
                listener.onError(e.message ?: "连接异常")
            }
        }

        fun send(text: String) {
            webSocket?.send(text)
        }

        fun disconnect() {
            isConnected = false
            webSocket?.close(1000, "App关闭")
            webSocket = null
        }
    }
}
