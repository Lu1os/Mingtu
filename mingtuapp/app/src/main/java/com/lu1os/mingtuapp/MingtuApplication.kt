package com.lu1os.mingtuapp

import android.app.Application
import android.util.Log
import com.iflytek.aikit.core.AiHelper
import com.iflytek.aikit.core.BaseLibrary
import com.iflytek.aikit.core.CoreListener
import com.iflytek.aikit.core.ErrType
import com.iflytek.aikit.core.LogLvl
import java.io.File
import java.io.FileOutputStream

/**
 * 应用程序入口类
 * 负责初始化讯飞 AIKit XTTS（离线语音合成）
 *
 * 语音唤醒已改用 sherpa-onnx（开源免费），不再需要讯飞 MSC SpeechUtility
 *
 * !!! 重要提醒 !!!
 * 首次运行必须【联网激活】，否则会报 18405/18708 错误
 * 激活成功后，后续可离线使用
 */
class MingtuApplication : Application() {

    companion object {
        private const val TAG = "MingtuApplication"
        private const val APP_ID = ""
        private const val API_KEY = ""
        private const val API_SECRET = ""

        // assets 中资源目录
        private const val ASSETS_XTTS_DIR = "iflytek/xtts"

        @Volatile
        var isAuthSuccess = false
            private set

        @Volatile
        var authErrorCode = -1
            private set
    }

    private val coreListener = CoreListener { type, code ->
        Log.i(TAG, "========== CoreListener 回调 ==========")
        Log.i(TAG, "type: $type, code: $code")

        when (type) {
            ErrType.AUTH -> {
                authErrorCode = code
                isAuthSuccess = code == 0

                if (code == 0) {
                    Log.d(TAG, "========================================")
                    Log.d(TAG, "讯飞离线语音合成 SDK 授权成功！")
                    Log.d(TAG, "========================================")
                } else {
                    Log.e(TAG, "========================================")
                    Log.e(TAG, "讯飞离线语音合成 SDK 授权失败！")
                    Log.e(TAG, "========================================")
                    Log.e(TAG, "授权错误码: $code")

                    when (code) {
                        18405 -> {
                            Log.e(TAG, "【错误原因】18405: SDK授权失败")
                            Log.e(TAG, "【解决方案】")
                            Log.e(TAG, "1. 请确保设备已联网（首次使用需要联网激活）")
                            Log.e(TAG, "2. 检查 APP_ID ($APP_ID) 是否正确")
                            Log.e(TAG, "3. 检查 API_KEY 和 API_SECRET 是否匹配")
                            Log.e(TAG, "4. 卸载App重新安装，联网后再次尝试")
                        }
                        18708 -> {
                            Log.e(TAG, "【错误原因】18708: 离线能力未激活")
                            Log.e(TAG, "【解决方案】首次使用需联网激活，请确保网络通畅后重启App")
                        }
                        else -> {
                            Log.e(TAG, "【错误原因】未知授权错误码: $code")
                            Log.e(TAG, "【解决方案】请检查网络连接和SDK配置")
                        }
                    }
                }
            }
            ErrType.HTTP -> {
                Log.w(TAG, "HTTP认证结果: $code")
            }
            else -> {
                Log.w(TAG, "其他错误类型: $type, 错误码: $code")
            }
        }

        Log.i(TAG, "========== CoreListener 结束 ==========")
    }

