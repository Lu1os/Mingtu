package com.lu1os.mingtuapp

import android.content.Context
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import com.iflytek.sparkchain.core.SparkChain
import com.iflytek.sparkchain.core.SparkChainConfig
import com.iflytek.sparkchain.core.asr.ASR
import com.iflytek.sparkchain.core.asr.AsrCallbacks

/**
 * 实时语音转写管理器
 * 使用 SparkChain SDK 的 ASR 类实现在线语音听写
 *
 * !!! 前置条件 !!!
 * 1. build.gradle.kts 中添加: implementation files('libs/SparkChain.aar')
 * 2. 设备需要联网（在线语音识别）
 * 3. 需要 RECORD_AUDIO 权限
 *
 * 工作流程：
 * 1. init() 初始化 SparkChain SDK 和 ASR 实例
 * 2. startTranscription() 开始录音并发送音频到云端
 * 3. 通过 onResult 回调实时返回识别结果
 * 4. stopTranscription() 停止录音并获取最终结果
 */
object SpeechTranscriber {

    private const val TAG = "SpeechTranscriber"

    // 认证信息
    private const val APP_ID = ""
    private const val API_KEY = ""
    private const val API_SECRET = ""

    // 音频录制参数
    private const val SAMPLE_RATE = 16000
    private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
    private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
    private const val BUFFER_SIZE_FACTOR = 2

    // 状态
    private var isInitialized = false
    private var isTranscribing = false
    private var appContext: Context? = null

    // ★ 最大录音时长（毫秒），超时后自动停止转写并恢复唤醒
    // 防止expect_reply场景下用户不说话，转写一直进行，唤醒永远不恢复
    private val MAX_RECORDING_DURATION_MS = 15_000L  // 15秒
    private var recordingStartTime = 0L
    private var recordingTimeoutHandler: android.os.Handler? = null
    private var recordingTimeoutRunnable: Runnable? = null

    // SparkChain ASR 实例
    private var asr: ASR? = null

    // 音频录制
    private var audioRecord: AudioRecord? = null
    private var recordThread: Thread? = null
    private var isRecording = false

    // 当前识别文本（拼接中间结果）
    private var currentText = StringBuilder()

    // 回调接口
    interface TranscriptionCallback {
        /** 识别结果回调，isFinal=true 表示本次识别结束 */
        fun onTranscriptionResult(text: String, isFinal: Boolean)
        /** 识别错误回调 */
        fun onTranscriptionError(msg: String)
        /** 识别开始 */
        fun onTranscriptionStarted()
        /** 识别停止 */
        fun onTranscriptionStopped()
        /** ★ 实时音量回调 0~1（可选实现）*/
        fun onAudioLevelChanged(level: Float) {}
    }

    private var callback: TranscriptionCallback? = null

    // ASR 结果监听
    private val asrCallbacks = object : AsrCallbacks {
        override fun onResult(result: ASR.ASRResult?, usrTag: Any?) {
            if (result == null) {
                Log.w(TAG, "ASR 结果为空")
                return
            }

            val text = result.bestMatchText ?: ""
            val status = result.status // 0:开始, 1:中间, 2:结束
            val sid = result.sid ?: ""

            Log.d(TAG, "ASR 结果: status=$status, text=\"$text\", sid=$sid")

            when (status) {
                0 -> {
                    // 开始
                    currentText.clear()
                    Log.d(TAG, "识别开始")
                    callback?.onTranscriptionStarted()
                }
                1 -> {
                    // 中间结果
                    currentText.append(text)
                    Log.d(TAG, "中间结果: \"$currentText\"")
                    callback?.onTranscriptionResult(currentText.toString(), false)
                }
                2 -> {
                    // 结束
                    val finalText = currentText.toString().ifEmpty { text }
                    Log.d(TAG, "========================================")
                    Log.d(TAG, "最终识别结果: \"$finalText\"")
                    Log.d(TAG, "========================================")
                    callback?.onTranscriptionResult(finalText, true)
                    currentText.clear()
                }
            }
        }

        override fun onError(error: ASR.ASRError?, usrTag: Any?) {
            if (error == null) {
                Log.w(TAG, "ASR 错误: null")
                return
            }

            val errorCode = error.code
            val errorMsg = error.errMsg ?: "未知错误"
            val sid = error.sid ?: ""

            Log.e(TAG, "ASR 错误: code=$errorCode, msg=$errorMsg, sid=$sid")

            when (errorCode) {
                10105 -> Log.e(TAG, "【10105】非法访问 - 请检查apiKey/apiSecret")
                10700 -> Log.e(TAG, "【10700】引擎错误")
                11200 -> Log.e(TAG, "【11200】授权失败 - 请检查APPID和网络")
                11201 -> Log.e(TAG, "【11201】引擎初始化失败")
                else -> Log.e(TAG, "【$errorCode】$errorMsg")
            }

            callback?.onTranscriptionError("$errorCode: $errorMsg")
        }
    }

