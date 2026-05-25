package com.lu1os.mingtuapp

import android.content.Context
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.iflytek.aikit.core.AeeEvent
import com.iflytek.aikit.core.AiHandle
import com.iflytek.aikit.core.AiHelper
import com.iflytek.aikit.core.AiInput
import com.iflytek.aikit.core.AiListener
import com.iflytek.aikit.core.AiRequest
import com.iflytek.aikit.core.AiResponse
import com.iflytek.aikit.core.AiText
import java.io.IOException
import java.util.LinkedList

/**
 * 离线语音合成管理器
 * 使用讯飞高品质版离线TTS引擎进行语音播报
 *
 * 能力ID: e2e44feff (AIKit XTTS)
 *
 * !!! 重要提醒 !!!
 * 首次运行必须【联网激活】，否则会报 18405/18708 错误
 * 激活成功后，后续可离线使用
 *
 * ★ 新增：播报状态与 SpeechManager 同步
 * - 播报开始时通知 SpeechManager（暂停唤醒检测）
 * - 播报结束时通知 SpeechManager（恢复唤醒检测）
 * - 防止 TTS 声音被麦克风拾取后误触发唤醒
 */
object TtsManager {

    private const val TAG = "TtsManager"
    private const val ABILITY_ID = "e2e44feff"

    private const val DEFAULT_VOICE = "xiaoyan"

    private const val XTTS_ASSETS_DIR = "iflytek/xtts"

    // 资源文件路径（.irf 和 .dat 格式）
    // 前端资源文件（必需）
    private const val FRONT_RES_PATH = "iflytek/xtts/e4caee636_1.0.2_xTTS_CnCn_front_Emb_arm_2017.irf"
    // 发音人资源文件 - xiaoyan
    private const val VOICE_IRF_PATH = "iflytek/xtts/e3fe94474_1.0.0_xTTS_CnCn_xiaoyan_2018_arm.irf"
    private const val VOICE_DAT_PATH = "iflytek/xtts/e05d571cc_1.0.0_xTTS_CnCn_xiaoyan_2018_fix_arm.dat"

    private const val SAMPLE_RATE = 16000
    private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_OUT_MONO
    private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT

    private var isInitialized = false
    private var isSpeaking = false
    // ★ Bug A修复：跟踪当前正在播报的消息source，用于同source打断替换
    private var _currentSpeakSource: String = ""
    private var _currentSpeakText: String = ""
    private var _lastSpeakFailed = false  // ★ 标记上次 doSpeak 是否因18310失败
    private var aiHandle: AiHandle? = null
    private var audioTrack: AudioTrack? = null
    private var audioPlayHandler: Handler? = null
    private var audioPlayThread: Thread? = null
    private var appContext: Context? = null
    private var resourceCheckPassed = false

    // ★ 问题4修复：TTS不可用时的通知回调（由MainActivity设置）
    var onTtsUnavailable: (() -> Unit)? = null

    private var onSpeakCompletedListener: (() -> Unit)? = null

    // ★ 播报队列：解决快速连续调用 speak() 时的竞态问题
    // 之前直接 stop() + start() 会导致讯飞 AIKit 引擎状态不一致（10110错误）
    // 现在改为队列模式：当前播报完成后自动播放下一条
    // ★ 新增 source 字段：同来源的消息在队列中只保留最新一条（抢占式替换）
    private data class SpeakRequest(val text: String, val source: String, val onCompleted: (() -> Unit)?)
    private val speakQueue = LinkedList<SpeakRequest>()
    private var isProcessingQueue = false

    private const val AUDIOPLAYER_INIT = 0x0000
    private const val AUDIOPLAYER_START = 0x0001
    private const val AUDIOPLAYER_WRITE = 0x0002
    private const val AUDIOPLAYER_END = 0x0003

