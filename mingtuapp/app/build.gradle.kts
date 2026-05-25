plugins {
    id("com.android.application")
}

android {
    namespace = "com.lu1os.mingtuapp"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.lu1os.mingtuapp"
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            abiFilters.addAll(setOf("arm64-v8a", "armeabi-v7a"))
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    packaging {
        jniLibs {
            pickFirsts.addAll(listOf(
                "lib/arm64-v8a/libAIKIT.so",
                "lib/arm64-v8a/libspark.so",
                "lib/arm64-v8a/libef7d69542_v10260_aee.so",
                "lib/armeabi-v7a/libAIKIT.so",
                "lib/armeabi-v7a/libspark.so",
                "lib/armeabi-v7a/libef7d69542_v10260_aee.so"
            ))
        }
        resources {
            excludes.addAll(listOf(
                "META-INF/DEPENDENCIES",
                "META-INF/LICENSE",
                "META-INF/LICENSE.txt",
                "META-INF/NOTICE",
                "META-INF/NOTICE.txt"
            ))
        }
    }
}

dependencies {

    // 高德SDK
    implementation(files("libs/AMap_Location_V11.1.001_20260402.jar"))

    // ===== 讯飞 SDK =====
    implementation(files("libs/AIKit_XTTS.aar"))
    implementation(files("libs/Msc.jar"))

    // ===== sherpa-onnx 语音唤醒 =====
    // ★★★ 不再使用 AAR！★★★
    // .so 文件直接放在 app/src/main/jniLibs/ 目录下
    // Kotlin 源码直接放在 app/src/main/java/com/k2fsa/sherpa/onnx/ 目录下
    // 这样 .class 和 .so 一定是匹配的 v1.12.36 版本
    // implementation(files("libs/sherpa-onnx-1.12.36.aar"))  // ← 已删除

    // ===== 其他 AAR =====
    implementation(files("libs/Codec.aar"))
    implementation(files("libs/SparkChain.aar"))

    // ===== 网络通信 =====
    implementation("org.java-websocket:Java-WebSocket:1.5.3")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // ===== AndroidX =====
    implementation("androidx.core:core-ktx:1.10.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.6.1")
    implementation("androidx.activity:activity-ktx:1.8.0")
    implementation("androidx.appcompat:appcompat:1.6.1")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")

    // ===== CameraX =====
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")
    implementation("androidx.camera:camera-view:1.3.4")

    // ===== 测试 =====
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.1.5")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.5.1")
}

// 自动复制外部资源文件到 assets 目录
tasks.register<Copy>("copyExternalAssets") {
    from(file("${rootProject.projectDir}/external_assets/iflytek/xtts")) {
        into("iflytek/xtts")
    }
    into(file("${project.projectDir}/src/main/assets"))
}

tasks.named("preBuild") {
    dependsOn("copyExternalAssets")
}