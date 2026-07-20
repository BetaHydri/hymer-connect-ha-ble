package com.ehg.tokenextractor

import java.io.ByteArrayOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.zip.CRC32

/**
 * PIA protocol: frame encoding (magic + length + CRC32) and
 * minimal protobuf wire-format helpers for PairMobileRequest/Response.
 */
object PiaProtocol {

    private const val PIA_MAGIC: Short = 0xA0CB.toShort()
    private const val PIA_HEADER_SIZE = 10 // 2-byte magic + 4-byte length + 4-byte CRC32

    // --- Protobuf wire-format helpers ---

    fun encodeVarint(value: Long): ByteArray {
        val out = ByteArrayOutputStream()
        var v = value
        while (v > 0x7F) {
            out.write((v.toInt() and 0x7F) or 0x80)
            v = v ushr 7
        }
        out.write(v.toInt() and 0x7F)
        return out.toByteArray()
    }

    fun encodeField(fieldNumber: Int, wireType: Int, data: ByteArray): ByteArray {
        val tag = encodeVarint(((fieldNumber shl 3) or wireType).toLong())
        return tag + data
    }

    fun encodeString(fieldNumber: Int, value: String): ByteArray {
        val bytes = value.toByteArray(Charsets.UTF_8)
        return encodeField(fieldNumber, 2, encodeVarint(bytes.size.toLong()) + bytes)
    }

    fun encodeVarintField(fieldNumber: Int, value: Long): ByteArray {
        return encodeField(fieldNumber, 0, encodeVarint(value))
    }

    fun encodeBoolField(fieldNumber: Int, value: Boolean): ByteArray {
        return encodeVarintField(fieldNumber, if (value) 1L else 0L)
    }

    fun encodeLenDelim(fieldNumber: Int, data: ByteArray): ByteArray {
        return encodeField(fieldNumber, 2, encodeVarint(data.size.toLong()) + data)
    }

    // --- PairMobileRequest ---

    fun buildPairMobileRequest(
        activationToken: String,
        confirmationToken: String,
        deviceName: String = "ehg-token-extractor",
        version: String = "v0.32.0"
    ): ByteArray {
        val requestId = (1..1000001).random().toLong()
        val timestamp = System.currentTimeMillis() / 1000

        // PairMobileDevice (field 4 of User)
        val pairDevice = encodeString(1, activationToken) +
                encodeString(2, confirmationToken) +
                encodeString(3, deviceName) +
                encodeBoolField(4, true)

        // User (field 8 of Request) containing PairMobileDevice (field 4)
        val user = encodeLenDelim(4, pairDevice)

        // Request
        val request = encodeVarintField(1, requestId) +
                encodeString(2, version) +
                encodeVarintField(3, timestamp) +
                encodeLenDelim(8, user)

        // BleProtocol.request (field 1)
        return encodeLenDelim(1, request)
    }

    // --- PairMobileConfirmation ---

    /**
     * Build PairMobileConfirmation(success=true) to finalize the pairing.
     * The SCU expects this after sending PairMobileResponse — without it,
     * the pairing may not be persisted on the SCU side.
     */
    fun buildPairMobileConfirmation(
        success: Boolean = true,
        version: String = "v0.32.0"
    ): ByteArray {
        val requestId = (1..1000001).random().toLong()
        val timestamp = System.currentTimeMillis() / 1000

        // PairMobileConfirmation (field 5 of User)
        val confirmation = encodeBoolField(1, success)

        // User (field 8 of Request) containing PairMobileConfirmation (field 5)
        val user = encodeLenDelim(5, confirmation)

        // Request
        val request = encodeVarintField(1, requestId) +
                encodeString(2, version) +
                encodeVarintField(3, timestamp) +
                encodeLenDelim(8, user)

        // BleProtocol.request (field 1)
        return encodeLenDelim(1, request)
    }

    // --- PIA Frame ---

    fun wrapPiaFrame(payload: ByteArray): ByteArray {
        val header = ByteBuffer.allocate(10).order(ByteOrder.BIG_ENDIAN)
        header.putShort(PIA_MAGIC)
        header.putInt(payload.size)
        header.putInt(0) // CRC placeholder

        val frame = header.array() + payload

        // Compute CRC32 over frame with zeroed CRC field
        val crc = CRC32()
        crc.update(frame)
        val crcValue = crc.value.toInt()

        // Write CRC into frame bytes 6-9
        ByteBuffer.wrap(frame, 6, 4).order(ByteOrder.BIG_ENDIAN).putInt(crcValue)

        return frame
    }

    // --- PairMobileResponse decoder ---

    data class PairMobileResponse(
        val remoteAccessToken: String?,
        val remoteAccessRefreshToken: String?,
        val confirmationRequired: Boolean?
    )

    fun parsePairMobileResponse(data: ByteArray): PairMobileResponse? {
        // Walk the protobuf to find Response.mobilePair (field 9)
        val response = findField(data, 2) ?: return null // BleProtocol.response
        val mobilePair = findField(response, 9) ?: return null // Response.mobilePair

        val token = readString(mobilePair, 1)
        val refreshToken = readString(mobilePair, 2)
        val confirmRequired = readBool(mobilePair, 3)

        return PairMobileResponse(token, refreshToken, confirmRequired)
    }

