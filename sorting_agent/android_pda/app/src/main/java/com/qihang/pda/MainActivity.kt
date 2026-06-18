package com.qihang.pda

import android.annotation.SuppressLint
import android.app.Activity
import android.os.Bundle
import android.view.KeyEvent
import android.view.WindowManager
import android.webkit.*
import android.widget.Toast

/**
 * 祺航分拣 PDA App
 *
 * 纯 WebView 壳，加载仓库服务器 http://10.39.1.65:5010/pda
 * Sunmi L2S-PRO 内置扫码枪以键盘模式输入，pda.html 监听 Enter 键触发扫码。
 *
 * 修改服务器 IP/端口：只需改 SERVER_URL 常量，重新打包即可。
 */
class MainActivity : Activity() {

    companion object {
        // ⚠️ 仓库 PC 的实际 IP + 端口，与 config.json flask_port 一致
        const val SERVER_URL = "http://192.168.70.158:5010/pda"
    }

    private lateinit var webView: WebView

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 全屏，保持屏幕常亮
        window.addFlags(
            WindowManager.LayoutParams.FLAG_FULLSCREEN or
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
        )

        webView = WebView(this)
        setContentView(webView)

        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_DEFAULT
            // 允许 http 混合内容（仓库内网无 HTTPS）
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?, request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
                // 连接失败时展示提示页
                view?.loadData(
                    """
                    <html><body style="background:#0f172a;color:#f1f5f9;
                          font-family:sans-serif;text-align:center;padding-top:40%;">
                    <h2>⚠️ 无法连接服务器</h2>
                    <p style="color:#94a3b8">$SERVER_URL</p>
                    <p style="color:#94a3b8">请确认 Wi-Fi 已连接仓库网络</p>
                    <br><a href="$SERVER_URL" style="color:#3b82f6">重试</a>
                    </body></html>
                    """.trimIndent(),
                    "text/html", "UTF-8"
                )
            }
        }

        webView.loadUrl(SERVER_URL)
    }

    // 返回键：WebView 内部后退（若已在首页则提示）
    override fun onKeyDown(keyCode: Int, event: KeyEvent?): Boolean {
        if (keyCode == KeyEvent.KEYCODE_BACK && webView.canGoBack()) {
            webView.goBack()
            return true
        }
        if (keyCode == KeyEvent.KEYCODE_BACK) {
            Toast.makeText(this, "再按一次退出", Toast.LENGTH_SHORT).show()
            return true
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
    }

    override fun onPause() {
        super.onPause()
        webView.onPause()
    }
}
