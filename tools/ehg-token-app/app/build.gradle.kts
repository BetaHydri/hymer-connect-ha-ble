plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.ehg.tokenextractor"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.ehg.tokenextractor"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    // Nordic BLE library — handles MTU negotiation, write splitting, bonding
    implementation("no.nordicsemi.android:ble:2.9.0")
    // Google Code Scanner — camera-based QR scanning without camera permission
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