    private val ttsListener = object : AiListener {
        override fun onResult(handleID: Int, outputs: MutableList<AiResponse>?, usrContext: Any?) {
            Log.d(TAG, "========== onResult 回调 ==========")
            Log.d(TAG, "handleID: $handleID")
            Log.d(TAG, "outputs 是否为空: ${outputs == null}")
            Log.d(TAG, "outputs 数量: ${outputs?.size ?: 0}")

            var totalAudioBytes = 0L

            if (outputs != null && outputs.isNotEmpty()) {
                for ((index, output) in outputs.withIndex()) {
                    val bytes = output.value
                    val key = output.key

                    Log.d(TAG, "outputs[$index]: key=$key, bytes=${bytes?.size ?: "null"}")

                    if (bytes == null) {
                        Log.w(TAG, "outputs[$index] bytes 为空，跳过")
                        continue
                    }

                    if ("audio" == key) {
                        totalAudioBytes += bytes.size
                        Log.d(TAG, "收到音频数据，大小: ${bytes.size} 字节，累计: $totalAudioBytes 字节")
                        val bundle = Bundle()
                        bundle.putByteArray("audio", bytes)
                        val msg = audioPlayHandler?.obtainMessage()
                        msg?.what = AUDIOPLAYER_WRITE
                        msg?.obj = bundle
                        audioPlayHandler?.sendMessage(msg!!)
                    }
                }
            }

            if (totalAudioBytes == 0L) {
                Log.w(TAG, "!!! 警告: 本次回调未收到任何音频数据 !!!")
            }

            Log.d(TAG, "========== onResult 结束 ==========")
        }

        override fun onEvent(handleID: Int, event: Int, eventData: MutableList<AiResponse>?, usrContext: Any?) {
            Log.d(TAG, "========== onEvent 回调 ==========")
            Log.d(TAG, "handleID: $handleID")
            Log.d(TAG, "event: $event")
            Log.d(TAG, "eventData 是否为空: ${eventData == null}")

            when (event) {
                AeeEvent.AEE_EVENT_END.value -> {
                    Log.d(TAG, "事件类型: AEE_EVENT_END (合成结束)")
                    // ★ 修复闪退：先调用 end() 释放会话，再立即置 null
                    val handleToEnd = aiHandle
                    aiHandle = null  // 先置 null，防止并发
                    if (handleToEnd != null) {
                        try {
                            AiHelper.getInst().end(handleToEnd)
                        } catch (e: Exception) {
                            Log.e(TAG, "end() 异常（可忽略）: ${e.message}")
                        }
                    }
                    audioPlayHandler?.sendEmptyMessage(AUDIOPLAYER_END)
                    // ★ 注意：不在合成结束时触发回调，改为在音频播放结束时触发
                    // 避免合成完成但音频还在播放时就开始录音，录到播报内容
                }
                AeeEvent.AEE_EVENT_PROGRESS.value -> {
                    Log.d(TAG, "事件类型: AEE_EVENT_PROGRESS (合成进度)")
                }
                else -> {
                    Log.d(TAG, "事件类型: 未知事件 (event=$event)")
                }
            }

            Log.d(TAG, "========== onEvent 结束 ==========")
        }

        override fun onError(handleID: Int, err: Int, msg: String?, usrContext: Any?) {
            Log.e(TAG, "========== onError 回调 ==========")
            Log.e(TAG, "handleID: $handleID")
            Log.e(TAG, "错误码(err): $err")
            Log.e(TAG, "错误信息(msg): $msg")
            Log.e(TAG, "usrContext: $usrContext")

            when (err) {
                10101 -> Log.e(TAG, "【错误码 10101】引擎未初始化 - 请先初始化SDK")
                10102 -> Log.e(TAG, "【错误码 10102】引擎已初始化 - 无需重复初始化")
                10103 -> Log.e(TAG, "【错误码 10103】引擎初始化失败 - 请检查SDK配置")
                10104 -> Log.e(TAG, "【错误码 10104】引擎未授权 - 请检查AppID和能力ID")
                10105 -> Log.e(TAG, "【错误码 10105】引擎授权失败 - 请确保设备已联网")
                10106 -> Log.e(TAG, "【错误码 10106】引擎参数错误 - 请检查vcn、language等参数")
                10107 -> Log.e(TAG, "【错误码 10107】引擎不支持该功能")
                10108 -> Log.e(TAG, "【错误码 10108】引擎资源加载失败 - 请检查workDir下的资源文件")
                10109 -> Log.e(TAG, "【错误码 10109】引擎内部错误")
                10110 -> Log.e(TAG, "【错误码 10110】引擎正在使用中")
                18405 -> {
                    Log.e(TAG, "【错误码 18405】SDK授权失败")
                    Log.e(TAG, "【解决方案】首次使用需联网激活，请确保网络通畅后重启App")
                }
                18708 -> {
                    Log.e(TAG, "【错误码 18708】离线能力未激活")
                    Log.e(TAG, "【解决方案】首次使用需联网激活，请确保网络通畅后重启App")
                }
                else -> Log.e(TAG, "【未知错误码】$err - 请查阅讯飞AIKit官方文档")
            }

            Log.e(TAG, "========== onError 结束 ==========")

            isSpeaking = false
            _currentSpeakSource = ""  // ★ Bug A修复
            _currentSpeakText = ""  // ★ Bug A修复
            // ★ TTS 出错时也要通知 SpeechManager 恢复唤醒
            SpeechManager.onTtsStopped()
            onSpeakCompletedListener?.invoke()
            onSpeakCompletedListener = null
        }
    }

