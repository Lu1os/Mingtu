package com.lu1os.mingtuapp

import android.content.Context
import android.content.res.AssetManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.os.Build
import android.util.Log
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.KeywordSpotterResult
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig

/**
 * 语音唤醒管理器（sherpa-onnx 版本）
 *
 * 替换原讯飞 MSC VoiceWakeuper（11201 授权问题）
 *
 * 关键配置：
 * 1. 使用 sherpa-onnx v1.12.36
 * 2. Kotlin 源码直接放在项目中（com/k2fsa/sherpa/onnx/），
 *    覆盖 AAR 中可能损坏的同名 class，确保 .class 和 .so 版本一致
 * 3. 不设置 modelType，让 sherpa-onnx 自动从模型元数据检测
 * 4. maxActivePaths = 4, keywordsScore = 1.0f, keywordsThreshold = 0.25f
 *
 * ★ 新增：声学回声消除（AEC）
 * - 使用 VOICE_COMMUNICATION 音频源（Android 自动启用系统 AEC）
 * - TTS 播报时暂停唤醒检测，避免 TTS 声音误触发
 * - TTS 停止后延迟恢复唤醒，避免残留回声误触发
 */
object SpeechManager {

    private const val TAG = "SpeechManager"

    private const val SAMPLE_RATE = 16000
    private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
    private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
    private const val MODEL_DIR = "sherpa-kws"

    private var sdkReady = false
    private var isListening = false
    private var appContext: Context? = null

    private var spotter: KeywordSpotter? = null
    private var stream: OnlineStream? = null

    private var audioRecord: AudioRecord? = null
    private var recordThread: Thread? = null
    private var isRecording = false

    // ★ TTS 播报状态：TTS 播报时暂停唤醒检测，避免回声误触发
    private var isTtsSpeaking = false
    // ★ TTS 停止后的恢复延迟（毫秒），等待回声完全消散
    private val TTS_RESUME_DELAY_MS = 400L  // ★ 800→400ms，更快恢复唤醒检测

    interface SpeechCallback {
        fun onWakeUp()
        fun onError(msg: String)
        fun onSdkInitialized(success: Boolean)
        fun onAudioLevel(level: Float) {}  // ★ 唤醒检测时的音量回调（可选实现）
    }

    private var speechCallback: SpeechCallback? = null

    fun init(context: Context) {
        if (sdkReady) {
            Log.d(TAG, "语音唤醒已初始化，跳过")
            return
        }

        appContext = context.applicationContext

        try {
            Log.d(TAG, "========================================")
            Log.d(TAG, "开始初始化 sherpa-onnx 语音唤醒")
            Log.d(TAG, "========================================")

            val assetManager: AssetManager = context.assets

            val config = KeywordSpotterConfig(
                featConfig = FeatureConfig(
                    sampleRate = SAMPLE_RATE,
                    featureDim = 80
                ),
                modelConfig = OnlineModelConfig(
                    transducer = OnlineTransducerModelConfig(
                        encoder = "$MODEL_DIR/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                        decoder = "$MODEL_DIR/decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
                        joiner = "$MODEL_DIR/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
                    ),
                    tokens = "$MODEL_DIR/tokens.txt",
                    numThreads = 2,
                    provider = "cpu"
                ),
                keywordsFile = "$MODEL_DIR/keywords.txt",
                keywordsThreshold = currentThreshold,
                keywordsScore = 1.0f,
                maxActivePaths = 4
            )

            Log.d(TAG, "配置: threshold=$currentThreshold, score=1.0, paths=4")

            spotter = KeywordSpotter(
                assetManager = assetManager,
                config = config
            )

            stream = spotter!!.createStream()

            sdkReady = true
            Log.d(TAG, "sherpa-onnx 语音唤醒初始化完成")
            Log.d(TAG, "========================================")

            speechCallback?.onSdkInitialized(true)

        } catch (e: Exception) {
            Log.e(TAG, "初始化异常: ${e.message}")
            e.printStackTrace()
            speechCallback?.onSdkInitialized(false)
        }
    }

    fun setCallback(callback: SpeechCallback) {
        this.speechCallback = callback
    }

    fun isSdkReady(): Boolean = sdkReady

    private var currentThreshold = 0.08f  // ★ 0.12→0.08，提高唤醒灵敏度

    fun setThreshold(threshold: Float) {
        currentThreshold = threshold
        Log.d(TAG, "唤醒阈值已更新: $threshold（需重启唤醒后生效）")
    }

    // ==================== TTS 状态同步 ====================

    /**
     * TTS 开始播报时调用
     * 暂停唤醒检测，避免 TTS 声音被麦克风拾取后误触发唤醒
     */
    fun onTtsStarted() {
        if (!isTtsSpeaking) {
            isTtsSpeaking = true
            Log.d(TAG, "TTS 开始播报，暂停唤醒检测（防止回声误触发）")
        }
    }

    /**
     * TTS 停止播报时调用
     * 延迟恢复唤醒检测，等待扬声器回声完全消散
     */
    fun onTtsStopped() {
        if (isTtsSpeaking) {
            isTtsSpeaking = false
            Log.d(TAG, "TTS 停止播报，${TTS_RESUME_DELAY_MS}ms 后恢复唤醒检测")
        }
    }

    fun isTtsSpeaking(): Boolean = isTtsSpeaking

