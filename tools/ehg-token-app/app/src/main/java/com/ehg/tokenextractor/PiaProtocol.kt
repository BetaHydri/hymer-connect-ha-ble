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
                else -> return null // unsupported wire type
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
}
