package com.lu1os.mingtuapp

import android.content.Context
import android.graphics.YuvImage
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.annotation.OptIn
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.lifecycle.LifecycleOwner
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import java.io.ByteArrayOutputStream
import java.net.URI
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

object CameraStreamManager {

    private const val TAG = "CameraStream"
    // ★ 服务器地址改为动态获取（从 SharedPreferences 读取用户设置的 IP）
    private var _context: Context? = null

    private fun getStreamWsUrl(): String {
        return _context?.let { AppConfig.getVisionWsUrl(it) } ?: "ws://${AppConfig.DEFAULT_SERVER_IP}:${AppConfig.VISION_WS_PORT}"
    }
    private const val TARGET_FRAME_INTERVAL_MS = 55L
    private const val JPEG_QUALITY = 40  // ★ 60→40：降低JPEG质量，减少文件大小和传输时间
    private const val TARGET_WIDTH = 640
    private const val TARGET_HEIGHT = 480

    private val isStreaming = AtomicBoolean(false)
    private var lifecycleOwner: LifecycleOwner? = null
    private var cameraProvider: ProcessCameraProvider? = null
    private var imageAnalysis: ImageAnalysis? = null
    private var wsClient: WebSocketClient? = null

    /**
     * ★ 获取 ImageAnalysis use case（供外部绑定到 Lifecycle）
     * 调用者应将此 use case 与 preview 一起传给 bindToLifecycle，
     * 避免多次调用 bindToLifecycle 导致互相解绑
     */
    fun getImageAnalysis(): ImageAnalysis? = imageAnalysis

    private var analysisExecutor = Executors.newSingleThreadExecutor()
    private var lastFrameTime = 0L
    private var frameCount = 0L
    private var fpsDisplayTime = 0L
    private val mainHandler = Handler(Looper.getMainLooper())

    // ★ 异步发送：用单独的线程发送帧，避免阻塞 analysisExecutor
    // 只保留最新帧，丢掉中间帧（解决画面延迟问题）
    private var sendExecutor = Executors.newSingleThreadExecutor()
    private val latestFrame = AtomicReference<ByteArray>(null)
    private var isSending = false

    interface StreamCallback {
        fun onStreamConnected()
        fun onStreamDisconnected()
        fun onStreamError(msg: String)
        fun onFpsUpdate(fps: Int)
        fun onDetectionResult(detections: String)
    }

    private var callback: StreamCallback? = null

    fun init(owner: LifecycleOwner, provider: ProcessCameraProvider) {
        lifecycleOwner = owner
        cameraProvider = provider
        Log.d(TAG, "CameraStreamManager 初始化完成")
    }

    fun setCallback(cb: StreamCallback) {
        this.callback = cb
    }

    @OptIn(androidx.camera.core.ExperimentalGetImage::class)
    fun start(context: Context) {
        _context = context
        if (isStreaming.get()) {
            Log.w(TAG, "已在传输中")
            return
        }

        // ★ 如果 executor 已被 shutdown（App 重启后），重新创建
        if (analysisExecutor.isShutdown) {
            analysisExecutor = Executors.newSingleThreadExecutor()
            Log.d(TAG, "analysisExecutor 已关闭，重新创建")
        }
        if (sendExecutor.isShutdown) {
            sendExecutor = Executors.newSingleThreadExecutor()
            Log.d(TAG, "sendExecutor 已关闭，重新创建")
        }

        isStreaming.set(true)
        connectWebSocket()

        val owner = lifecycleOwner
        val provider = cameraProvider
        if (owner == null || provider == null) {
            Log.w(TAG, "摄像头未就绪（owner=${owner != null}, provider=${provider != null}），WebSocket 已连接，等待摄像头初始化...")
            return
        }

        imageAnalysis = ImageAnalysis.Builder()
            .setTargetResolution(android.util.Size(TARGET_WIDTH, TARGET_HEIGHT))
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()

        imageAnalysis?.setAnalyzer(analysisExecutor) { imageProxy ->
            if (isStreaming.get() && wsClient?.isOpen == true) {
                val now = System.currentTimeMillis()
                if (now - lastFrameTime >= TARGET_FRAME_INTERVAL_MS) {
                    lastFrameTime = now
                    frameCount++

                    if (now - fpsDisplayTime >= 1000) {
                        val fps = ((frameCount * 1000) / (now - fpsDisplayTime)).toInt()
                        Log.d(TAG, "当前帧率: ${fps} FPS")
                        mainHandler.post { callback?.onFpsUpdate(fps) }
                        frameCount = 0
                        fpsDisplayTime = now
                    }

                    try {
                        val jpegBytes = imageProxyToJpeg(imageProxy)
                        if (jpegBytes != null && jpegBytes.isNotEmpty()) {
                            // ★ 只保存最新帧，不阻塞当前线程
                            // 如果上一帧还没发完，直接丢弃（保证延迟最低）
                            latestFrame.set(jpegBytes)
                            triggerSend()
                        }
                    } catch (e: Exception) {
                        Log.e(TAG, "帧处理异常: ${e.message}")
                    }
                }
            }
            imageProxy.close()
        }

        Log.d(TAG, "ImageAnalysis 已创建，等待外部绑定到 Lifecycle")
    }