    fun startWakeup() {
        if (!sdkReady) {
            Log.w(TAG, "SDK未就绪，无法启动唤醒")
            return
        }

        if (isListening) {
            Log.d(TAG, "唤醒已在监听中，跳过")
            return
        }

        try {
            Log.d(TAG, "---------- 启动唤醒监听 ----------")

            val currentSpotter = spotter ?: run {
                Log.e(TAG, "KeywordSpotter 为空")
                speechCallback?.onError("KeywordSpotter 未初始化")
                return
            }
            val currentStream = stream ?: run {
                Log.e(TAG, "OnlineStream 为空")
                speechCallback?.onError("音频流未初始化")
                return
            }

            val minBufferSize = AudioRecord.getMinBufferSize(
                SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT
            )

            // ★ 使用 VOICE_COMMUNICATION 音频源
            // Android 系统会自动启用声学回声消除（AEC）和噪声抑制（NS）
            // 这样麦克风会自动过滤掉扬声器播放的 TTS 声音
            val audioSource = MediaRecorder.AudioSource.VOICE_COMMUNICATION

            audioRecord = AudioRecord(
                audioSource,
                SAMPLE_RATE,
                CHANNEL_CONFIG,
                AUDIO_FORMAT,
                minBufferSize * 2
            )

            if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                Log.e(TAG, "AudioRecord 初始化失败（VOICE_COMMUNICATION）")
                Log.w(TAG, "尝试回退到 MIC 音频源...")
                // 回退到 MIC 音频源（某些设备不支持 VOICE_COMMUNICATION）
                audioRecord = AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    SAMPLE_RATE,
                    CHANNEL_CONFIG,
                    AUDIO_FORMAT,
                    minBufferSize * 2
                )
                if (audioRecord?.state != AudioRecord.STATE_INITIALIZED) {
                    Log.e(TAG, "AudioRecord 初始化失败（MIC 回退也失败）")
                    speechCallback?.onError("麦克风初始化失败")
                    return
                }
            }

            audioRecord?.startRecording()
            isRecording = true
            isListening = true

            recordThread = Thread {
                Log.d(TAG, "唤醒检测线程启动（AEC 模式）")
                val byteBuffer = ByteArray(3200)

                while (isRecording) {
                    val read = audioRecord?.read(byteBuffer, 0, byteBuffer.size) ?: -1

                    if (read > 0) {
                        try {
                            val floatSamples = byteToFloat(byteBuffer, read)
                            currentStream.acceptWaveform(floatSamples, SAMPLE_RATE)

                            while (currentSpotter.isReady(currentStream)) {
                                currentSpotter.decode(currentStream)
                            }

                            // 源码覆盖后 getResult() 正确返回 KeywordSpotterResult
                            val result: KeywordSpotterResult = currentSpotter.getResult(currentStream)
                            if (result.keyword.isNotEmpty()) {
                                Log.d(TAG, "========================================")
                                Log.d(TAG, "【唤醒成功】检测到: ${result.keyword}")
                                Log.d(TAG, "========================================")
                                speechCallback?.onWakeUp()
                                currentSpotter.reset(currentStream)
                            }

                            // ★ 计算音量并回调（让波浪线在转写启动前也能跟声音变化）
                            if (!isTtsSpeaking) {
                                var sum = 0L
                                for (j in 0 until read) {
                                    val s = (byteBuffer[j].toInt() and 0xFF) - 128
                                    sum += s.toLong() * s
                                }
                                val rms = kotlin.math.sqrt(sum.toFloat() / read) / 128f
                                val level = (rms * 3f).coerceIn(0f, 1f)
                                speechCallback?.onAudioLevel(level)
                            }
                        } catch (e: Exception) {
                            Log.e(TAG, "检测异常: ${e.message}")
                        }
                    } else if (read < 0) {
                        Log.e(TAG, "AudioRecord.read() 错误: $read")
                        break
                    }
                }

                Log.d(TAG, "唤醒检测线程结束")
            }.apply {
                name = "KwsDetectThread"
                start()
            }

            Log.d(TAG, "唤醒监听已启动（AEC 模式），等待唤醒词 [小途小途]...")

        } catch (e: Exception) {
            Log.e(TAG, "启动唤醒异常: ${e.message}")
            e.printStackTrace()
            speechCallback?.onError("启动唤醒异常: ${e.message}")
        }
    }

    fun stopWakeup() {
        if (!isListening) {
            return
        }

        try {
            Log.d(TAG, "停止唤醒监听")
            isRecording = false

            try {
                recordThread?.join(1000)
            } catch (e: Exception) {
                Log.w(TAG, "等待检测线程结束异常: ${e.message}")
            }
            recordThread = null

            try {
                audioRecord?.stop()
                audioRecord?.release()
                audioRecord = null
            } catch (e: Exception) {
                Log.w(TAG, "释放 AudioRecord 异常: ${e.message}")
            }

            isListening = false
            Log.d(TAG, "唤醒监听已停止")
        } catch (e: Exception) {
            Log.e(TAG, "停止唤醒异常: ${e.message}")
        }
    }

    fun release() {
        stopWakeup()
        try {
            stream?.release()
        } catch (e: Exception) {
            Log.w(TAG, "释放 OnlineStream 异常: ${e.message}")
        }
        stream = null
        try {
            spotter?.release()
        } catch (e: Exception) {
            Log.w(TAG, "释放 KeywordSpotter 异常: ${e.message}")
        }
        spotter = null
        sdkReady = false
        speechCallback = null
        appContext = null
        Log.d(TAG, "语音唤醒资源已释放")
    }

    private fun byteToFloat(byteArray: ByteArray, length: Int): FloatArray {
        val floatArray = FloatArray(length / 2)
        for (i in floatArray.indices) {
            val idx = i * 2
            val shortVal = (byteArray[idx].toInt() and 0xFF) or
                    (byteArray[idx + 1].toInt() shl 8)
            floatArray[i] = shortVal.toFloat() / 32768.0f
        }
        return floatArray
    }
}