    override fun onCreate() {
        super.onCreate()

        val workDir = getExternalFilesDir(null)?.absolutePath ?: filesDir.absolutePath

        Log.d(TAG, "========================================")
        Log.d(TAG, "应用程序启动，初始化讯飞 AIKit XTTS")
        Log.d(TAG, "========================================")
        Log.d(TAG, "APP_ID: $APP_ID")
        Log.d(TAG, "workDir: $workDir")

        try {
            // ========== 第一步：复制 XTTS 资源文件到 workDir ==========
            copyAssetsToWorkDir(ASSETS_XTTS_DIR, File(workDir, ASSETS_XTTS_DIR))

            // ========== 第二步：初始化 AIKit XTTS ==========
            Log.d(TAG, "---------- 初始化 AIKit XTTS ----------")

            // 设置日志级别
            AiHelper.getInst().setLogInfo(LogLvl.VERBOSE, 1, "$workDir/aikit/aeeLog.txt")

            // 构建初始化参数
            val params = BaseLibrary.Params.builder()
                .appId(APP_ID)
                .apiKey(API_KEY)
                .apiSecret(API_SECRET)
                .workDir(workDir)
                .build()

            Log.d(TAG, "AIKit 初始化参数构建完成")

            // 注册SDK状态监听器
            AiHelper.getInst().registerListener(coreListener)
            Log.d(TAG, "CoreListener 注册完成")

            // 在后台线程初始化SDK
            Thread {
                try {
                    Log.d(TAG, "开始调用 AiHelper.initEntry()...")
                    AiHelper.getInst().initEntry(applicationContext, params)
                    Log.d(TAG, "AiHelper.initEntry() 调用完成")
                    Log.d(TAG, "请等待 CoreListener 回调确认授权状态")
                } catch (e: Exception) {
                    Log.e(TAG, "AiHelper.initEntry() 异常: ${e.message}")
                    e.printStackTrace()
                }
            }.start()

        } catch (e: Exception) {
            Log.e(TAG, "SDK 初始化异常: ${e.message}")
            e.printStackTrace()
        }
    }

    /**
     * 递归复制 assets 目录到 workDir
     */
    private fun copyAssetsToWorkDir(assetsPath: String, destDir: File) {
        Log.d(TAG, "---------- 复制资源文件到 workDir ----------")
        Log.d(TAG, "源: assets/$assetsPath")
        Log.d(TAG, "目标: ${destDir.absolutePath}")

        try {
            val files = assets.list(assetsPath)
            if (files.isNullOrEmpty()) {
                Log.w(TAG, "assets/$assetsPath 目录为空或不存在")
                return
            }

            if (!destDir.exists()) {
                val created = destDir.mkdirs()
                Log.d(TAG, "创建目标目录: ${destDir.absolutePath} ($created)")
            }

            var copiedCount = 0
            var skippedCount = 0

            for (fileName in files) {
                val assetFilePath = "$assetsPath/$fileName"
                val destFile = File(destDir, fileName)

                // 递归处理子目录
                if (isAssetDirectory(assetFilePath)) {
                    copyAssetsToWorkDir(assetFilePath, destFile)
                    continue
                }

                // 检查文件是否已存在且大小一致，避免重复复制
                if (destFile.exists()) {
                    val assetSize = getAssetFileSize(assetFilePath)
                    if (assetSize > 0 && destFile.length() == assetSize) {
                        skippedCount++
                        Log.d(TAG, "跳过已存在文件: $fileName (${destFile.length()} 字节)")
                        continue
                    }
                    Log.d(TAG, "文件已存在但大小不匹配，重新复制: $fileName (assets=$assetSize, disk=${destFile.length()})")
                }

                // 复制文件
                try {
                    assets.open(assetFilePath).use { input ->
                        FileOutputStream(destFile).use { output ->
                            input.copyTo(output)
                        }
                    }
                    copiedCount++
                    Log.d(TAG, "复制完成: $fileName (${destFile.length()} 字节)")
                } catch (e: Exception) {
                    Log.e(TAG, "复制文件失败: $fileName - ${e.message}")
                }
            }

            Log.d(TAG, "---------- 资源复制完成: 新复制 $copiedCount 个, 跳过 $skippedCount 个 ----------")

        } catch (e: Exception) {
            Log.e(TAG, "复制资源文件异常: ${e.message}")
            e.printStackTrace()
        }
    }

    private fun isAssetDirectory(assetsPath: String): Boolean {
        return try {
            val files = assets.list(assetsPath)
            !files.isNullOrEmpty()
        } catch (e: Exception) {
            false
        }
    }

    private fun getAssetFileSize(assetsPath: String): Long {
        return try {
            assets.open(assetsPath).use { it.available().toLong() }
        } catch (e: Exception) {
            -1L
        }
    }
}