    /**
     * ★ 触发异步发送：如果当前没有在发送，启动发送任务
     * 发送线程会循环取最新帧发送，直到队列为空
     */
    private fun triggerSend() {
        if (isSending) return
        isSending = true
        sendExecutor.execute {
            try {
                while (isStreaming.get()) {
                    val frame = latestFrame.getAndSet(null) ?: break
                    if (wsClient?.isOpen == true) {
                        try {
                            wsClient?.send(frame)
                        } catch (e: Exception) {
                            Log.e(TAG, "发送帧失败: ${e.message}")
                            break
                        }
                    } else {
                        break
                    }
                }
            } finally {
                isSending = false
            }
        }
    }

    fun stop() {
        isStreaming.set(false)
        imageAnalysis?.let {
            try {
                cameraProvider?.unbind(it)
            } catch (e: Exception) {
                Log.w(TAG, "解绑 ImageAnalysis 异常: ${e.message}")
            }
        }
        imageAnalysis = null
        disconnectWebSocket()
        Log.d(TAG, "画面传输已停止")
    }

    fun release() {
        stop()
        analysisExecutor.shutdown()
        sendExecutor.shutdown()
        lifecycleOwner = null
        cameraProvider = null
        callback = null
        Log.d(TAG, "CameraStreamManager 资源已释放")
    }

    fun isActive(): Boolean = isStreaming.get() && wsClient?.isOpen == true

    // ==================== WebSocket ====================

    private fun connectWebSocket() {
        if (wsClient?.isOpen == true) return

        try {
            Log.d(TAG, "正在连接视频流 WebSocket: ${getStreamWsUrl()}")

            val uri = URI(getStreamWsUrl())
            wsClient = object : WebSocketClient(uri) {
                override fun onOpen(handshakedata: ServerHandshake?) {
                    Log.d(TAG, "✅ 视频流 WebSocket 已连接！")
                    mainHandler.post { callback?.onStreamConnected() }
                }

                override fun onMessage(message: String) {
                    Log.d(TAG, "收到检测结果: $message")
                    mainHandler.post { callback?.onDetectionResult(message) }
                }

                override fun onClose(code: Int, reason: String?, remote: Boolean) {
                    Log.d(TAG, "视频流 WebSocket 关闭: code=$code, reason=$reason, remote=$remote")
                    mainHandler.post { callback?.onStreamDisconnected() }
                    if (isStreaming.get()) {
                        Log.d(TAG, "3秒后尝试重连...")
                        mainHandler.postDelayed({ connectWebSocket() }, 3000)
                    }
                }

                override fun onError(ex: Exception) {
                    Log.e(TAG, "视频流 WebSocket 错误: ${ex.message}")
                    mainHandler.post { callback?.onStreamError("连接错误: ${ex.message}") }
                }
            }

            wsClient?.connect()
        } catch (e: Exception) {
            Log.e(TAG, "创建 WebSocket 连接失败: ${e.message}")
            mainHandler.post {
                callback?.onStreamError("创建连接失败: ${e.message}")
                if (isStreaming.get()) {
                    mainHandler.postDelayed({ connectWebSocket() }, 3000)
                }
            }
        }
    }

    private fun disconnectWebSocket() {
        try {
            wsClient?.close()
        } catch (e: Exception) {
            Log.w(TAG, "关闭 WebSocket 异常: ${e.message}")
        }
        wsClient = null
    }

    // ==================== 图像处理（修复花屏：正确处理 rowStride）====================

