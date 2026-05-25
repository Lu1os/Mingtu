package com.lu1os.mingtuapp

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.Vibrator
import android.util.Log
import android.view.KeyEvent

/**
 * 物理按键手柄控制器
 *
 * 用音量键替代触摸屏操作，为视障用户提供无障碍导航。
 *
 * 按键映射：
 * - 音量+（上键）：选择上一个选项 / 控制模式中增加
 * - 音量-（下键）：选择下一个选项 / 控制模式中减少
 * - 双击音量+：进入/确认当前选项
 * - 双击音量-：退出/返回上一层
 *
 * 状态机：
 * MAIN_NAV → 双击+进入设置页 → SETTINGS_NAV → 双击+进入控制 → SETTINGS_CONTROL
 * SETTINGS_CONTROL → 双击-退出 → SETTINGS_NAV → 双击-退出 → MAIN_NAV
 */
object KeyHandler {

    private const val TAG = "KeyHandler"
    private const val PREFS_NAME = "mingtu_settings"
    private const val KEY_ENABLED = "key_handler_enabled"

    enum class Mode {
        MAIN_NAV,
        SETTINGS_NAV,
        SETTINGS_CONTROL
    }

    private val mainNavItems = listOf("首页", "SOS", "设置")
    private var mainNavIndex = 0

    private val settingsNavItems = listOf(
        "播报音色", "语速调节", "音量调节",
        "唤醒灵敏度", "控制键手柄", "触感反馈", "帮助中心"
    )
    private var settingsNavIndex = 0

    private val DOUBLE_CLICK_TIMEOUT = 350L
    private var lastPlusPressTime = 0L
    private var lastMinusPressTime = 0L
    private var pendingPlusClick = false
    private var pendingMinusClick = false
    private val handler = Handler(Looper.getMainLooper())
    private var pendingPlusRunnable: Runnable? = null
    private var pendingMinusRunnable: Runnable? = null

    var currentMode = Mode.MAIN_NAV
        private set

    var isEnabled = false
        private set

    interface KeyHandlerCallback {
        fun onMainNavItemChanged(index: Int, name: String)
        fun onMainNavConfirmed(index: Int, name: String)
        fun onSettingsNavItemChanged(index: Int, name: String)
        fun onSettingsNavEnterControl(index: Int, name: String)
        fun onSettingsNavExitControl()
        fun onSettingsExit()
        fun onSettingsControlAction(isIncrease: Boolean)
    }

    private var mainCallback: KeyHandlerCallback? = null
    private var settingsCallback: KeyHandlerCallback? = null
    private var appContext: Context? = null

    // ★ 问题6修复：双击直接聆听回调
    var onDoubleTapForListening: (() -> Unit)? = null

    fun init(context: Context) {
        appContext = context
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        isEnabled = prefs.getBoolean(KEY_ENABLED, false)
        Log.d(TAG, "初始化，手柄功能: ${if (isEnabled) "开启" else "关闭"}")
    }

    /** 设置主页回调（MainActivity 调用） */
    fun setMainCallback(cb: KeyHandlerCallback?) {
        mainCallback = cb
        Log.d(TAG, "设置主页回调: ${if (cb != null) "已设置" else "已清空"}")
    }

    /** 设置设置页回调（SettingsActivity 调用） */
    fun setSettingsCallback(cb: KeyHandlerCallback?) {
        settingsCallback = cb
        Log.d(TAG, "设置设置页回调: ${if (cb != null) "已设置" else "已清空"}")
    }

    /** @deprecated 兼容旧代码，同时设置两个回调 */
    fun setCallback(cb: KeyHandlerCallback?) {
        mainCallback = cb
        settingsCallback = cb
    }

    /** 获取当前活跃的 callback */
    private fun getCallback(): KeyHandlerCallback? {
        return when (currentMode) {
            Mode.MAIN_NAV -> mainCallback
            Mode.SETTINGS_NAV, Mode.SETTINGS_CONTROL -> settingsCallback
        }
    }

    fun setEnabled(enabled: Boolean, context: Context) {
        isEnabled = enabled
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            .edit().putBoolean(KEY_ENABLED, enabled).apply()
        Log.d(TAG, "手柄功能: ${if (enabled) "开启" else "关闭"}")
    }

    fun enterSettingsMode() {
        currentMode = Mode.SETTINGS_NAV
        settingsNavIndex = 0
        clearPendingState()
        Log.d(TAG, "进入设置页浏览模式，当前选项: ${settingsNavItems[0]}")
    }

