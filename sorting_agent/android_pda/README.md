# 祺航分拣 PDA App

Sunmi L2S-PRO 专用 WebView 壳应用，加载仓库服务器 H5 页面。

## 修改服务器地址

打开 `app/src/main/java/com/qihang/pda/MainActivity.kt`，修改第一行常量：

```kotlin
const val SERVER_URL = "http://10.39.1.65:5010/pda"
//                          ^^^^^^^^^^^^^ 仓库 PC IP
//                                        ^^^^ Flask 端口（与 config.json flask_port 一致）
```

## 编译 APK

### 方法一：Android Studio（推荐）
1. 打开 Android Studio → Open → 选择本目录 `android_pda/`
2. 等待 Gradle sync 完成
3. 菜单 Build → Build Bundle(s)/APK(s) → Build APK(s)
4. APK 路径：`app/build/outputs/apk/debug/app-debug.apk`

### 方法二：命令行
```bash
cd android_pda
./gradlew assembleRelease
# APK: app/build/outputs/apk/release/app-release-unsigned.apk
```

## 安装到 Sunmi L2S-PRO

```bash
# USB 连接后：
adb install app/build/outputs/apk/debug/app-debug.apk

# 或直接拷贝 APK 到设备用文件管理器安装
```

## 使用说明

1. 确保 PDA 连接仓库 Wi-Fi（能 ping 通 `10.39.1.65`）
2. 打开 App，自动进入楼层选择页
3. 点击当前楼层（1-4楼）
4. 对准货物扫码，内置扫码枪自动填入条码并回车提交
5. 全屏绿色 = 成功，全屏红色 = 失败/错误
6. 点击"换楼层"可重新选楼层

## 注意事项

- `usesCleartextTraffic="true"` 允许访问内网 http（无 HTTPS 证书）
- 屏幕常亮，适合长时间拣货使用
- 返回键在已到根页面时提示"再按一次退出"（防误触退出）
- WebView 缓存策略为 `LOAD_DEFAULT`，服务器更新后自动刷新