    @OptIn(androidx.camera.core.ExperimentalGetImage::class)
    private fun imageProxyToJpeg(imageProxy: ImageProxy): ByteArray? {
        val image = imageProxy.image ?: return null

        try {
            val width = image.width
            val height = image.height

            val yPlane = image.planes[0]
            val uPlane = image.planes[1]
            val vPlane = image.planes[2]

            val yRowStride = yPlane.rowStride
            val uvRowStride = uPlane.rowStride
            val uvPixelStride = uPlane.pixelStride

            // NV21 数组：Y(width*height) + VU(width*height/2)
            val nv21 = ByteArray(width * height * 3 / 2)

            // 逐行复制 Y 数据（正确处理 rowStride 填充字节）
            val yBuffer = yPlane.buffer
            for (row in 0 until height) {
                val srcOffset = row * yRowStride
                val dstOffset = row * width
                if (srcOffset + width <= yBuffer.capacity()) {
                    yBuffer.position(srcOffset)
                    yBuffer.get(nv21, dstOffset, width)
                }
            }

            // 逐行复制 UV 数据，交错排列 V/U（NV21 格式）
            val vBuffer = vPlane.buffer
            val uBuffer = uPlane.buffer
            var uvIndex = width * height

            for (row in 0 until height / 2) {
                for (col in 0 until width / 2) {
                    val srcOffset = row * uvRowStride + col * uvPixelStride
                    if (srcOffset + 1 <= vBuffer.capacity() && srcOffset + 1 <= uBuffer.capacity()) {
                        nv21[uvIndex++] = vBuffer.get(srcOffset)
                        nv21[uvIndex++] = uBuffer.get(srcOffset)
                    }
                }
            }

            // ★ 优化：先处理旋转，再统一压缩一次JPEG
            // 原逻辑：NV21→JPEG→解码Bitmap→旋转→再JPEG（两次编解码）
            // 新逻辑：NV21→旋转NV21→JPEG（只编解码一次）
            val rotation = imageProxy.imageInfo.rotationDegrees
            val finalNv21: ByteArray
            val finalWidth: Int
            val finalHeight: Int

            if (rotation != 0 && (rotation == 90 || rotation == 270)) {
                // 90°/270°旋转：宽高互换，旋转NV21像素数据
                finalWidth = height
                finalHeight = width
                finalNv21 = ByteArray(width * height * 3 / 2)
                // 旋转Y平面
                for (y in 0 until height) {
                    for (x in 0 until width) {
                        val srcIdx = y * width + x
                        val dstIdx: Int
                        if (rotation == 90) {
                            dstIdx = x * height + (height - 1 - y)
                        } else { // 270
                            dstIdx = (width - 1 - x) * height + y
                        }
                        finalNv21[dstIdx] = nv21[srcIdx]
                    }
                }
                // 旋转UV平面（NV21: VU交错，2x2块为单位）
                // ★ 修复：UV平面宽度是 width/2，不是 width
                val uvSrcBase = width * height
                val uvDstBase = finalWidth * finalHeight
                val uvW = width / 2
                val uvH = height / 2
                for (y in 0 until uvH) {
                    for (x in 0 until uvW) {
                        val srcBase = uvSrcBase + (y * uvW + x) * 2
                        val v = nv21[srcBase]
                        val u = nv21[srcBase + 1]
                        val dstBase: Int
                        if (rotation == 90) {
                            // 90°旋转：UV宽高互换 → 新宽=uvH, 新高=uvW
                            dstBase = uvDstBase + (x * uvH + (uvH - 1 - y)) * 2
                        } else { // 270
                            dstBase = uvDstBase + ((uvW - 1 - x) * uvH + y) * 2
                        }
                        finalNv21[dstBase] = v
                        finalNv21[dstBase + 1] = u
                    }
                }
            } else if (rotation == 180) {
                // 180°旋转：宽高不变，像素倒序
                finalWidth = width
                finalHeight = height
                finalNv21 = ByteArray(width * height * 3 / 2)
                val ySize = width * height
                // 旋转Y（整个Y平面倒序）
                for (i in 0 until ySize) {
                    finalNv21[ySize - 1 - i] = nv21[i]
                }
                // 旋转UV（★ 修复：2x2块为单位倒序，每块2字节VU一起移动）
                val uvSize = ySize / 2  // UV数据总字节数
                for (i in 0 until uvSize step 2) {
                    finalNv21[ySize + uvSize - 2 - i] = nv21[ySize + i]
                    finalNv21[ySize + uvSize - 1 - i] = nv21[ySize + i + 1]
                }
            } else {
                finalWidth = width
                finalHeight = height
                finalNv21 = nv21
            }

            val yuvImage = YuvImage(finalNv21, android.graphics.ImageFormat.NV21, finalWidth, finalHeight, null)
            val out = ByteArrayOutputStream()
            yuvImage.compressToJpeg(android.graphics.Rect(0, 0, finalWidth, finalHeight), JPEG_QUALITY, out)
            return out.toByteArray()

        } catch (e: Exception) {
            Log.e(TAG, "图像转换异常: ${e.message}")
            return null
        }
    }
}
