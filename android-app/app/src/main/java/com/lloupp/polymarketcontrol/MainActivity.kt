package com.lloupp.polymarketcontrol

import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView

    // Troque para o IP do seu servidor na rede local (ex: http://192.168.0.15:8090)
    private val baseUrl = "http://127.0.0.1:8090"
    // Se configurar CONTROL_TOKEN no servidor, preencha aqui:
    private val token = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        webView.settings.javaScriptEnabled = true
        webView.webViewClient = WebViewClient()
        webView.loadUrl(baseUrl)

        findViewById<Button>(R.id.btnStart).setOnClickListener { callControl("start") }
        findViewById<Button>(R.id.btnStop).setOnClickListener { callControl("stop") }
        findViewById<Button>(R.id.btnStatus).setOnClickListener { callControl("status") }
    }

    private fun callControl(action: String) {
        thread {
            try {
                val tk = if (token.isNotBlank()) "?token=$token" else ""
                val url = URL("$baseUrl/api/control/$action$tk")
                val conn = (url.openConnection() as HttpURLConnection).apply {
                    requestMethod = "GET"
                    connectTimeout = 7000
                    readTimeout = 12000
                }
                val code = conn.responseCode
                runOnUiThread {
                    Toast.makeText(this, "$action -> HTTP $code", Toast.LENGTH_SHORT).show()
                    if (action == "status") webView.reload()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "Erro: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }
}
