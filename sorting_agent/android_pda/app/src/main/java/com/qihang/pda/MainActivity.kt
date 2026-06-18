package com.qihang.pda

import android.annotation.SuppressLint
import android.app.Activity
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Bundle
import android.view.KeyEvent
import android.view.WindowManager
import android.webkit.*
import android.widget.Toast

/**
 * 祺航分拣 PDA App
 *
 * 纯 WebView 壳，加载仓库服务器 http://192.168.70.158:5010/pda
 *
 * 扫码枪接入方式（双保险）：
 *   1. 广播接收（优先）：监听 SUNMI 扫码广播，直接注入 JS，不依赖焦点
 *   2. 键盘 Enter（兜底）：pda.html 监听 keydown Enter，应对其他设备
 */
class MainActivity : Activity() {

    companion object {
        const val SERVER_URL = "http://192.168.70.158:5010/pda"

        // SUNMI 扫码枪广播
        private const val SUNMI_SCAN_ACTION  = "com.sunmi.scanner.ACTION_DATA_CODE_RECEIVED"
        private const val SUNMI_SCAN_DATA_KEY = "data"
    }

    private lateinit var webView: WebView

    // 接收 SUNMI 扫码广播，注入到网页
    private val scanReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            val barcode = intent?.getStringExtra(SUNMI_SCAN_DATA_KEY)?.trim() ?: return
            if (barcode.isEmpty()) return
            // 转义单引号，调用网页 JS 函数
            val escaped = barcode.replace("\\", "\\\\").replace("'", "\\'")
            webView.evaluateJavascript("if(window.onScannerInput)window.onScannerInput('$escaped');", null)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

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
            mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
        }

        webView.webViewClient = object : WebViewClient() {
            override fun onReceivedError(
                view: WebView?, request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
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

    override fun onResume() {
        super.onResume()
        webView.onResume()
        registerReceiver(scanReceiver, IntentFilter(SUNMI_SCAN_ACTION))
    }

    override fun onPause() {
        super.onPause()
        webView.onPause()
        unregisterReceiver(scanReceiver)
    }

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
}
