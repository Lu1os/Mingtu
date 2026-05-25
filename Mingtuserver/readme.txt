============================================
文件夹名称：MingtuApp（项目根目录）
============================================
本文件夹为安卓应用"明途无障碍助手"的完整工程代码。
主要功能：手机上传（GPS、指南针朝向、录音数据、摄像头画面）至服务端处理，
并结合离线语音合成与唤醒词技术，为视障用户提供出行辅助。

============================================
子文件夹及重要文件说明
============================================

1. 📁 app/
   应用主模块，包含源代码、资源、依赖库及配置文件。
   - build.gradle.kts          ：模块级 Gradle 构建脚本（SDK版本、依赖、签名等）
   - proguard-rules.pro        ：代码混淆规则（Release 打包时启用，保留反射/JNI/序列化）
   - libs/                     ：存放第三方本地库（.aar / .jar）
        * AIKit_XTTS.aar       ：讯飞星火语音合成库
        * AMap_Location_*.jar  ：高德定位 SDK
        * Codec.aar            ：编解码库
        * Msc.jar              ：讯飞语音核心库
        * SparkChain.aar       ：星火大模型链库
   - src/                      ：源码与资源目录（详见下方）

2. 📁 app/src/main/
   主源码与资源
   - AndroidManifest.xml       ：应用清单（权限、组件注册、包名等）
   - assets/                   ：原始资源文件（语音唤醒词、TTS 模型等）
        * iflytek/             ：讯飞语音唤醒 & 离线 TTS 模型文件
        * sherpa-kws/          ：Sherpa-ONNX 唤醒词模型（keywords.txt 等）
   - java/com/lu1os/mingtuapp/：Kotlin 源码包
        * MingtuApplication.kt ：Application 入口，全局初始化
        * MainActivity.kt      ：主界面，语音交互与导航控制
        * SettingsActivity.kt  ：设置界面，参数配置
        * CameraStreamManager.kt ：摄像头推流管理（WebSocket 发送至视觉AI）
        * SpeechManager.kt     ：语音识别与合成管理（讯飞 SDK）
        * SpeechTranscriber.kt ：实时语音转写
        * TtsManager.kt        ：文本转语音（离线/在线）
        * AmapLocationHelper.kt：高德定位封装
        * KeyHandler.kt        ：物理按键事件处理
        * AppConfig.kt         ：全局配置（服务端 IP、端口等）
        * ui/                  ：界面组件（页面、主题）
        * utils/               ：工具类
        * view/ListeningOrbView.kt ：语音交互动画
   - java/com/k2fsa/sherpa/onnx/：Sherpa-ONNX 唤醒词引擎（开源集成）
        * KeywordSpotter.kt    ：唤醒词检测
        * OnlineRecognizer.kt  ：在线识别器
        * FeatureConfig.kt     ：特征配置
        * OnlineStream.kt      ：音频流处理
   - jniLibs/                  ：原生库（arm64-v8a / armeabi-v7a）
        * libapssdk.so         ：高德定位 SDK 原生库
        * libonnxruntime.so    ：ONNX Runtime（AI推理）
        * libsherpa-onnx-*.so  ：Sherpa-ONNX 唤醒词引擎
   - res/                      ：Android 资源文件
        * drawable/            ：图标、形状、背景等 XML
        * layout/              ：界面布局（activity_main.xml 等）
        * mipmap-*/            ：应用图标（多分辨率）
        * values/              ：颜色、字符串、主题
        * xml/                 ：备份规则、数据提取规则

3. 📁 app/src/test/
   单元测试（ExampleUnitTest.kt）

4. 📁 app/src/androidTest/
   仪器化测试（ExampleInstrumentedTest.kt）

5. 📁 gradle/wrapper/
   Gradle Wrapper 配置（无需预装 Gradle）
   - gradle-wrapper.properties ：指定 Gradle 版本与下载地址
   - gradle-wrapper.jar       ：Wrapper 可执行文件

6. 📄 根目录配置文件
   - build.gradle.kts          ：项目级构建脚本（AGP 版本声明）
   - settings.gradle.kts       ：项目名、模块、仓库配置
   - gradle.properties         ：Gradle 参数（JVM、编码等）
   - gradlew / gradlew.bat     ：Gradle Wrapper 启动脚本（Linux / Windows）
   - local.properties          ：本地 SDK 路径（需自行创建，不提交 Git）
   - readme.txt                ：本文件

============================================
编译与运行
============================================
1. 安装 Android Studio（最新稳定版）
2. 用 Android Studio 打开本项目根目录
3. 等待 Gradle Sync 完成（首次需下载依赖，约5-10分钟）
4. 修改 `app/src/main/java/com/lu1os/mingtuapp/AppConfig.kt` 中的 `SERVER_HOST` 常量，
   改为运行服务端电脑的局域网 IP（例如 `"192.168.1.100"`）
5. 连接 Android 设备（开启 USB 调试）或启动模拟器
6. 点击 Run（绿色三角形）运行

命令行编译（可选）：
   Windows: gradlew.bat assembleDebug
   Linux/Mac: ./gradlew assembleDebug
   产物位置：app/build/outputs/apk/debug/

============================================
服务端连接端口说明
============================================
App 需要连接以下服务端端口：

| 服务        | 端口  | 用途                    |
|-------------|-------|-------------------------|
| 小助手 AI   | 8766  | 主连接（语音、导航）     |
| 视觉 AI     | 8765  | 摄像头推流（障碍物检测） |

说明：
- 端口 8768 是视觉 AI 与小助手 AI 的内部通信端口
- 端口 8767 是 GPS AI 与小助手 AI 的内部通信端口
- 端口 5000 是 GPS AI 的 REST API（由小助手 AI 调用，App 不直连）

============================================
技术栈
============================================
- 语言：Kotlin
- 最低支持：Android 7.0（minSdk=24）
- 目标版本：Android 15（targetSdk=36）
- 构建工具：Gradle + AGP 9.0.0-alpha06
- 相机：CameraX 1.3.4
- 定位：高德定位 SDK V11.1.001
- 语音合成：讯飞 XTTS（离线）
- 语音唤醒：Sherpa-ONNX v1.12.36
- 网络通信：OkHttp 4.12.0 + Java-WebSocket 1.5.3
- AI对话：科大讯飞星火大模型（SparkChain）

============================================
注意事项
============================================
- 所有本地依赖库（讯飞、高德、Sherpa）均为离线 SDK，无额外下载
- 远程依赖（CameraX、OkHttp 等）由 Gradle 自动处理
- 如 Gradle Sync 失败，请在 gradle.properties 中添加：
  `android.overridePathCheck=true`
- 本项目需配合服务端（明途-服务端源码）一起使用
