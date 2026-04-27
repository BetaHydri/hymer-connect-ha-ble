package com.ehg.tokenextractor

import java.nio.ByteBuffer
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.*

/**
 * TLS 1.0/1.1 handshake over BLE NUS using SSLEngine.
 * The SCU only supports legacy TLS with AES128-SHA / AES256-SHA.
 * SSLEngine provides the same MemoryBIO-style interface as Python's ssl module.
 */
class TlsOverBle {

    private lateinit var engine: SSLEngine
    private val appBuffer = ByteBuffer.allocate(16384)
    private val netBuffer = ByteBuffer.allocate(16384)
    private val peerAppBuffer = ByteBuffer.allocate(16384)
    private val peerNetBuffer = ByteBuffer.allocate(16384)

    val isHandshakeComplete: Boolean
        get() = engine.handshakeStatus == SSLEngineResult.HandshakeStatus.NOT_HANDSHAKING ||
                engine.handshakeStatus == SSLEngineResult.HandshakeStatus.FINISHED

    /**
     * Initialize the TLS engine and return the ClientHello bytes.
     */
    fun beginHandshake(): ByteArray {
        // Trust all certs (SCU uses self-signed)
        val trustAllCerts = arrayOf<TrustManager>(object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        })

        // Try TLSv1.1 first, fall back to TLSv1
        val ctx = try {
            SSLContext.getInstance("TLSv1.1")
        } catch (_: Exception) {
            SSLContext.getInstance("TLSv1")
        }
        ctx.init(null, trustAllCerts, SecureRandom())

        engine = ctx.createSSLEngine()
        engine.useClientMode = true

        // Enable legacy ciphers
        val ciphers = engine.supportedCipherSuites.filter {
            it.contains("AES_128_CBC_SHA") || it.contains("AES_256_CBC_SHA")
        }.toTypedArray()
        if (ciphers.isNotEmpty()) {
            engine.enabledCipherSuites = ciphers
        }

        engine.beginHandshake()

        // Generate ClientHello
        return wrapOutbound(ByteArray(0))
    }

    /**
     * Feed encrypted data from SCU, return outbound TLS data to send back.
     * Also returns any decrypted application data.
     */
    fun feedEncrypted(incoming: ByteArray): Pair<ByteArray, ByteArray> {
        peerNetBuffer.clear()
        peerNetBuffer.put(incoming)
        peerNetBuffer.flip()

        var outbound = ByteArray(0)
        var plaintext = ByteArray(0)

        while (peerNetBuffer.hasRemaining()) {
            peerAppBuffer.clear()
            val result = engine.unwrap(peerNetBuffer, peerAppBuffer)
            peerAppBuffer.flip()

            if (peerAppBuffer.hasRemaining()) {
                val data = ByteArray(peerAppBuffer.remaining())
                peerAppBuffer.get(data)
                plaintext += data
            }

            when (result.handshakeStatus) {
                SSLEngineResult.HandshakeStatus.NEED_WRAP -> {
                    outbound += wrapOutbound(ByteArray(0))
                }
                SSLEngineResult.HandshakeStatus.NEED_TASK -> {
                    var task = engine.delegatedTask
                    while (task != null) {
                        task.run()
                        task = engine.delegatedTask
                    }
                }
                else -> {}
            }

            if (result.status == SSLEngineResult.Status.CLOSED ||
                result.status == SSLEngineResult.Status.BUFFER_UNDERFLOW) break
        }

        // Check if we still need to wrap after unwrap
        if (engine.handshakeStatus == SSLEngineResult.HandshakeStatus.NEED_WRAP) {
            outbound += wrapOutbound(ByteArray(0))
        }

        return Pair(outbound, plaintext)
    }

    /**
     * Encrypt application data for sending to SCU.
     */
    fun encrypt(plaintext: ByteArray): ByteArray {
        return wrapOutbound(plaintext)
    }

    /**
     * Decrypt incoming data from SCU.
     */
    fun decrypt(ciphertext: ByteArray): ByteArray {
        val (_, plain) = feedEncrypted(ciphertext)
        return plain
    }

    private fun wrapOutbound(data: ByteArray): ByteArray {
        appBuffer.clear()
        appBuffer.put(data)
        appBuffer.flip()

        netBuffer.clear()
        engine.wrap(appBuffer, netBuffer)
        netBuffer.flip()

        val result = ByteArray(netBuffer.remaining())
        netBuffer.get(result)
        return result
    }
}