    fun init(context: Context) {
        if (isInitialized) {
            Log.d(TAG, "离线合成已初始化，跳过")
            return
        }

        appContext = context.applicationContext

        try {
            Log.d(TAG, "========================================")
            Log.d(TAG, "开始初始化高品质离线语音合成 (XTTS)")
            Log.d(TAG, "========================================")
            Log.d(TAG, "ABILITY_ID: $ABILITY_ID")
            Log.d(TAG, "DEFAULT_VOICE: $DEFAULT_VOICE")

            Log.d(TAG, "---------- SDK 授权状态检查 ----------")

            val authStatus = MingtuApplication.isAuthSuccess
            val authErrorCode = MingtuApplication.authErrorCode

            if (authStatus) {
                Log.d(TAG, "SDK初始化成功 - 已授权")
            } else {
                Log.e(TAG, "SDK初始化失败 - 未授权")
                Log.e(TAG, "授权错误码: $authErrorCode")
                Log.e(TAG, "!!! 请确保设备已联网，首次运行需要联网激活 !!!")

                when (authErrorCode) {
                    18405 -> {
                        Log.e(TAG, "【18405】SDK授权失败 - 请检查网络连接和APPID配置")
                        Log.e(TAG, "【解决方案】卸载App重新安装，确保联网后再次运行")
                    }
                    18708 -> {
                        Log.e(TAG, "【18708】离线能力未激活 - 首次使用需联网激活")
                        Log.e(TAG, "【解决方案】确保网络通畅后重启App")
                    }
                }
            }

            Log.d(TAG, "---------- 检查资源文件 ----------")
            resourceCheckPassed = checkAssetsFiles(context)

            if (!resourceCheckPassed) {
                Log.e(TAG, "========================================")
                Log.e(TAG, "!!! 资源文件检查失败 !!!")
                Log.e(TAG, "请将以下文件放入 assets/iflytek/xtts/ 目录:")
                Log.e(TAG, "1. e4caee636_1.0.2_xTTS_CnCn_front_Emb_arm_2017.irf (前端资源)")
                Log.e(TAG, "2. e3fe94474_1.0.0_xTTS_CnCn_xiaoyan_2018_arm.irf (发音人模型)")
                Log.e(TAG, "3. e05d571cc_1.0.0_xTTS_CnCn_xiaoyan_2018_fix_arm.dat (发音人修复)")
                Log.e(TAG, "========================================")
            }

            Log.d(TAG, "注册能力监听器...")
            AiHelper.getInst().registerListener(ABILITY_ID, ttsListener)
            Log.d(TAG, "注册能力监听器完成")

            Log.d(TAG, "启动音频播放线程...")
            startAudioPlayThread()
            Log.d(TAG, "音频播放线程启动完成")

            isInitialized = true
            Log.d(TAG, "========================================")
            Log.d(TAG, "离线合成初始化完成")
            Log.d(TAG, "========================================")
        } catch (e: Exception) {
            Log.e(TAG, "初始化异常: ${e.message}")
            e.printStackTrace()
        }
    }

    private fun checkAssetFile(context: Context, path: String): Boolean {
        return try {
            val inputStream = context.assets.open(path)
            val size = inputStream.available()
            inputStream.close()
            Log.d(TAG, "资源文件存在且可读: $path (大小: $size 字节)")
            true
        } catch (e: IOException) {
            Log.e(TAG, "资源文件不存在或不可读: $path")
            false
        }
    }