    // --- Protobuf field readers ---

    private fun findField(data: ByteArray, targetField: Int): ByteArray? {
        var offset = 0
        while (offset < data.size) {
            val (tag, tagLen) = readVarint(data, offset) ?: return null
            offset += tagLen
            val fieldNumber = (tag shr 3).toInt()
            val wireType = (tag and 0x07).toInt()

            when (wireType) {
                0 -> { // varint
                    val (_, vLen) = readVarint(data, offset) ?: return null
                    if (fieldNumber == targetField) return data.sliceArray(offset until offset + vLen)
                    offset += vLen
                }
                2 -> { // length-delimited
                    val (len, lenLen) = readVarint(data, offset) ?: return null
                    offset += lenLen
                    val end = offset + len.toInt()
                    if (end > data.size) return null
                    if (fieldNumber == targetField) return data.sliceArray(offset until end)
                    offset = end
                }
                1 -> { // fixed64 — skip so parsing doesn't abort before the target field
                    if (offset + 8 > data.size) return null
                    if (fieldNumber == targetField) return data.sliceArray(offset until offset + 8)
                    offset += 8
                }
                5 -> { // fixed32 — skip so parsing doesn't abort before the target field
                    if (offset + 4 > data.size) return null
                    if (fieldNumber == targetField) return data.sliceArray(offset until offset + 4)
                    offset += 4
                }
                else -> return null // unsupported wire type (3/4 = groups, deprecated)
            }
        }
        return null
    }

    private fun readString(data: ByteArray, fieldNumber: Int): String? {
        val bytes = findField(data, fieldNumber) ?: return null
        return String(bytes, Charsets.UTF_8)
    }

    private fun readBool(data: ByteArray, fieldNumber: Int): Boolean? {
        val bytes = findField(data, fieldNumber) ?: return null
        return if (bytes.isNotEmpty()) bytes[0] != 0.toByte() else null
    }

    private fun readVarint(data: ByteArray, offset: Int): Pair<Long, Int>? {
        var result = 0L
        var shift = 0
        var i = offset
        while (i < data.size) {
            val b = data[i].toInt() and 0xFF
            result = result or ((b.toLong() and 0x7F) shl shift)
            i++
            if (b and 0x80 == 0) return Pair(result, i - offset)
            shift += 7
            if (shift > 63) return null
        }
        return null
    }

    // --- Incoming PIA frame reassembly ---

    private fun indexOfMagic(data: ByteArray, from: Int): Int {
        var i = if (from < 0) 0 else from
        while (i + 1 < data.size) {
            if (data[i] == 0xA0.toByte() && data[i + 1] == 0xCB.toByte()) return i
            i++
        }
        return -1
    }

    /**
     * Accumulates decrypted plaintext bytes and extracts complete BLE PIA frame
     * PAYLOADS (10-byte header stripped), mirroring the proven Python
     * `_FrameAccumulator` in the HA integration's ble_client.py.
     *
     * Incoming responses are PIA-framed exactly like the outgoing ones:
     *   2-byte magic 0xA0CB + 4-byte big-endian length + 4-byte CRC32 + payload.
     * The protobuf body starts only AFTER that header. Feeding the raw
     * accumulated bytes straight into [parsePairMobileResponse] makes the
     * protobuf walk start on the magic byte (0xA0) and mis-parse, so
     * PairMobileResponse is never recognised even though the bytes arrived.
     * Frames may arrive split across TLS records or coalesced with periodic
     * status pushes; this class resyncs to each magic marker, waits for a full
     * frame, then yields the header-stripped payload. A partial trailing frame
     * stays buffered until the rest arrives.
     */
    class PiaFrameAccumulator {
        private val buf = ByteArrayOutputStream()

        fun feed(data: ByteArray): List<ByteArray> {
            buf.write(data)
            val bytes = buf.toByteArray()
            val payloads = mutableListOf<ByteArray>()
            var offset = 0
            while (true) {
                val idx = indexOfMagic(bytes, offset)
                if (idx < 0) {
                    // No magic marker in the remainder — discard it (matches Python).
                    offset = bytes.size
                    break
                }
                offset = idx
                if (bytes.size - offset < PIA_HEADER_SIZE) break // header incomplete
                val len = ByteBuffer.wrap(bytes, offset + 2, 4)
                    .order(ByteOrder.BIG_ENDIAN).int
                if (len < 0) { offset += 2; continue } // bogus length — skip past this magic
                val frameLen = PIA_HEADER_SIZE + len
                if (bytes.size - offset < frameLen) break // frame incomplete — wait for more
                payloads.add(bytes.sliceArray(offset + PIA_HEADER_SIZE until offset + frameLen))
                offset += frameLen
            }
            // Retain only the unconsumed tail for the next feed().
            val tail = bytes.copyOfRange(offset, bytes.size)
            buf.reset()
            buf.write(tail)
            return payloads
        }
    }
}