    fun enterMainNavMode() {
        currentMode = Mode.MAIN_NAV
        mainNavIndex = 0
        clearPendingState()
        Log.d(TAG, "进入主页导航模式，当前选项: ${mainNavItems[0]}")
    }

    fun enterControlMode() {
        currentMode = Mode.SETTINGS_CONTROL
        clearPendingState()
        Log.d(TAG, "进入设置控制模式，当前选项: ${settingsNavItems[settingsNavIndex]}")
    }

    fun exitControlMode() {
        currentMode = Mode.SETTINGS_NAV
        clearPendingState()
        getCallback()?.onSettingsNavExitControl()
        Log.d(TAG, "退出设置控制模式，回到浏览模式")
    }

    fun onKeyEvent(event: KeyEvent): Boolean {
        if (!isEnabled) return false
        if (event.action != KeyEvent.ACTION_DOWN) return false
        if (event.repeatCount > 0) return true

        when (event.keyCode) {
            KeyEvent.KEYCODE_VOLUME_UP -> {
                handlePlusKey()
                return true
            }
            KeyEvent.KEYCODE_VOLUME_DOWN -> {
                handleMinusKey()
                return true
            }
        }
        return false
    }

    /**
     * 音量+（上键）处理
     * - 浏览模式：上一个选项
     * - 控制模式：增加操作
     * - 双击：进入/确认
     */
    private fun handlePlusKey() {
        val now = System.currentTimeMillis()

        when (currentMode) {
            Mode.MAIN_NAV -> {
                if (pendingPlusClick && (now - lastPlusPressTime) < DOUBLE_CLICK_TIMEOUT) {
                    cancelPendingPlus()
                    // ★ 问题6修复：双击音量键直接进入聆听
                    onDoubleTapForListening?.invoke()
                    val name = mainNavItems[mainNavIndex]
                    Log.d(TAG, "主页双击+：确认进入 [$name], index=$mainNavIndex")
                    getCallback()?.onMainNavConfirmed(mainNavIndex, name)
                } else {
                    cancelPendingPlus()
                    pendingPlusClick = true
                    pendingPlusRunnable = Runnable {
                        if (pendingPlusClick) {
                            pendingPlusClick = false
                            // 音量+ = 上一个选项
                            mainNavIndex = (mainNavIndex - 1 + mainNavItems.size) % mainNavItems.size
                            val name = mainNavItems[mainNavIndex]
                            Log.d(TAG, "主页单击+：上一个选项 [$name], index=$mainNavIndex")
                            vibrate()
                            TtsManager.speak(name)
                            getCallback()?.onMainNavItemChanged(mainNavIndex, name)
                        }
                    }
                    handler.postDelayed(pendingPlusRunnable!!, DOUBLE_CLICK_TIMEOUT)
                }
                lastPlusPressTime = now
            }

            Mode.SETTINGS_NAV -> {
                if (pendingPlusClick && (now - lastPlusPressTime) < DOUBLE_CLICK_TIMEOUT) {
                    cancelPendingPlus()
                    // ★ 问题6修复：双击音量键直接进入聆听
                    onDoubleTapForListening?.invoke()
                    val name = settingsNavItems[settingsNavIndex]
                    Log.d(TAG, "设置页双击+：进入控制 [$name], index=$settingsNavIndex")
                    getCallback()?.onSettingsNavEnterControl(settingsNavIndex, name)
                } else {
                    cancelPendingPlus()
                    pendingPlusClick = true
                    pendingPlusRunnable = Runnable {
                        if (pendingPlusClick) {
                            pendingPlusClick = false
                            // 音量+ = 上一个选项
                            settingsNavIndex = (settingsNavIndex - 1 + settingsNavItems.size) % settingsNavItems.size
                            val name = settingsNavItems[settingsNavIndex]
                            Log.d(TAG, "设置页单击+：上一个选项 [$name], index=$settingsNavIndex")
                            vibrate()
                            TtsManager.speak(name)
                            getCallback()?.onSettingsNavItemChanged(settingsNavIndex, name)
                        }
                    }
                    handler.postDelayed(pendingPlusRunnable!!, DOUBLE_CLICK_TIMEOUT)
                }
                lastPlusPressTime = now
            }

            Mode.SETTINGS_CONTROL -> {
                Log.d(TAG, "设置控制模式：+ 增加, 当前选项 index=$settingsNavIndex")
                getCallback()?.onSettingsControlAction(isIncrease = true)
            }
        }
    }

