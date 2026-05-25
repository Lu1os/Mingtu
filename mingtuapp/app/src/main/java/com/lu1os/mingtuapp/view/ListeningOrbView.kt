package com.lu1os.mingtuapp.view

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.*
import android.util.AttributeSet
import android.util.Log
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator
import kotlin.math.abs
import kotlin.math.sin

/**
 * 语音唤醒动画 View —— 玻璃质感波形动画
 *
 * 状态:
 *   IDLE(隐藏) → LISTENING(胶囊+波形呼吸脉冲 → 平静等待) → SPEAKING(波形随声音波动) → IDLE
 *   任意状态 → THINKING(蓝紫色脉冲) → IDLE
 */
class ListeningOrbView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    enum class State {
        IDLE,       // 隐藏
        LISTENING,  // 胶囊 + 波形（呼吸脉冲 → 平静等待用户说话）
        SPEAKING,   // 波形随声音实时波动
        THINKING    // 蓝紫色缓慢脉冲（AI思考）
    }

    // ===== 动画参数 =====
    private val WAVE_POINTS = 60                    // 波浪线采样点数（越多越平滑）
    private val WAVE_STROKE_WIDTH = 2.5f            // 波浪线粗细 dp
    private val WAVE_SPEED = 0.003f                 // 波浪流动速度（越小越慢越平滑）
    private val WAVE_FREQUENCY = 2.5f               // 波浪频率（几个波峰）
    private val CAPSULE_MIN_HEIGHT = 28f           // 胶囊最小高度 dp
    private val CAPSULE_MAX_HEIGHT = 100f          // 胶囊最大高度 dp
    private val CAPSULE_WIDTH_RATIO = 0.80f        // 胶囊宽度占 view 宽度
    private val CAPSULE_RADIUS = 14f               // 胶囊圆角 dp
    private val BALL_SIZE = 48f                    // 初始圆球大小 dp
    private val ANIM_WAKEUP_DURATION = 900L        // 唤醒动画时长 ms
    private val ANIM_COLLAPSE_DURATION = 500L      // 收缩动画时长 ms
    private val BREATH_PERIOD = 2200L              // 呼吸脉冲周期 ms

    // ===== 运行时状态 =====
    private var currentState = State.IDLE
    private var audioLevel = 0f                    // 0~1 音量
    private var smoothAudioLevel = 0f              // 平滑后的音量
    private var capsuleHeight = 0f                 // 当前胶囊高度 px
    private var capsuleWidth = 0f                  // 当前胶囊宽度 px
    private var capsuleAlpha = 0f                  // 胶囊透明度 0~1
    private var ballScale = 0f
    private var ballAlpha = 0f
    private var breathPhase = 0f                   // 呼吸脉冲相位
    private var isThinking = false
    private var thinkPhase = 0f
    private var wavePhase = 0f                     // ★ 波浪流动相位（持续递增，实现平滑流动）

    // ===== 动画器 =====
    private var mainAnimator: ValueAnimator? = null
    private var breathAnimator: ValueAnimator? = null

    // ===== Paint =====
    private val capsulePaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val capsuleBorderPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#555580")
        style = Paint.Style.STROKE
        strokeWidth = 1.5f.dpToPx()
    }
    private val glowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#6060ff")
        maskFilter = BlurMaskFilter(25f.dpToPx(), BlurMaskFilter.Blur.NORMAL)
    }
    private val barPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        style = Paint.Style.FILL
    }
    private val highlightPaint = Paint(Paint.ANTI_ALIAS_FLAG)

    // ===== 尺寸 =====
    private var viewWidth = 0
    private var viewHeight = 0

    init {
        visibility = INVISIBLE
    }

    // ==================== 公开 API ====================

    fun setState(state: State) {
        if (currentState == state) return
        if (isAttachedToWindow) {
            post { setStateInternal(state) }
        } else {
            setStateInternal(state)
        }
    }

    /** 设置实时音量 0~1，SPEAKING 状态下持续调用 */
    fun setAudioLevel(level: Float) {
        audioLevel = level.coerceIn(0f, 1f)
    }

    private fun setStateInternal(state: State) {
        if (currentState == state) return
        val oldState = currentState
        currentState = state
        Log.d("ListeningOrb", "setState: $oldState -> $state")

        // ★ 非 IDLE 状态强制可见（防止第二次唤醒后不显示）
        if (state != State.IDLE) {
            visibility = VISIBLE
        }

        when (state) {
            State.IDLE -> animateToIdle()
            State.LISTENING -> {
                if (oldState == State.IDLE) animateWakeup() else animateToListening()
            }
            State.SPEAKING -> animateToSpeaking()
            State.THINKING -> animateToThinking()
        }
    }

    // ==================== 动画 ====================

    private fun animateWakeup() {
        visibility = VISIBLE
        cancelAnimators()
        ballScale = 0f
        ballAlpha = 0f
        capsuleAlpha = 0f
        capsuleHeight = 0f
        smoothAudioLevel = 0f
        wavePhase = 0f

        mainAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = ANIM_WAKEUP_DURATION
            interpolator = AccelerateDecelerateInterpolator()
            addUpdateListener {
                val p = it.animatedValue as Float
                when {
                    p < 0.35f -> {
                        // 圆球出现
                        val t = p / 0.35f
                        ballScale = t
                        ballAlpha = t
                        capsuleAlpha = 0f
                    }
                    p < 0.55f -> {
                        // 圆球→胶囊过渡
                        val t = (p - 0.35f) / 0.2f
                        ballScale = 1f - t * 0.3f
                        ballAlpha = 1f - t
                        capsuleAlpha = t
                        capsuleWidth = getCapsuleWidthPx() * (0.5f + 0.5f * smoothStep(t))
                        capsuleHeight = CAPSULE_MIN_HEIGHT.dpToPx() * smoothStep(t)
                    }
                    else -> {
                        // 胶囊完全展开
                        val t = (p - 0.55f) / 0.45f
                        ballScale = 0f
                        ballAlpha = 0f
                        capsuleAlpha = 1f
                        capsuleWidth = getCapsuleWidthPx()
                        capsuleHeight = CAPSULE_MIN_HEIGHT.dpToPx()
                        wavePhase = 0f
                    }
                }
                invalidate()
            }
            addListener(object : android.animation.AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: android.animation.Animator) {
                    if (currentState == State.LISTENING) {
                        startBreathAnimation()
                    }
                }
            })
            start()
        }
    }

    private fun animateToListening() {
        cancelAnimators()
        capsuleHeight = CAPSULE_MIN_HEIGHT.dpToPx()
        capsuleAlpha = 1f
        wavePhase = 0f
        startBreathAnimation()
    }

    private fun animateToSpeaking() {
        cancelAnimators()
        isThinking = false
        capsuleAlpha = 1f
        capsuleHeight = CAPSULE_MIN_HEIGHT.dpToPx()
        // ★ 持续刷新：wavePhase 递增实现平滑波浪流动
        mainAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 16  // ~60fps
            repeatCount = ValueAnimator.INFINITE
            addUpdateListener {
                wavePhase += WAVE_SPEED
                updateCapsuleHeight()
                invalidate()
            }
            start()
        }
    }

    private fun animateToThinking() {
        cancelAnimators()
        isThinking = true
        capsuleAlpha = 1f
        capsuleHeight = CAPSULE_MIN_HEIGHT.dpToPx()

        mainAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 16  // ~60fps
            repeatCount = ValueAnimator.INFINITE
            addUpdateListener {
                wavePhase += WAVE_SPEED * 0.6f  // 思考时波浪慢一点
                thinkPhase = (thinkPhase + 0.008f) % 1f
                invalidate()
            }
            start()
        }
    }

    private fun animateToIdle() {
        cancelAnimators()
        isThinking = false
        val startAlpha = capsuleAlpha
        val startH = capsuleHeight

        mainAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = ANIM_COLLAPSE_DURATION
            interpolator = AccelerateDecelerateInterpolator()
            addUpdateListener {
                val p = it.animatedValue as Float
                val ease = 1f - smoothStep(p)
                capsuleAlpha = startAlpha * ease
                capsuleHeight = startH * ease
                invalidate()
            }
            addListener(object : android.animation.AnimatorListenerAdapter() {
                override fun onAnimationEnd(animation: android.animation.Animator) {
                    // ★ 只有当前状态还是 IDLE 才隐藏（防止动画被取消后误隐藏）
                    if (currentState == State.IDLE) {
                        visibility = INVISIBLE
                    }
                }
            })
            start()
        }
    }

    /** 呼吸脉冲：波形条轻微起伏，模拟"我在听" */
    private fun startBreathAnimation() {
        breathAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 16  // ~60fps
            repeatCount = ValueAnimator.INFINITE
            addUpdateListener {
                // ★ 实时读取音量，更新 smoothAudioLevel（让波浪跟声音变化）
                smoothAudioLevel += (audioLevel - smoothAudioLevel) * if (audioLevel > smoothAudioLevel) 0.35f else 0.12f
                wavePhase += WAVE_SPEED * (0.3f + smoothAudioLevel * 2.0f)  // 有声音时波浪加快
                breathPhase = (breathPhase + 0.005f) % 1f
                invalidate()
            }
            start()
        }
    }

    /** 根据音量更新平滑音量（胶囊大小不变） */
    private fun updateCapsuleHeight() {
        smoothAudioLevel += (audioLevel - smoothAudioLevel) * if (audioLevel > smoothAudioLevel) 0.35f else 0.12f
        // ★ 胶囊大小固定不变，只有波浪振幅跟随音量
    }

    private fun cancelAnimators() {
        mainAnimator?.cancel()
        breathAnimator?.cancel()
        mainAnimator = null
        breathAnimator = null
    }

    // ==================== 绘制 ====================

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        viewWidth = w
        viewHeight = h
        capsuleWidth = getCapsuleWidthPx()
    }

    private fun getCapsuleWidthPx(): Float = viewWidth * CAPSULE_WIDTH_RATIO

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        if (currentState == State.IDLE) return

        val cx = viewWidth / 2f
        val cy = viewHeight / 2f

        canvas.save()

        // ===== 1. 圆球（唤醒过渡）=====
        if (ballAlpha > 0.01f) {
            val r = BALL_SIZE.dpToPx() / 2f * ballScale
            glowPaint.alpha = (ballAlpha * 80).toInt()
            canvas.drawCircle(cx, cy, r + 12f.dpToPx(), glowPaint)

            capsulePaint.shader = RadialGradient(cx, cy, r,
                intArrayOf(Color.parseColor("#3a3a6a"), Color.parseColor("#1a1a30")),
                null, Shader.TileMode.CLAMP)
            capsulePaint.alpha = (ballAlpha * 255).toInt()
            canvas.drawCircle(cx, cy, r, capsulePaint)

            // 高光
            highlightPaint.color = Color.WHITE
            highlightPaint.alpha = (ballAlpha * 40).toInt()
            canvas.drawCircle(cx - r * 0.2f, cy - r * 0.2f, r * 0.3f, highlightPaint)
        }

        // ===== 2. 胶囊 =====
        if (capsuleAlpha > 0.01f) {
            val capW = capsuleWidth
            val capH = capsuleHeight.coerceAtLeast(1f)
            val capR = CAPSULE_RADIUS.dpToPx().coerceAtMost(capH / 2f)
            val left = cx - capW / 2f
            val top = cy - capH / 2f

            // 外发光
            glowPaint.alpha = (capsuleAlpha * 90).toInt()
            canvas.drawRoundRect(
                RectF(left - 10f.dpToPx(), top - 10f.dpToPx(),
                    left + capW + 10f.dpToPx(), top + capH + 10f.dpToPx()),
                capR + 6f.dpToPx(), capR + 6f.dpToPx(), glowPaint)

            // 胶囊主体（半透明玻璃效果）
            capsulePaint.shader = RadialGradient(cx, cy, capW / 2f,
                intArrayOf(Color.parseColor("#3a3a5e"), Color.parseColor("#252540"), Color.parseColor("#1a1a30")),
                null, Shader.TileMode.CLAMP)
            capsulePaint.alpha = (capsuleAlpha * 180).toInt()  // ★ 降低透明度，玻璃感
            canvas.drawRoundRect(RectF(left, top, left + capW, top + capH), capR, capR, capsulePaint)

            // 边框
            capsuleBorderPaint.alpha = (capsuleAlpha * 120).toInt()
            canvas.drawRoundRect(RectF(left, top, left + capW, top + capH), capR, capR, capsuleBorderPaint)

            // 顶部高光
            highlightPaint.shader = LinearGradient(left + capR, top, left + capW - capR, top,
                intArrayOf(Color.TRANSPARENT, Color.argb(50, 255, 255, 255), Color.TRANSPARENT),
                null as FloatArray?, Shader.TileMode.CLAMP)
            highlightPaint.alpha = (capsuleAlpha * 180).toInt()
            canvas.drawRoundRect(RectF(left + capR, top + 1f.dpToPx(), left + capW - capR, top + 3f.dpToPx()),
                1.5f.dpToPx(), 1.5f.dpToPx(), highlightPaint)

            // ===== 3. 连续波浪线（一根线，平滑流动）=====
            val wavePadding = 12f.dpToPx()  // 波浪线左右留白
            val waveStartX = left + wavePadding
            val waveEndX = left + capW - wavePadding
            val waveWidth = waveEndX - waveStartX

            // 波浪振幅：静止时很小，说话时跟随音量实时变化
            val baseAmplitude = 1.5f.dpToPx()  // 静止时微小振幅
            val maxAmplitude = (capH - 10f.dpToPx()) / 2f  // 最大不超过胶囊高度
            val amplitude = when {
                currentState == State.SPEAKING -> baseAmplitude + smoothAudioLevel * (maxAmplitude - baseAmplitude)
                isThinking -> baseAmplitude + 3f.dpToPx()
                else -> baseAmplitude + smoothAudioLevel * (maxAmplitude - baseAmplitude) * 0.8f  // ★ LISTENING也跟音量走
            }

            // 构建 Path（正弦曲线）
            val wavePath = Path()
            for (i in 0..WAVE_POINTS) {
                val t = i.toFloat() / WAVE_POINTS  // 0~1
                val x = waveStartX + t * waveWidth
                // 正弦波 + 边缘衰减（两端振幅趋近0，中间最大）
                val envelope = sin(t * Math.PI).toFloat()  // 0→1→0
                val y = cy + sin(wavePhase * Math.PI * 2 * WAVE_FREQUENCY + t * Math.PI * 2 * WAVE_FREQUENCY).toFloat() * amplitude * envelope
                if (i == 0) wavePath.moveTo(x, y) else wavePath.lineTo(x, y)
            }

            // 渐变色（紫→蓝→粉）
            val gradient = LinearGradient(waveStartX, cy, waveEndX, cy,
                intArrayOf(
                    Color.parseColor("#b080ff"),  // 紫
                    Color.parseColor("#80a0ff"),  // 蓝
                    Color.parseColor("#ff80c0"),  // 粉
                ), null, Shader.TileMode.CLAMP)
            barPaint.shader = gradient
            barPaint.style = Paint.Style.STROKE
            barPaint.strokeWidth = WAVE_STROKE_WIDTH.dpToPx()
            barPaint.strokeCap = Paint.Cap.ROUND
            barPaint.strokeJoin = Paint.Join.ROUND
            barPaint.alpha = (capsuleAlpha * 230).toInt()
            canvas.drawPath(wavePath, barPaint)
            barPaint.shader = null
            barPaint.style = Paint.Style.FILL
        }

        canvas.restore()
    }

    // ==================== 工具 ====================

    private fun smoothStep(t: Float): Float = t * t * (3f - 2f * t)

    private fun Float.dpToPx(): Float = this * resources.displayMetrics.density
    private fun Int.dpToPx(): Float = this * resources.displayMetrics.density

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        cancelAnimators()
    }
}
