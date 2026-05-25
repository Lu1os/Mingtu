package com.lu1os.mingtuapp

import android.content.Context
import android.graphics.Typeface
import android.os.Bundle
import android.text.InputType
import android.util.Log
import android.util.TypedValue
import android.view.KeyEvent
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.SeekBar
import android.widget.Switch
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat

/**
 * 设置界面（重构版）
 *
 * 功能：
 * 1. 切换发音人（小燕/小峰）— 小峰模型不存在时禁用选项
 * 2. 调节语速 — 安全设置，不会触发竞态
 * 3. 调节音量
 * 4. 调节唤醒灵敏度
 * 5. 触感反馈开关
 * 6. 控制键手柄开关
 * 7. 音量键无障碍导航（通过 KeyHandler）
 *
 * Bug 修复：
 * - 小峰发音人模型不存在：禁用选项 + 显示不可用提示
 * - 进设置页卡顿：设置页自身无耗时操作，不触发 MainActivity 的 onResume 重初始化
 * - 调语速 bug：setSpeed 仅修改参数，不触发 speak()，避免竞态
 */
class SettingsActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "SettingsActivity"
        private const val PREFS_NAME = "mingtu_settings"

        // SharedPreferences Keys
        private const val KEY_VOICE = "voice_name"
        private const val KEY_SPEED = "tts_speed"
        private const val KEY_VOLUME = "tts_volume"
        private const val KEY_THRESHOLD = "kws_threshold"
        private const val KEY_HAPTIC = "haptic_enabled"

        // 唤醒阈值映射范围（SeekBar 0~100 → sherpa-onnx 0.10~0.40）
        private const val THRESHOLD_MIN = 0.10f
        private const val THRESHOLD_MAX = 0.40f

        // 小峰发音人资源文件路径（用于检测是否安装）
        private const val XIAOFENG_IRF_PATH = "iflytek/xtts/xiaofeng.irf"

        // SeekBar 按键调节步长
        private const val SEEK_BAR_STEP = 5
    }

    // Views
    private lateinit var rgVoice: RadioGroup
    private lateinit var rbXiaoyan: RadioButton
    private lateinit var rbXiaofeng: RadioButton
    private lateinit var seekbarSpeed: SeekBar
    private lateinit var seekbarVolume: SeekBar
    private lateinit var seekbarThreshold: SeekBar
    private lateinit var tvSpeedValue: TextView
    private lateinit var tvVolumeValue: TextView
    private lateinit var tvThresholdValue: TextView
    private lateinit var switchHaptic: Switch
    private lateinit var switchKeyHandler: Switch

    // 缓存 SharedPreferences
    private lateinit var prefs: android.content.SharedPreferences

    // 小峰模型是否可用
    private var isXiaofengAvailable = false

    // 防止 RadioGroup 初始化时触发监听器
    private var isInitializing = true

    // ========== SeekBar 统一监听器 ==========
    private val seekBarListener = object : SeekBar.OnSeekBarChangeListener {
        override fun onProgressChanged(seekBar: SeekBar?, progress: Int, fromUser: Boolean) {
            if (!fromUser) return
            when (seekBar?.id) {
                R.id.seekbar_speed -> tvSpeedValue.text = getSpeedLabel(progress)
                R.id.seekbar_volume -> tvVolumeValue.text = getString(R.string.volume_percent, progress)
                R.id.seekbar_threshold -> tvThresholdValue.text = getThresholdLabel(progress)
            }
        }

        override fun onStartTrackingTouch(seekBar: SeekBar?) {}

        override fun onStopTrackingTouch(seekBar: SeekBar?) {
            if (seekBar == null) return
            when (seekBar.id) {
                R.id.seekbar_speed -> {
                    val speed = seekBar.progress
                    saveSetting(KEY_SPEED, speed)
                    TtsManager.setSpeed(speed)
                    Log.d(TAG, "设置语速: $speed")
                }
                R.id.seekbar_volume -> {
                    val volume = seekBar.progress
                    saveSetting(KEY_VOLUME, volume)
                    TtsManager.setVolume(volume)
                    Log.d(TAG, "设置音量: $volume")
                }
                R.id.seekbar_threshold -> {
                    val percent = seekBar.progress
                    saveSetting(KEY_THRESHOLD, percent)
                    val threshold = THRESHOLD_MIN + (percent / 100f) * (THRESHOLD_MAX - THRESHOLD_MIN)
                    SpeechManager.setThreshold(threshold)
                    Log.d(TAG, "设置唤醒门限: $threshold")
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_settings)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        initViews()
        checkVoiceAvailability()
        loadSettings()
        setupListeners()

        // 初始化按键手柄（进入设置页浏览模式）
        KeyHandler.enterSettingsMode()
        KeyHandler.setSettingsCallback(settingsKeyHandlerCallback)

        isInitializing = false
    }

    override fun dispatchKeyEvent(event: KeyEvent): Boolean {
        if (KeyHandler.onKeyEvent(event)) {
            return true
        }
        return super.dispatchKeyEvent(event)
    }

    override fun onDestroy() {
        super.onDestroy()
        KeyHandler.setSettingsCallback(null)
    }

    private fun initViews() {
        // 返回按钮
        findViewById<ImageButton>(R.id.btn_back).setOnClickListener { finish() }

        // 语音设置
        rgVoice = findViewById(R.id.rg_voice)
        rbXiaoyan = findViewById(R.id.rb_xiaoyan)
        rbXiaofeng = findViewById(R.id.rb_xiaofeng)

        // SeekBar
        seekbarSpeed = findViewById(R.id.seekbar_speed)
        seekbarVolume = findViewById(R.id.seekbar_volume)
        seekbarThreshold = findViewById(R.id.seekbar_threshold)

        // 数值显示
        tvSpeedValue = findViewById(R.id.tv_speed_value)
        tvVolumeValue = findViewById(R.id.tv_volume_value)
        tvThresholdValue = findViewById(R.id.tv_threshold_value)

        // 开关
        switchHaptic = findViewById(R.id.switch_haptic)

        // 控制键手柄开关（XML 中已定义）
        switchKeyHandler = findViewById(R.id.switch_key_handler)

        // 试听按钮
        findViewById<Button>(R.id.btn_preview_voice).setOnClickListener { previewVoice() }

        // ★ 服务器 IP 地址设置
        findViewById<LinearLayout>(R.id.layout_server_ip)?.setOnClickListener {
            showIpEditDialog()
        }

        // 帮助中心
        findViewById<LinearLayout>(R.id.layout_help).setOnClickListener {
            Toast.makeText(this, R.string.help_center_in_dev, Toast.LENGTH_SHORT).show()
        }
    }

    /**
     * 检测小峰发音人模型是否已安装
     */
    private fun checkVoiceAvailability() {
        isXiaofengAvailable = try {
            assets.open(XIAOFENG_IRF_PATH).close()
            true
        } catch (e: Exception) {
            false
        }

        if (!isXiaofengAvailable) {
            rbXiaofeng.isEnabled = false
            rbXiaofeng.alpha = 0.4f

            val hint = TextView(this).apply {
                text = "小峰发音人模型暂未安装，当前不可用"
                setTextColor(ContextCompat.getColor(this@SettingsActivity, R.color.gray_500))
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 11f)
                typeface = Typeface.DEFAULT
                setPadding(0, dpToPx(8), 0, 0)
            }
            rgVoice.addView(hint)

            Log.w(TAG, "小峰发音人模型未安装，已禁用该选项")
        }
    }

    private fun dpToPx(dp: Int): Int {
        return (dp * resources.displayMetrics.density).toInt()
    }

    private fun loadSettings() {
        // 发音人
        val voice = prefs.getString(KEY_VOICE, "xiaoyan") ?: "xiaoyan"
        when {
            voice == "xiaofeng" && isXiaofengAvailable -> rbXiaofeng.isChecked = true
            else -> rbXiaoyan.isChecked = true
        }

        // 语速
        val speed = prefs.getInt(KEY_SPEED, 50)
        seekbarSpeed.progress = speed
        tvSpeedValue.text = getSpeedLabel(speed)

        // 音量
        val volume = prefs.getInt(KEY_VOLUME, 50)
        seekbarVolume.progress = volume
        tvVolumeValue.text = getString(R.string.volume_percent, volume)

        // 唤醒灵敏度
        val thresholdPercent = prefs.getInt(KEY_THRESHOLD, 50)
        seekbarThreshold.progress = thresholdPercent
        tvThresholdValue.text = getThresholdLabel(thresholdPercent)

        // 触感反馈
        switchHaptic.isChecked = prefs.getBoolean(KEY_HAPTIC, false)

        // 控制键手柄
        switchKeyHandler.isChecked = KeyHandler.isEnabled

        // ★ 加载已保存的服务器 IP 地址
        val savedIp = AppConfig.getServerIp(this)
        updateIpDisplay(savedIp)
    }

    private fun setupListeners() {
        // 发音人切换
        rgVoice.setOnCheckedChangeListener { _, checkedId ->
            if (isInitializing) return@setOnCheckedChangeListener

            if (checkedId == R.id.rb_xiaofeng && !isXiaofengAvailable) {
                rbXiaoyan.isChecked = true
                Toast.makeText(this, R.string.voice_not_available, Toast.LENGTH_SHORT).show()
                return@setOnCheckedChangeListener
            }

            val voiceName = if (checkedId == R.id.rb_xiaofeng) "xiaofeng" else "xiaoyan"
            saveSetting(KEY_VOICE, voiceName)
            TtsManager.setVoice(voiceName)
            Log.d(TAG, "切换发音人: $voiceName")
        }

        // 三个 SeekBar 共用一个监听器
        seekbarSpeed.setOnSeekBarChangeListener(seekBarListener)
        seekbarVolume.setOnSeekBarChangeListener(seekBarListener)
        seekbarThreshold.setOnSeekBarChangeListener(seekBarListener)

        // 触感反馈
        switchHaptic.setOnCheckedChangeListener { _, isChecked ->
            saveSetting(KEY_HAPTIC, isChecked)
            Log.d(TAG, "触感反馈: $isChecked")
        }

        // 控制键手柄开关
        switchKeyHandler.setOnCheckedChangeListener { _, isChecked ->
            KeyHandler.setEnabled(isChecked, this)
            TtsManager.speak(if (isChecked) "控制键手柄已开启" else "控制键手柄已关闭")
            Log.d(TAG, "控制键手柄: $isChecked")
        }
    }

    private fun previewVoice() {
        val selectedId = rgVoice.checkedRadioButtonId
        if (selectedId == R.id.rb_xiaofeng && !isXiaofengAvailable) {
            Toast.makeText(this, R.string.voice_not_available, Toast.LENGTH_SHORT).show()
            return
        }

        val voiceName = if (selectedId == R.id.rb_xiaofeng) "xiaofeng" else "xiaoyan"
        val displayName = if (voiceName == "xiaofeng") "小峰" else "小燕"
        TtsManager.setVoice(voiceName)
        TtsManager.speak("您好，我是${displayName}，很高兴为您服务。")
    }

    // ==================== 按键手柄回调 ====================

    private val settingsKeyHandlerCallback = object : KeyHandler.KeyHandlerCallback {
        override fun onMainNavItemChanged(index: Int, name: String) {
            // 设置页不处理主页事件
        }

        override fun onMainNavConfirmed(index: Int, name: String) {
            // 设置页不处理主页事件
        }

        override fun onSettingsNavItemChanged(index: Int, name: String) {
            // KeyHandler 已负责播报，这里只处理 UI 更新（如需要）
        }

        override fun onSettingsNavEnterControl(index: Int, name: String) {
            runOnUiThread {
                KeyHandler.enterControlMode()
                when (index) {
                    0 -> {
                        // 播报音色：切换发音人
                        TtsManager.speak("播报音色，按加键切换到小峰，按减键切换到小燕")
                    }
                    1 -> {
                        // 语速调节
                        TtsManager.speak("语速调节，按加键加快，按减键减慢，当前${tvSpeedValue.text}")
                    }
                    2 -> {
                        // 音量调节
                        TtsManager.speak("音量调节，按加键增大，按减键减小，当前${tvVolumeValue.text}")
                    }
                    3 -> {
                        // 唤醒灵敏度
                        TtsManager.speak("唤醒灵敏度，按加键降低灵敏度，按减键提高灵敏度，当前${tvThresholdValue.text}")
                    }
                    4 -> {
                        // 控制键手柄
                        val state = if (switchKeyHandler.isChecked) "已开启" else "已关闭"
                        TtsManager.speak("控制键手柄，按加键开启，按减键关闭，当前$state")
                    }
                    5 -> {
                        // 触感反馈
                        val state = if (switchHaptic.isChecked) "已开启" else "已关闭"
                        TtsManager.speak("触感反馈，按加键开启，按减键关闭，当前$state")
                    }
                    6 -> {
                        // 帮助中心
                        TtsManager.speak("帮助中心功能开发中")
                        // 自动退出控制模式
                        KeyHandler.exitControlMode()
                    }
                }
            }
        }

        override fun onSettingsNavExitControl() {
            runOnUiThread {
                val name = KeyHandler.getCurrentSettingsItemName()
                TtsManager.speak("已退出$name")
            }
        }

        override fun onSettingsExit() {
            runOnUiThread {
                finish()
            }
        }

        override fun onSettingsControlAction(isIncrease: Boolean) {
            runOnUiThread {
                val index = KeyHandler.getSettingsNavIndex()
                when (index) {
                    0 -> handleVoiceControl(isIncrease)
                    1 -> handleSpeedControl(isIncrease)
                    2 -> handleVolumeControl(isIncrease)
                    3 -> handleThresholdControl(isIncrease)
                    4 -> handleKeyHandlerControl(isIncrease)
                    5 -> handleHapticControl(isIncrease)
                    6 -> {
                        // 帮助中心在控制模式无操作
                    }
                }
            }
        }
    }

    // ========== 各设置项的按键控制逻辑 ==========

    private fun handleVoiceControl(isIncrease: Boolean) {
        if (!isXiaofengAvailable) {
            TtsManager.speak("小峰发音人模型暂未安装")
            return
        }
        if (isIncrease) {
            rbXiaofeng.isChecked = true
            TtsManager.speak("小峰")
        } else {
            rbXiaoyan.isChecked = true
            TtsManager.speak("小燕")
        }
    }

    private fun handleSpeedControl(isIncrease: Boolean) {
        val current = seekbarSpeed.progress
        val newProgress = (current + if (isIncrease) SEEK_BAR_STEP else -SEEK_BAR_STEP)
            .coerceIn(0, 100)
        seekbarSpeed.progress = newProgress
        tvSpeedValue.text = getSpeedLabel(newProgress)
        saveSetting(KEY_SPEED, newProgress)
        TtsManager.setSpeed(newProgress)
        TtsManager.speak("语速${tvSpeedValue.text}")
    }

    private fun handleVolumeControl(isIncrease: Boolean) {
        val current = seekbarVolume.progress
        val newProgress = (current + if (isIncrease) SEEK_BAR_STEP else -SEEK_BAR_STEP)
            .coerceIn(0, 100)
        seekbarVolume.progress = newProgress
        tvVolumeValue.text = getString(R.string.volume_percent, newProgress)
        saveSetting(KEY_VOLUME, newProgress)
        TtsManager.setVolume(newProgress)
        TtsManager.speak("音量${tvVolumeValue.text}")
    }

    private fun handleThresholdControl(isIncrease: Boolean) {
        val current = seekbarThreshold.progress
        val newProgress = (current + if (isIncrease) SEEK_BAR_STEP else -SEEK_BAR_STEP)
            .coerceIn(0, 100)
        seekbarThreshold.progress = newProgress
        tvThresholdValue.text = getThresholdLabel(newProgress)
        saveSetting(KEY_THRESHOLD, newProgress)
        val threshold = THRESHOLD_MIN + (newProgress / 100f) * (THRESHOLD_MAX - THRESHOLD_MIN)
        SpeechManager.setThreshold(threshold)
        TtsManager.speak("灵敏度${tvThresholdValue.text}")
    }

    private fun handleHapticControl(isIncrease: Boolean) {
        // 先移除监听器，避免程序设置 Switch 状态时触发监听器导致重复保存
        switchHaptic.setOnCheckedChangeListener(null)
        switchHaptic.isChecked = isIncrease
        saveSetting(KEY_HAPTIC, isIncrease)
        // 恢复监听器
        switchHaptic.setOnCheckedChangeListener { _, isChecked ->
            saveSetting(KEY_HAPTIC, isChecked)
            Log.d(TAG, "触感反馈: $isChecked")
        }
        TtsManager.speak("触感反馈${if (isIncrease) "已开启" else "已关闭"}")
    }

    private fun handleKeyHandlerControl(isIncrease: Boolean) {
        // 先移除监听器，避免程序设置 Switch 状态时触发监听器导致重复调用 setEnabled
        switchKeyHandler.setOnCheckedChangeListener(null)
        switchKeyHandler.isChecked = isIncrease
        KeyHandler.setEnabled(isIncrease, this)
        // 恢复监听器
        switchKeyHandler.setOnCheckedChangeListener { _, isChecked ->
            KeyHandler.setEnabled(isChecked, this)
            TtsManager.speak(if (isChecked) "控制键手柄已开启" else "控制键手柄已关闭")
            Log.d(TAG, "控制键手柄: $isChecked")
        }
        TtsManager.speak("控制键手柄${if (isIncrease) "已开启" else "已关闭"}")
    }

    // ========== 标签映射 ==========

    private fun getSpeedLabel(progress: Int): String = when {
        progress <= 20 -> getString(R.string.speed_very_slow)
        progress <= 40 -> getString(R.string.speed_slow)
        progress <= 60 -> getString(R.string.speed_normal)
        progress <= 80 -> getString(R.string.speed_fast)
        else -> getString(R.string.speed_very_fast)
    }

    private fun getThresholdLabel(progress: Int): String = when {
        progress <= 20 -> getString(R.string.threshold_very_high)
        progress <= 40 -> getString(R.string.threshold_high)
        progress <= 60 -> getString(R.string.threshold_normal)
        progress <= 80 -> getString(R.string.threshold_low)
        else -> getString(R.string.threshold_very_low)
    }

    // ========== SharedPreferences 工具 ==========

    /**
     * ★ 显示 IP 地址编辑对话框
     */
    private fun showIpEditDialog() {
        val currentIp = AppConfig.getServerIp(this)
        val dp = { value: Int -> (value * resources.displayMetrics.density).toInt() }

        // ★ 自定义深色风格对话框，和设置页面风格一致
        val dialog = AlertDialog.Builder(this).create()

        // 外层容器
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(24), dp(24), dp(8))
        }

        // 标题
        val title = TextView(this).apply {
            text = "服务器 IP 地址"
            setTextColor(0xFFFFFFFF.toInt())
            textSize = 20f
            typeface = Typeface.DEFAULT_BOLD
        }
        container.addView(title)

        // 说明文字
        val desc = TextView(this).apply {
            text = "请输入电脑的局域网 IP 地址，确保手机和电脑在同一个 WiFi 下"
            setTextColor(0xFF9CA3AF.toInt())
            textSize = 13f
            setPadding(0, dp(8), 0, dp(16))
        }
        container.addView(desc)

        // 输入框容器（玻璃面板风格）
        val inputContainer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundResource(R.drawable.bg_glass_panel)
            setPadding(dp(16), dp(4), dp(16), dp(16))
        }

        // 输入框
        val input = EditText(this).apply {
            setText(currentIp)
            inputType = InputType.TYPE_CLASS_PHONE or InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD
            setTextColor(0xFFFFFFFF.toInt())
            setHintTextColor(0xFF6B7280.toInt())
            hint = "例如: 192.168.1.100"
            textSize = 18f
            setSingleLine()
            setPadding(0, dp(12), 0, dp(12))
            background = null  // 去掉默认下划线
            selectAll()
        }
        inputContainer.addView(input)
        container.addView(inputContainer)

        // 按钮容器
        val btnContainer = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(20), 0, dp(4))
            gravity = android.view.Gravity.END
        }

        // 取消按钮
        val btnCancel = TextView(this).apply {
            text = "取消"
            setTextColor(0xFF9CA3AF.toInt())
            textSize = 15f
            setPadding(dp(16), dp(12), dp(16), dp(12))
            isClickable = true
            setOnClickListener { dialog.dismiss() }
        }
        btnContainer.addView(btnCancel)

        // 保存按钮
        val btnSave = TextView(this).apply {
            text = "保存"
            setTextColor(0xFF3B82F6.toInt())
            textSize = 15f
            typeface = Typeface.DEFAULT_BOLD
            setPadding(dp(16), dp(12), dp(16), dp(12))
            isClickable = true
            setOnClickListener {
                val newIp = input.text.toString().trim()
                if (isValidIp(newIp)) {
                    AppConfig.setServerIp(this@SettingsActivity, newIp)
                    updateIpDisplay(newIp)
                    Toast.makeText(this@SettingsActivity, "IP 已保存为 $newIp，重启 App 后生效", Toast.LENGTH_LONG).show()
                    Log.d(TAG, "服务器 IP 已更新: $newIp")
                    dialog.dismiss()
                } else {
                    Toast.makeText(this@SettingsActivity, "IP 地址格式不正确，请重新输入", Toast.LENGTH_SHORT).show()
                }
            }
        }
        btnContainer.addView(btnSave)
        container.addView(btnContainer)

        dialog.setView(container)
        dialog.window?.setBackgroundDrawableResource(android.R.color.transparent)
        dialog.show()
    }

    /**
     * ★ 更新 IP 地址显示
     */
    private fun updateIpDisplay(ip: String) {
        findViewById<TextView>(R.id.tv_server_ip_value)?.text = ip
    }

    /**
     * ★ 简单 IP 地址格式校验
     */
    private fun isValidIp(ip: String): Boolean {
        if (ip.isBlank()) return false
        val parts = ip.split(".")
        if (parts.size != 4) return false
        return parts.all { part ->
            part.toIntOrNull()?.let { it in 0..255 } == true
        }
    }

    // ========== SharedPreferences 工具 ==========

    private fun saveSetting(key: String, value: Any) {
        prefs.edit().apply {
            when (value) {
                is String -> putString(key, value)
                is Int -> putInt(key, value)
                is Boolean -> putBoolean(key, value)
            }
            apply()
        }
    }
}