    /**
     * 初始化 SparkChain SDK 和 ASR 实例
     * 必须在使用转写功能前调用
     */
    fun init(context: Context) {
        if (isInitialized) {
            Log.d(TAG, "语音转写已初始化，跳过")
            return
        }

        appContext = context.applicationContext
        recordingTimeoutHandler = android.os.Handler(android.os.Looper.getMainLooper())

        try {
            Log.d(TAG, "========================================")
            Log.d(TAG, "开始初始化 SparkChain ASR")
            Log.d(TAG, "========================================")

            // 第一步：初始化 SparkChain SDK（全局只需一次）
            val workDir = context.getExternalFilesDir(null)?.absolutePath
                ?: context.filesDir.absolutePath

            val config = SparkChainConfig.builder()
                .appID(APP_ID)
                .apiKey(API_KEY)
                .apiSecret(API_SECRET)
                .workDir(workDir)
                .logLevel(4) // WARN 级别

            val ret = SparkChain.getInst().init(context.applicationContext, config)
            Log.d(TAG, "SparkChain.init() 返回值: $ret")

            if (ret != 0) {
                Log.e(TAG, "SparkChain SDK 初始化失败: $ret")
                callback?.onTranscriptionError("SparkChain 初始化失败: $ret")
                return
            }
            Log.d(TAG, "SparkChain SDK 初始化成功")

            // 第二步：创建 ASR 实例
            asr = ASR()
            asr?.language("zh_cn")    // 中文
            asr?.domain("iat")         // 日常用语
            asr?.accent("mandarin")    // 普通话
            asr?.vadEos(3000)          // 静默 3 秒后结束
            asr?.ptt(true)             // 开启标点符号

            // 注册回调
            asr?.registerCallbacks(asrCallbacks)

            Log.d(TAG, "ASR 实例创建完成")
            Log.d(TAG, "  language: zh_cn")
            Log.d(TAG, "  domain: iat")
            Log.d(TAG, "  accent: mandarin")
            Log.d(TAG, "  vadEos: 3000ms")
            Log.d(TAG, "========================================")

            isInitialized = true

        } catch (e: Exception) {
            Log.e(TAG, "初始化异常: ${e.message}")
            e.printStackTrace()
            callback?.onTranscriptionError("初始化异常: ${e.message}")
        }
    }

    /**
     * 设置回调
     */
    fun setCallback(cb: TranscriptionCallback) {
        this.callback = cb
    }

    /**
     * 开始实时转写
     * 开始录音并将音频数据发送到 SparkChain ASR
     */
    fun startTranscription() {
        if (!isInitialized) {
            Log.e(TAG, "未初始化，无法开始转写")
            callback?.onTranscriptionError("语音转写未初始化")
            return
        }

        if (isTranscribing) {
            Log.w(TAG, "正在转写中，跳过")
            return
        }

        try {
            Log.d(TAG, "---------- 开始实时转写 ----------")
            currentText.clear()

            // 启动 ASR 会话
            val ret = asr?.start("mingtu_transcription")
            Log.d(TAG, "ASR.start() 返回值: $ret")

            if (ret != 0) {
                Log.e(TAG, "ASR 启动失败: $ret")
                callback?.onTranscriptionError("ASR 启动失败: $ret")
                return
            }

            // 开始录音线程
            startRecording()
            isTranscribing = true
            Log.d(TAG, "实时转写已启动")

            // ★ 启动超时计时器，防止用户不说话时转写一直进行
            recordingStartTime = System.currentTimeMillis()
            recordingTimeoutHandler?.removeCallbacksAndMessages(null)
            recordingTimeoutRunnable = Runnable {
                if (isTranscribing) {
                    Log.w(TAG, "录音超时(${MAX_RECORDING_DURATION_MS / 1000}秒)，自动停止转写")
                    stopTranscription()
                }
            }
            recordingTimeoutHandler?.postDelayed(recordingTimeoutRunnable!!, MAX_RECORDING_DURATION_MS)

        } catch (e: Exception) {
            Log.e(TAG, "启动转写异常: ${e.message}")
            e.printStackTrace()
            callback?.onTranscriptionError("启动转写异常: ${e.message}")
        }
    }