    private fun checkAssetsFiles(context: Context): Boolean {
        Log.d(TAG, "---------- 检查 assets 资源文件 ----------")

        var allExists = true

        try {
            val assets = context.assets

            Log.d(TAG, "检查 assets 目录结构...")

            val rootFiles = assets.list("")
            Log.d(TAG, "assets/ 目录: ${rootFiles?.joinToString(", ") ?: "空"}")

            val iflytekFiles = assets.list("iflytek")
            Log.d(TAG, "assets/iflytek/ 目录: ${iflytekFiles?.joinToString(", ") ?: "空"}")

            val xttsFiles = assets.list(XTTS_ASSETS_DIR)
            Log.d(TAG, "assets/$XTTS_ASSETS_DIR/ 目录: ${xttsFiles?.joinToString(", ") ?: "空"}")

            Log.d(TAG, "检查前端资源: $FRONT_RES_PATH")
            if (!checkAssetFile(context, FRONT_RES_PATH)) {
                Log.e(TAG, "错误：前端资源文件不存在")
                allExists = false
            }

            Log.d(TAG, "检查发音人模型: $VOICE_IRF_PATH")
            if (!checkAssetFile(context, VOICE_IRF_PATH)) {
                Log.e(TAG, "错误：发音人模型文件不存在")
                allExists = false
            }

            Log.d(TAG, "检查发音人修复文件: $VOICE_DAT_PATH")
            if (!checkAssetFile(context, VOICE_DAT_PATH)) {
                Log.w(TAG, "警告：发音人修复文件不存在（可选，不影响基本功能）")
            }

        } catch (e: Exception) {
            Log.e(TAG, "检查 assets 资源异常: ${e.message}")
            allExists = false
        }

        Log.d(TAG, "---------- assets 资源检查完成 ----------")
        return allExists
    }

    private fun startAudioPlayThread() {
        audioPlayThread = Thread {
            Looper.prepare()
            audioPlayHandler = Handler(Looper.myLooper()!!) { msg ->
                when (msg.what) {
                    AUDIOPLAYER_INIT -> {
                        Log.d(TAG, ">>> 音频播放器初始化")
                        val minBufferSize = AudioTrack.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
                        Log.d(TAG, "AudioTrack minBufferSize: $minBufferSize")
                        // ★ 使用 4 倍 minBufferSize，避免 INIT→START 异步延迟期间
                        // TTS 引擎已生成的音频数据堆积导致 buffer 溢出（实际写入 0 字节）
                        val bufferSize = minBufferSize * 4
                        audioTrack = AudioTrack(AudioManager.STREAM_MUSIC, SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT, bufferSize, AudioTrack.MODE_STREAM)
                        Log.d(TAG, "AudioTrack 状态: ${audioTrack?.state} (1=STATE_INITIALIZED)")
                        if (audioTrack?.state != AudioTrack.STATE_INITIALIZED) {
                            Log.e(TAG, "!!! AudioTrack 初始化失败 !!!")
                        }
                        audioPlayHandler?.sendEmptyMessage(AUDIOPLAYER_START)
                    }
                    AUDIOPLAYER_START -> {
                        Log.d(TAG, ">>> 音频播放器开始播放")
                        audioTrack?.play()
                        Log.d(TAG, "AudioTrack 播放状态: ${audioTrack?.playState} (3=PLAYSTATE_PLAYING)")
                        isSpeaking = true
                        // ★ 通知 SpeechManager：TTS 开始播报，暂停唤醒检测
                        SpeechManager.onTtsStarted()
                    }
                    AUDIOPLAYER_WRITE -> {
                        // ★ AudioTrack 还没 play() 时丢弃音频数据（INIT→START 异步处理中）
                        // 避免数据堆积导致 buffer 溢出，只丢失开头几十毫秒，不影响听感
                        if (audioTrack?.playState != AudioTrack.PLAYSTATE_PLAYING) {
                            return@Handler true
                        }
                        val bundle = msg.obj as Bundle
                        val audioData = bundle.getByteArray("audio")
                        if (audioTrack != null && audioData != null && audioData.isNotEmpty()) {
                            val written = audioTrack?.write(audioData, 0, audioData.size) ?: 0
                            Log.d(TAG, ">>> 写入音频数据: ${audioData.size} 字节, 实际写入: $written 字节")
                            if (written < 0) {
                                Log.e(TAG, "!!! AudioTrack 写入错误: $written !!!")
                            }
                        }
                    }
                    AUDIOPLAYER_END -> {
                        Log.d(TAG, ">>> 音频播放结束")
                        if (audioTrack != null && isSpeaking) {
                            audioTrack?.stop()
                            isSpeaking = false
                            _currentSpeakSource = ""  // ★ Bug A修复
                            _currentSpeakText = ""  // ★ Bug A修复
                        }
                        // ★ 通知 SpeechManager：TTS 停止播报，可以恢复唤醒检测
                        SpeechManager.onTtsStopped()
                        // ★ 修复：在音频播放真正结束时触发回调
                        // 之前在 AEE_EVENT_END（合成结束）触发，但音频可能还在播放
                        // 导致 expect_reply 场景下录音启动时 TTS 还在播，录到播报内容
                        val listener = onSpeakCompletedListener
                        onSpeakCompletedListener = null
                        listener?.invoke()
                        // ★ 播报队列：当前播报完成后，自动处理队列中的下一条
                        processNextInQueue()
                    }
                }
                true
            }
            Looper.loop()
        }.apply { start() }
    }