    /**
     * 音量-（下键）处理
     * - 浏览模式：下一个选项
     * - 控制模式：减少操作
     * - 双击：退出/返回
     */
    private fun handleMinusKey() {
        val now = System.currentTimeMillis()

        when (currentMode) {
            Mode.MAIN_NAV -> {
                if (pendingMinusClick && (now - lastMinusPressTime) < DOUBLE_CLICK_TIMEOUT) {
                    cancelPendingMinus()
                    Log.d(TAG, "主页双击-：无操作（主页是顶层）")
                } else {
                    cancelPendingMinus()
                    pendingMinusClick = true
                    pendingMinusRunnable = Runnable {
                        if (pendingMinusClick) {
                            pendingMinusClick = false
                            // 音量- = 下一个选项
                            mainNavIndex = (mainNavIndex + 1) % mainNavItems.size
                            val name = mainNavItems[mainNavIndex]
                            Log.d(TAG, "主页单击-：下一个选项 [$name], index=$mainNavIndex")
                            vibrate()
                            TtsManager.speak(name)
                            getCallback()?.onMainNavItemChanged(mainNavIndex, name)
                        }
                    }
                    handler.postDelayed(pendingMinusRunnable!!, DOUBLE_CLICK_TIMEOUT)
                }
                lastMinusPressTime = now
            }

            Mode.SETTINGS_NAV -> {
                if (pendingMinusClick && (now - lastMinusPressTime) < DOUBLE_CLICK_TIMEOUT) {
                    cancelPendingMinus()
                    // ★ 问题6修复：双击音量键直接进入聆听
                    onDoubleTapForListening?.invoke()
                    Log.d(TAG, "设置页双击-：退出设置页，返回主页")
                    getCallback()?.onSettingsExit()
                } else {
                    cancelPendingMinus()
                    pendingMinusClick = true
                    pendingMinusRunnable = Runnable {
                        if (pendingMinusClick) {
                            pendingMinusClick = false
                            // 音量- = 下一个选项
                            settingsNavIndex = (settingsNavIndex + 1) % settingsNavItems.size
                            val name = settingsNavItems[settingsNavIndex]
                            Log.d(TAG, "设置页单击-：下一个选项 [$name], index=$settingsNavIndex")
                            vibrate()
                            TtsManager.speak(name)
                            getCallback()?.onSettingsNavItemChanged(settingsNavIndex, name)
                        }
                    }
                    handler.postDelayed(pendingMinusRunnable!!, DOUBLE_CLICK_TIMEOUT)
                }
                lastMinusPressTime = now
            }

            Mode.SETTINGS_CONTROL -> {
                if (pendingMinusClick && (now - lastMinusPressTime) < DOUBLE_CLICK_TIMEOUT) {
                    cancelPendingMinus()
                    // ★ 问题6修复：双击音量键直接进入聆听
                    onDoubleTapForListening?.invoke()
                    Log.d(TAG, "设置控制模式双击-：退出控制模式")
                    exitControlMode()
                } else {
                    cancelPendingMinus()
                    pendingMinusClick = true
                    pendingMinusRunnable = Runnable {
                        if (pendingMinusClick) {
                            pendingMinusClick = false
                            Log.d(TAG, "设置控制模式单击-：减少, 当前选项 index=$settingsNavIndex")
                            getCallback()?.onSettingsControlAction(isIncrease = false)
                        }
                    }
                    handler.postDelayed(pendingMinusRunnable!!, DOUBLE_CLICK_TIMEOUT)
                }
                lastMinusPressTime = now
            }
        }
    }

    fun getSettingsNavIndex(): Int = settingsNavIndex
    fun getMainNavIndex(): Int = mainNavIndex
    fun getCurrentSettingsItemName(): String = settingsNavItems[settingsNavIndex]

    private fun cancelPendingPlus() {
        pendingPlusRunnable?.let { handler.removeCallbacks(it) }
        pendingPlusRunnable = null
        pendingPlusClick = false
    }

    private fun cancelPendingMinus() {
        pendingMinusRunnable?.let { handler.removeCallbacks(it) }
        pendingMinusRunnable = null
        pendingMinusClick = false
    }

    private fun clearPendingState() {
        cancelPendingPlus()
        cancelPendingMinus()
    }

    /**
     * 触感反馈：短震动
     * 每次选项切换时调用，不依赖手柄开关是否开启
     */
    fun vibrate() {
        val ctx = appContext ?: return
        val prefs = ctx.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val hapticEnabled = prefs.getBoolean("haptic_enabled", false)
        if (!hapticEnabled) return
        try {
            val vibrator = ctx.getSystemService(Context.VIBRATOR_SERVICE) as? Vibrator
            vibrator?.vibrate(30L)
        } catch (e: Exception) {
            Log.w(TAG, "震动反馈失败: ${e.message}")
        }
    }
}
