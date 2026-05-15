package com.lloupp.polymarketcontrol

import android.os.Bundle
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Button
import android.widget.EditText
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : AppCompatActivity() {
    private lateinit var webView: WebView
    private lateinit var etBaseUrl: EditText

    // Se configurar CONTROL_TOKEN no servidor, preencha aqui:
    private val token = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        webView = findViewById(R.id.webView)
        etBaseUrl = findViewById(R.id.etBaseUrl)
        val prefs = getSharedPreferences("polymarket_control", MODE_PRIVATE)
        val defaultUrl = "http://192.168.0.10:8090"
        etBaseUrl.setText(prefs.getString("baseUrl", defaultUrl) ?: defaultUrl)

        webView.settings.javaScriptEnabled = true
        webView.webViewClient = WebViewClient()
        webView.loadUrl(currentBaseUrl())

        findViewById<Button>(R.id.btnStart).setOnClickListener { callControl("start") }
        findViewById<Button>(R.id.btnStop).setOnClickListener { callControl("stop") }
        findViewById<Button>(R.id.btnStatus).setOnClickListener { callControl("status") }
    }

    private fun callControl(action: String) {
        thread {
            try {
                val baseUrl = currentBaseUrl()
                getSharedPreferences("polymarket_control", MODE_PRIVATE)
                    .edit()
                    .putString("baseUrl", baseUrl)
                    .apply()

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
                    if (action == "status") webView.loadUrl(baseUrl)
                }
            } catch (e: Exception) {
                runOnUiThread {
                    Toast.makeText(this, "Erro: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    private fun currentBaseUrl(): String {
        val raw = etBaseUrl.text?.toString()?.trim().orEmpty().ifBlank { "http://192.168.0.10:8090" }
        return raw.removeSuffix("/")
    }
}