    fun speak(text: String) {
        speak(text, "", null)
    }

    fun speak(text: String, source: String = "", onCompleted: (() -> Unit)? = null) {
        Log.d(TAG, "========================================")
        Log.d(TAG, "speak() 被调用: \"$text\" (source=$source, 队列大小: ${speakQueue.size}, isSpeaking: $isSpeaking)")
        Log.d(TAG, "========================================")

        if (!isInitialized) {
            Log.e(TAG, "错误: 未初始化，无法播报")
            onCompleted?.invoke()
            return
        }

        if (!MingtuApplication.isAuthSuccess) {
            Log.e(TAG, "错误: SDK未授权，无法播报")
            Log.e(TAG, "授权错误码: ${MingtuApplication.authErrorCode}")
            // ★ 问题4修复：通知盲人TTS不可用
            onTtsUnavailable?.invoke()
            onCompleted?.invoke()
            return
        }

        if (!resourceCheckPassed) {
            Log.w(TAG, "警告: 资源文件检查未通过，尝试再次验证...")
            appContext?.let { ctx ->
                if (!checkAssetFile(ctx, FRONT_RES_PATH) || !checkAssetFile(ctx, VOICE_IRF_PATH)) {
                    Log.e(TAG, "错误: 资源文件缺失，无法播报")
                    // ★ 问题4修复：通知盲人TTS不可用
                    onTtsUnavailable?.invoke()
                    onCompleted?.invoke()
                    return
                }
                resourceCheckPassed = true
            }
        }

        // ★ 队列模式：如果正在播报，将请求放入队列等待
        // 之前直接 stop() + start() 会导致讯飞 AIKit 引擎状态不一致
        if (isSpeaking) {
            Log.d(TAG, "正在播报中，加入队列等待: \"$text\" (source=$source)")

            // ★ Bug A修复：如果当前正在播报的消息和本次消息同source，打断当前播报
            // 防止旧GPS指令和新GPS指令都被完整播报（盲人听到两个不同距离会困惑）
            if (source.isNotEmpty() && _currentSpeakSource == source) {
                Log.d(TAG, "★★★ 同source($source)正在播报，打断并替换: \"${_currentSpeakText}\" → \"$text\"")
                // 停止当前播报
                stop()
                // 直接播报新消息（不经过队列）
                doSpeak(text, source, onCompleted)
                return
            }

            // ★ 抢占式替换：如果队列中已有同 source 的消息，替换掉旧的
            // 这样 GPS 新指令会替换旧的 GPS 指令，视觉新警告替换旧的视觉警告
            // 用户永远只听到最新的导航指令，不会播一堆过时的
            if (source.isNotEmpty()) {
                val iterator = speakQueue.iterator()
                while (iterator.hasNext()) {
                    val existing = iterator.next()
                    if (existing.source == source) {
                        Log.d(TAG, "队列中已有同source($source)的消息，替换: \"${existing.text}\" → \"$text\"")
                        existing.onCompleted?.invoke()  // 触发被替换消息的回调
                        iterator.remove()
                        break
                    }
                }
            }

            speakQueue.add(SpeakRequest(text, source, onCompleted))
            return
        }

        // 执行实际播报
        doSpeak(text, source, onCompleted)
    }

