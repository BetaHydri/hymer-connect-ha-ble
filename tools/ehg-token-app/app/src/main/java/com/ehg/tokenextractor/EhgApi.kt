package com.ehg.tokenextractor

import android.util.Base64
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder

/**
 * EHG Cloud API client — OAuth2 ROPC login and confirmation token for BLE pairing.
 * Matches the HA integration's api.py exactly.
 */
class EhgApi(private val brand: String) {

    companion object {
        private const val BASE_URL = "https://smartrv.erwinhymergroup.com"
        private const val ENDPOINT_AUTH = "/api/v2/oauth/token"
        private const val ENDPOINT_VEHICLES_BY_TOKEN = "/api/ehg/v1/vehicles/byToken"
        private const val ENDPOINT_CONFIRMATION_TOKEN = "/api/ehg/v1/accounts/confirmationToken"
        // OAuth2 Basic auth header (client_id:client_secret base64-encoded)
        private const val OAUTH2_BASIC_AUTH = "Basic ZWhnLXByb2QtbW9iaWxlLWFwcC10ZWNobmljYWwtdXNlcjpaez96Ois3bVFhNXZAb2VlNV0lZEVeUSpxeDh9WXIoYWw1eFNUaC05LERdYm48OzhWbzh1PGclc8OcLShOMyV5"
        private const val APP_VERSION = "2.10.14"
        private const val USER_AGENT = "EHGConnect/2.10.14 (Android)"
    }

    private var accessToken: String? = null

    suspend fun login(email: String, password: String): Boolean = withContext(Dispatchers.IO) {
        val url = URL("$BASE_URL$ENDPOINT_AUTH")
        val conn = url.openConnection() as HttpURLConnection
        conn.requestMethod = "POST"
        conn.setRequestProperty("Content-Type", "application/x-www-form-urlencoded")
        conn.setRequestProperty("Accept", "application/json, text/plain, */*")
        conn.setRequestProperty("Authorization", OAUTH2_BASIC_AUTH)
        conn.setRequestProperty("User-Agent", USER_AGENT)
        conn.setRequestProperty("X-EHG-Brand", "${brand.replaceFirstChar { it.uppercase() }}/$APP_VERSION")
        conn.doOutput = true

        val body = "grant_type=password" +
            "&username=${URLEncoder.encode(email, "UTF-8")}" +
            "&password=${URLEncoder.encode(password, "UTF-8")}"

        OutputStreamWriter(conn.outputStream).use { it.write(body) }

        if (conn.responseCode == 200) {
            val response = conn.inputStream.bufferedReader().readText()
            val json = JSONObject(response)
            accessToken = json.optString("access_token", null)
            accessToken != null
        } else {
            false
        }
    }

    var lastError: String? = null
        private set

    suspend fun getConfirmationToken(activationToken: String): String? = withContext(Dispatchers.IO) {
        val token = accessToken ?: run {
            lastError = "No access token"
            return@withContext null
        }

        // First validate the activation token against the vehicle
        val byTokenUrl = URL("$BASE_URL$ENDPOINT_VEHICLES_BY_TOKEN?token=${URLEncoder.encode(activationToken, "UTF-8")}")
        val byTokenConn = byTokenUrl.openConnection() as HttpURLConnection
        byTokenConn.setRequestProperty("Authorization", "Bearer $token")
        byTokenConn.setRequestProperty("User-Agent", USER_AGENT)

        if (byTokenConn.responseCode != 200) {
            val errorBody = try { byTokenConn.errorStream?.bufferedReader()?.readText()?.take(200) } catch (_: Exception) { null }
            lastError = "vehicles/byToken returned ${byTokenConn.responseCode}: $errorBody"
            return@withContext null
        }

        // Now get the confirmation token
        val confirmUrl = URL("$BASE_URL$ENDPOINT_CONFIRMATION_TOKEN")
        val confirmConn = confirmUrl.openConnection() as HttpURLConnection
        confirmConn.setRequestProperty("Authorization", "Bearer $token")
        confirmConn.setRequestProperty("User-Agent", USER_AGENT)

        if (confirmConn.responseCode == 200) {
            val response = confirmConn.inputStream.bufferedReader().readText()
            val json = JSONObject(response)
            json.optString("confirmationToken", null)
                ?: json.optString("confirmation_token", null)
                ?: json.optString("token", null)
        } else {
            val errorBody = try { confirmConn.errorStream?.bufferedReader()?.readText()?.take(200) } catch (_: Exception) { null }
            lastError = "confirmationToken returned ${confirmConn.responseCode}: $errorBody"
            null
        }
    }
}