    /**
     * 停止实时转写
     */
    fun stopTranscription() {
        if (!isTranscribing) {
            return
        }

        try {
            Log.d(TAG, "---------- 停止实时转写 ----------")

            // ★ 取消超时计时器
            recordingTimeoutHandler?.removeCallbacksAndMessages(null)
            recordingTimeoutRunnable = null

            // 停止录音
            stopRecording()

            // 停止 ASR 会话（等待最终结果）
            asr?.stop(false) // false = 等待云端发送最终结果后再结束

            isTranscribing = false
            Log.d(TAG, "实时转写已停止")

            callback?.onTranscriptionStopped()

        } catch (e: Exception) {
            Log.e(TAG, "停止转写异常: ${e.message}")
        }
    }

    /**
     * 开始录音线程
     * 从麦克风采集音频数据并发送给 ASR 引擎
     */
    private fun startRecording() {
        val minBufferSize = AudioRecord.getMinBufferSize(
            SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT
        )
        val bufferSize = minBufferSize * BUFFER_SIZE_FACTOR

        try {
            audioRecord = AudioRecord(
                MediaRecorder.AudioSource.MIC,
                SAMPLE_RATE,
                CHANNEL_CONFIG,
                AUDIO_FORMAT,
                bufferSize
            )

            if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord 初始化失败")
                callback?.onTranscriptionError("麦克风初始化失败")
                return
            }

            audioRecord?.startRecording()
            isRecording = true

            // 启动录音线程，每 40ms 发送 1280 字节（16K * 16bit * 单声道 * 0.04s = 1280）
            recordThread = Thread {
                Log.d(TAG, "录音线程启动")
                val buffer = ByteArray(1280)

                while (isRecording) {
                    val read = audioRecord?.read(buffer, 0, buffer.size) ?: -1

                    if (read > 0) {
                        // 将音频数据写入 ASR 引擎
                        val writeRet = asr?.write(buffer.copyOfRange(0, read))
                        if (writeRet != 0) {
                            Log.w(TAG, "ASR.write() 返回: $writeRet")
                        }

                        // ★ 计算 RMS 音量
                        var sum = 0L
                        for (j in 0 until read) {
                            val sample = (buffer[j].toInt() and 0xFF) - 128
                            sum += sample.toLong() * sample
                        }
                        val rms = kotlin.math.sqrt(sum.toFloat() / read) / 128f
                        val level = (rms * 3f).coerceIn(0f, 1f)  // 放大3倍让小声音也能看到波动
                        callback?.onAudioLevelChanged(level)
                    } else if (read < 0) {
                        Log.e(TAG, "AudioRecord.read() 错误: $read")
                        break
                    }
                }

                Log.d(TAG, "录音线程结束")
            }.apply {
                name = "AudioRecordThread"
                start()
            }

        } catch (e: Exception) {
            Log.e(TAG, "启动录音异常: ${e.message}")
            e.printStackTrace()
        }
    }

    /**
     * 停止录音
     */
    private fun stopRecording() {
        isRecording = false

        try {
            recordThread?.join(1000) // 等待录音线程结束
        } catch (e: Exception) {
            Log.w(TAG, "等待录音线程结束异常: ${e.message}")
        }
        recordThread = null

        try {
            audioRecord?.stop()
            audioRecord?.release()
            audioRecord = null
        } catch (e: Exception) {
            Log.w(TAG, "释放 AudioRecord 异常: ${e.message}")
        }
    }

    /**
     * 检查是否正在转写
     */
    fun isTranscribing(): Boolean = isTranscribing

    /**
     * 检查是否已初始化
     */
    fun isInitialized(): Boolean = isInitialized

    /**
     * 释放资源
     */
    fun release() {
        stopTranscription()

        try {
            SparkChain.getInst().unInit()
            Log.d(TAG, "SparkChain SDK 已逆初始化")
        } catch (e: Exception) {
            Log.w(TAG, "SparkChain unInit 异常: ${e.message}")
        }

        asr = null
        isInitialized = false
        callback = null
        appContext = null
        Log.d(TAG, "语音转写资源已释放")
    }
}