    /**
     * 处理队列中的下一条播报请求
     */
    private fun processNextInQueue() {
        val next = speakQueue.poll()
        if (next != null) {
            Log.d(TAG, "从队列取出下一条播报: \"${next.text}\" (剩余: ${speakQueue.size})")
            // 延迟100ms，给引擎一点释放时间
            audioPlayHandler?.postDelayed({
                doSpeak(next.text, next.source, next.onCompleted)
            }, 100)
        } else {
            isProcessingQueue = false
        }
    }

    /**
     * 实际执行播报（内部方法）
     */
    private fun doSpeak(text: String, source: String = "", onCompleted: (() -> Unit)?) {
        Log.d(TAG, "---------- doSpeak() 开始 ----------")
        Log.d(TAG, "文本: \"$text\"")
        _currentSpeakSource = source  // ★ Bug A修复
        _currentSpeakText = text  // ★ Bug A修复

        try {
            onSpeakCompletedListener = onCompleted

            if (audioTrack == null) {
                Log.d(TAG, "AudioTrack 未初始化，发送初始化消息")
                audioPlayHandler?.sendEmptyMessage(AUDIOPLAYER_INIT)
            } else {
                Log.d(TAG, "AudioTrack 已初始化，发送开始消息")
                audioPlayHandler?.sendEmptyMessage(AUDIOPLAYER_START)
            }

            Log.d(TAG, "---------- 构建合成参数 ----------")

            val paramBuilder = AiInput.builder()
                .param("vcn", currentVoice)        // 发音人（string）
                .param("language", 1)              // 语种（int）：1=中文
                .param("textEncoding", "UTF-8")    // 文本编码（string）
                .param("pitch", 50)                // 语调（int）：0-100
                .param("volume", currentVolume)    // 音量（int）：0-100
                .param("speed", currentSpeed)      // 语速（int）：0-100

            Log.d(TAG, "[参数] vcn = $currentVoice (string)")
            Log.d(TAG, "[参数] language = 1 (int, 中文)")
            Log.d(TAG, "[参数] textEncoding = UTF-8 (string)")
            Log.d(TAG, "[参数] pitch = 50 (int)")
            Log.d(TAG, "[参数] volume = $currentVolume (int)")
            Log.d(TAG, "[参数] speed = $currentSpeed (int)")
            Log.d(TAG, "---------- 参数构建完成 ----------")

            val builtParams = paramBuilder.build()
            if (builtParams == null) {
                Log.e(TAG, "参数构建失败，builtParams 为 null")
                onSpeakCompletedListener?.invoke()
                onSpeakCompletedListener = null
                return
            }

            Log.d(TAG, "调用 AiHelper.getInst().start()...")
            aiHandle = AiHelper.getInst().start(ABILITY_ID, builtParams, null)

            val startCode = aiHandle?.code ?: -1
            Log.d(TAG, "start() 返回码: $startCode")

            when (startCode) {
                0 -> Log.d(TAG, "start() 成功 - 开始合成")
                18500 -> Log.e(TAG, "start() 失败: 错误码 18500 - 参数校验失败，请检查上方参数日志")
                10101 -> Log.e(TAG, "start() 失败: 错误码 10101 - 引擎未初始化")
                10102 -> Log.e(TAG, "start() 失败: 错误码 10102 - 引擎已初始化")
                10103 -> Log.e(TAG, "start() 失败: 错误码 10103 - 引擎初始化失败")
                10104 -> Log.e(TAG, "start() 失败: 错误码 10104 - 引擎未授权")
                10105 -> Log.e(TAG, "start() 失败: 错误码 10105 - 引擎授权失败")
                10106 -> Log.e(TAG, "start() 失败: 错误码 10106 - 引擎参数错误")
                10107 -> Log.e(TAG, "start() 失败: 错误码 10107 - 引擎不支持该功能")
                10108 -> Log.e(TAG, "start() 失败: 错误码 10108 - 引擎资源加载失败")
                10109 -> Log.e(TAG, "start() 失败: 错误码 10109 - 引擎内部错误")
                10110 -> Log.e(TAG, "start() 失败: 错误码 10110 - 引擎正在使用中")
                18405 -> Log.e(TAG, "start() 失败: 错误码 18405 - SDK授权失败")
                18708 -> Log.e(TAG, "start() 失败: 错误码 18708 - 离线能力未激活")
                else -> Log.e(TAG, "start() 失败: 未知错误码 ($startCode)")
            }

            if (startCode != 0) {
                Log.e(TAG, "start失败，终止播报")
                _lastSpeakFailed = true  // ★ 标记失败，供 doSpeakWithRetry 重试
                onSpeakCompletedListener?.invoke()
                onSpeakCompletedListener = null
                return
            }

            Log.d(TAG, "---------- 开始写入文本 ----------")
            Log.d(TAG, "文本内容: \"$text\" (长度: ${text.length})")

            val dataBuilder = AiRequest.builder()
            val input = AiText.get("text").data(text).valid()
            dataBuilder.payload(input)

            val writeRet = AiHelper.getInst().write(dataBuilder.build(), aiHandle)
            Log.d(TAG, "write() 返回码: $writeRet")

            if (writeRet != 0) {
                Log.e(TAG, "write失败，错误码: $writeRet")
                onSpeakCompletedListener?.invoke()
                onSpeakCompletedListener = null
            } else {
                Log.d(TAG, "文本写入成功，等待合成结果...")
            }

        } catch (e: Exception) {
            Log.e(TAG, "播报异常: ${e.message}")
            e.printStackTrace()
            isSpeaking = false
            _currentSpeakSource = ""  // ★ Bug A修复
            _currentSpeakText = ""  // ★ Bug A修复
            SpeechManager.onTtsStopped()
            onSpeakCompletedListener?.invoke()
            onSpeakCompletedListener = null
        }
    }

