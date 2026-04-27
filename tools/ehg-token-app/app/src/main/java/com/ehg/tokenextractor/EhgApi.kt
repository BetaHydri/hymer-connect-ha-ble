package com.ehg.tokenextractor

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL

/**
 * EHG Cloud API client — login and get confirmation token for BLE pairing.
 */
class EhgApi(private val brand: String) {

    private val baseUrl = "https://$brand-app-api.2-2connect.2-2.cloud"
    private var accessToken: String? = null

    suspend fun login(email: String, password: String): Boolean = withContext(Dispatchers.IO) {
        val url = URL("$baseUrl/api/ehg/v1/auth/login")
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.setRequestProperty("Content-Type", "application/json")
        conn.doOutput = true

        val body = JSONObject().apply {
            put("email", email)
            put("password", password)
        }

        OutputStreamWriter(conn.outputStream).use { it.write(body.toString()) }

        if (conn.responseCode == 200) {
            val response = conn.inputStream.bufferedReader().readText()
            val json = JSONObject(response)
            accessToken = json.optString("accessToken", null)
                ?: json.optString("access_token", null)
            accessToken != null
        } else {
            false
        }
    }

    suspend fun getConfirmationToken(activationToken: String): String? = withContext(Dispatchers.IO) {
        val token = accessToken ?: return@withContext null
        val url = URL("$baseUrl/api/ehg/v1/vehicles/byToken?token=$activationToken")
        val conn = url.openConnection() as HttpURLConnection
        conn.setRequestProperty("Authorization", "Bearer $token")

        if (conn.responseCode == 200) {
            val response = conn.inputStream.bufferedReader().readText()
            val json = JSONObject(response)
            json.optString("confirmationToken", null)
                ?: json.optString("confirmation_token", null)
        } else {
            null
        }
    }
}
