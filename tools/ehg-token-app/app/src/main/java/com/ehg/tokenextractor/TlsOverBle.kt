package com.ehg.tokenextractor

import org.bouncycastle.jce.provider.BouncyCastleProvider
import org.bouncycastle.jsse.provider.BouncyCastleJsseProvider
import java.nio.ByteBuffer
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.*

/**
 * TLS 1.0/1.1 handshake over BLE NUS using SSLEngine.
 *
 * The SCU only supports legacy TLS with TLS_RSA_WITH_AES_128/256_CBC_SHA. Modern
 * Android (API 29+, Conscrypt) removed those protocols and cipher suites, so the
 * platform SSLEngine can no longer talk to the SCU. We therefore drive the
 * handshake through BouncyCastle's pure-Java JSSE provider, which still speaks
 * legacy TLS on ANY Android version — the same reason the official EHG app works
 * even on Android 16 (it bundles node-forge, a JS TLS stack, instead of using the
 * OS). SSLEngine gives the same MemoryBIO-style interface as Python's ssl module.
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
     *
     * @param log optional diagnostics sink. The handshake runs on BouncyCastle's
     *   pure-Java JSSE provider so the legacy TLS 1.0/1.1 + AES-CBC-SHA the SCU
     *   requires works regardless of the Android version. We still surface the
     *   negotiated protocols/ciphers so any failure stays diagnosable instead of
     *   a silent "ERROR: null".
     */
    fun beginHandshake(log: (String) -> Unit = {}): ByteArray {
        // Some Android builds (e.g. Samsung's Android 13/14 on the S20 FE 5G) ship
        // a java.security config that lists "TLSv1, TLSv1.1" in
        // jdk.tls.disabledAlgorithms. BouncyCastle's JSSE intersects the ENABLED
        // protocols with that disabled-algorithms constraint, so even though
        // setEnabledProtocols(["TLSv1.1","TLSv1"]) succeeds, the handshake later
        // throws "IllegalStateException: No usable protocols enabled" from
        // ProvSSLContextSpi.getActiveProtocolVersions(). The SCU ONLY speaks legacy
        // TLS 1.0/1.1 + AES-CBC-SHA, and we already trust-all its self-signed cert,
        // so clear the constraint for this throwaway handshake.
        try {
            java.security.Security.setProperty("jdk.tls.disabledAlgorithms", "")
            log("  Cleared jdk.tls.disabledAlgorithms (allow legacy TLS 1.0/1.1)")
        } catch (e: Throwable) {
            log("  \u26a0\ufe0f Could not clear jdk.tls.disabledAlgorithms: " +
                "${e.javaClass.simpleName}: ${e.message ?: "(no message)"}")
        }

        // Trust all certs (SCU uses self-signed)
        val trustAllCerts = arrayOf<TrustManager>(object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
        })

        // Use BouncyCastle's software TLS stack — Android's platform TLS
        // (Conscrypt) dropped legacy TLS 1.0/1.1 + TLS_RSA_WITH_AES_*_CBC_SHA on
        // API 29+, but the SCU needs exactly those. BC speaks them on any Android
        // version, mirroring how the official EHG app bundles node-forge.
        val ctx = try {
            val bcJsse = BouncyCastleJsseProvider(BouncyCastleProvider())
            SSLContext.getInstance("TLS", bcJsse).also {
                log("  TLS provider: BouncyCastle JSSE (${bcJsse.name})")
            }
        } catch (e: Throwable) {
            log("  \u26a0\ufe0f BouncyCastle JSSE unavailable (${e.javaClass.simpleName}: " +
                "${e.message ?: "(no message)"}); falling back to platform TLS")
            try {
                SSLContext.getInstance("TLSv1.1")
            } catch (_: Exception) {
                SSLContext.getInstance("TLSv1")
            }
        }
        ctx.init(null, trustAllCerts, SecureRandom())

        engine = ctx.createSSLEngine()
        engine.useClientMode = true

        // Explicitly enable the legacy protocols the SCU requires.
        val wantProtocols = listOf("TLSv1.1", "TLSv1")
        val availProtocols = engine.supportedProtocols.toSet()
        val enableProtocols = wantProtocols.filter { it in availProtocols }
        log("  TLS supported protocols: ${engine.supportedProtocols.joinToString()}")
        if (enableProtocols.isNotEmpty()) {
            try {
                engine.enabledProtocols = enableProtocols.toTypedArray()
                log("  TLS enabled protocols: ${enableProtocols.joinToString()}")
            } catch (e: Exception) {
                log("  \u26a0\ufe0f Could not enable legacy TLS: ${e.javaClass.simpleName}: ${e.message}")
            }
        } else {
            log("  \u26a0\ufe0f Neither BouncyCastle nor the platform offers TLS 1.0/1.1 \u2014 SCU handshake will fail.")
        }

        // Enable legacy ciphers (AES-CBC-SHA).
        val ciphers = engine.supportedCipherSuites.filter {
            it.contains("AES_128_CBC_SHA") || it.contains("AES_256_CBC_SHA")
        }.toTypedArray()
        if (ciphers.isNotEmpty()) {
            engine.enabledCipherSuites = ciphers
            log("  TLS enabled ciphers: ${ciphers.joinToString()}")
        } else {
            log("  \u26a0\ufe0f No legacy AES-CBC-SHA ciphers on this device \u2014 SCU handshake will fail.")
        }

        engine.beginHandshake()

        // Generate ClientHello
        return wrapOutbound(ByteArray(0))
    }

    /**
     * Feed encrypted data from SCU, return outbound TLS data to send back.
     * Also returns any decrypted application data.
     *
     * IMPORTANT: peerNetBuffer is persistent across calls — it accumulates
     * partial TLS records that span multiple BLE notifications (common at
     * MTU=23 where a single TLS record is split across many 20-byte chunks).
     */
    fun feedEncrypted(incoming: ByteArray): Pair<ByteArray, ByteArray> {
        // Compact any consumed data and append new data (don't clear!)
        peerNetBuffer.compact()
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