    fun stop() {
        Log.d(TAG, "停止播报（清空队列，队列大小: ${speakQueue.size}）")
        speakQueue.clear()
        isProcessingQueue = false
        if (aiHandle != null) {
            AiHelper.getInst().end(aiHandle)
            aiHandle = null
        }
        // ★ 立即停止 AudioTrack 播放（不依赖异步消息）
        if (audioTrack != null) {
            try {
                if (audioTrack!!.playState == AudioTrack.PLAYSTATE_PLAYING) {
                    audioTrack!!.stop()
                    Log.d(TAG, "AudioTrack 已立即停止")
                }
            } catch (e: Exception) {
                Log.e(TAG, "AudioTrack stop 异常: ${e.message}")
            }
        }
        audioPlayHandler?.removeCallbacksAndMessages(null)
        isSpeaking = false
        _currentSpeakSource = ""  // ★ Bug A修复
        _currentSpeakText = ""  // ★ Bug A修复
        onSpeakCompletedListener = null
        // ★ 通知 SpeechManager：TTS 已停止
        SpeechManager.onTtsStopped()
    }

    /**
    ★ flush AudioTrack 缓冲区，立即丢弃残余音频数据
    用于唤醒时确保 TTS 立即静音
     */
    fun flushAudioTrack() {
        if (audioTrack != null) {
            try {
                if (audioTrack!!.playState == AudioTrack.PLAYSTATE_PLAYING) {
                    audioTrack!!.pause()   // ★ 先暂停
                    audioTrack!!.flush()   // ★ 再清空缓冲区
                    audioTrack!!.stop()    // ★ 最后停止
                    Log.d(TAG, "AudioTrack 已 pause + flush + stop")
                }
            } catch (e: Exception) {
                Log.e(TAG, "AudioTrack flush 异常: ${e.message}")
            }
        }
    }

    /**
     * ★ 紧急播报：立即打断当前播报，清空队列，直接播报紧急消息
     * 用于障碍物警告（前方近有汽车/人）等需要零延迟播报的场景
     *
     * @param text 紧急消息文本
     * @param onCompleted 播报完成回调
     */
    fun interruptAndSpeak(text: String, onCompleted: (() -> Unit)? = null) {
        Log.d(TAG, "★★★ 紧急播报: \"$text\" — 打断当前播报，清空队列 ★★★")
        // 先停止当前播报和队列
        stop()
        // ★ 延迟200ms让讯飞引擎释放（50ms太短，频繁触发18310错误导致禁音）
        // 18310 = 引擎还在释放中不能立即开始新合成
        audioPlayHandler?.postDelayed({
            doSpeakWithRetry(text, onCompleted, maxRetries = 2)
        }, 200)
    }

    /**
     * ★ 问题3修复：紧急播报但保留导航指令
     * 用于障碍物警告打断后，确保导航指令不会丢失
     * 清空队列前保存最新的GPS消息，清空后重新加入队列
     */
    fun interruptAndSpeakPreserveNav(text: String, onCompleted: (() -> Unit)? = null) {
        Log.d(TAG, "★★★ 紧急播报(保留导航): \"$text\" ★★★")
        // 1. 保存队列中最新的GPS消息
        val lastGpsMessage = speakQueue.lastOrNull { it.source == "gps" }
        // 2. 清空全部
        stop()
        // 3. 如果有GPS消息，重新加入队列
        if (lastGpsMessage != null) {
            Log.d(TAG, "保留导航指令: \"${lastGpsMessage.text}\"")
            speakQueue.add(lastGpsMessage)
        }
        // 4. 播报紧急消息
        audioPlayHandler?.postDelayed({
            doSpeakWithRetry(text, onCompleted, maxRetries = 2)
        }, 200)
    }

    /**
     * ★ 带重试的播报（用于 interruptAndSpeak 后引擎可能未就绪的情况）
     * 18310 错误 = 引擎忙，等待后重试
     */
    private fun doSpeakWithRetry(text: String, onCompleted: (() -> Unit)?, maxRetries: Int, attempt: Int = 0) {
        if (attempt >= maxRetries) {
            Log.e(TAG, "doSpeakWithRetry: $maxRetries 次重试均失败，放弃播报: \"$text\"")
            onCompleted?.invoke()
            processNextInQueue()
            return
        }
        // ★ 检查上次是否 18310 失败
        if (attempt > 0) {
            Log.d(TAG, "doSpeakWithRetry: 第${attempt + 1}次尝试播报: \"$text\"")
        }
        // 用一个标志位检测是否 18310 失败
        _lastSpeakFailed = false
        doSpeak(text) {
            if (_lastSpeakFailed) {
                // 18310 失败，等待300ms后重试
                Log.w(TAG, "doSpeakWithRetry: 播报失败(18310)，${300}ms后重试 (attempt=$attempt)")
                audioPlayHandler?.postDelayed({
                    doSpeakWithRetry(text, onCompleted, maxRetries, attempt + 1)
                }, 300)
            } else {
                onCompleted?.invoke()
            }
        }
    }

    fun destroy() {
        try {
            stop()

            AiHelper.getInst().engineUnInit(ABILITY_ID)

            audioTrack?.release()
            audioTrack = null

            audioPlayHandler?.looper?.quit()
            audioPlayHandler = null
            audioPlayThread = null

            isInitialized = false
            resourceCheckPassed = false
            Log.d(TAG, "离线合成资源已释放")
        } catch (e: Exception) {
            Log.e(TAG, "释放资源异常: ${e.message}")
        }
    }

    fun isReady(): Boolean = isInitialized

    fun isSpeaking(): Boolean = isSpeaking

    // ========== 运行时可调参数 ==========

    private var currentVoice = "xiaoyan"
    private var currentSpeed = 50
    private var currentVolume = 50

    /**
     * 切换发音人
     * @param voiceName "xiaoyan" 或 "xiaofeng"
     */
    fun setVoice(voiceName: String) {
        currentVoice = voiceName
        Log.d(TAG, "切换发音人: $voiceName")
    }

    /**
     * 设置语速
     * @param speed 0-100，50为正常
     */
    fun setSpeed(speed: Int) {
        currentSpeed = speed.coerceIn(0, 100)
        Log.d(TAG, "设置语速: $currentSpeed")
    }

    /**
     * 设置音量
     * @param volume 0-100，50为正常
     */
    fun setVolume(volume: Int) {
        currentVolume = volume.coerceIn(0, 100)
        Log.d(TAG, "设置音量: $currentVolume")
    }
}
