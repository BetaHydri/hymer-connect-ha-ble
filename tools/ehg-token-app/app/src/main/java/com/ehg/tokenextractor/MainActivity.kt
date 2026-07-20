package com.ehg.tokenextractor

import android.Manifest
import android.bluetooth.*
import android.bluetooth.le.*
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.ParcelUuid
import android.util.Log
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.lifecycle.lifecycleScope
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import kotlinx.coroutines.*
import java.io.ByteArrayOutputStream
import java.util.*

/**
 * EHG Token Extractor — Single-activity Android app.
 *
 * Extracts the EHG Remote Access Refresh Token from the vehicle's SCU
 * via BLE, bypassing the need for mitmproxy. The token can then be
 * pasted into the Home Assistant HYMER Connect integration config.
 *
 * Flow:
 * 1. Login to EHG cloud API
 * 2. Get confirmation token for the QR code activation token
 * 3. BLE scan for SCU (or use provided MAC)
 * 4. Bond with SCU (user must press CONNECTION on SCU touch panel)
 * 5. TLS 1.0/1.1 handshake over NUS
 * 6. Send PairMobileRequest (PIA protobuf)
 * 7. Receive PairMobileResponse with refresh token
 * 8. Display token for copy/paste
 *
 * @author Jan Tiedemann
 * @date 2026
 */
class MainActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "EhgTokenExtractor"
        private const val REQUEST_PERMISSIONS = 100
        private val NUS_SERVICE_UUID = UUID.fromString("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
        private val NUS_RX_UUID = UUID.fromString("6E400002-B5A3-F393-E0A9-E50E24DCCA9E")
        private val NUS_TX_UUID = UUID.fromString("6E400003-B5A3-F393-E0A9-E50E24DCCA9E")
        private val CCCD_UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    }

    private lateinit var editEmail: EditText
    private lateinit var editPassword: EditText
    private lateinit var editQrToken: EditText
    private lateinit var btnStart: Button
    private lateinit var btnCopy: Button
    private lateinit var btnScanQr: Button
    private lateinit var txtLog: TextView

    private var extractedToken: String? = null
    private var bluetoothGatt: BluetoothGatt? = null
    private val rxQueue = LinkedList<ByteArray>()
    private var rxCharacteristic: BluetoothGattCharacteristic? = null
    private var servicesDiscovered = CompletableDeferred<Unit>()
    private var mtuNegotiated = CompletableDeferred<Unit>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        editEmail = findViewById(R.id.editEmail)
        editPassword = findViewById(R.id.editPassword)
        editQrToken = findViewById(R.id.editQrToken)
        btnStart = findViewById(R.id.btnStart)
        btnCopy = findViewById(R.id.btnCopy)
        btnScanQr = findViewById(R.id.btnScanQr)
        txtLog = findViewById(R.id.txtLog)

        btnStart.setOnClickListener { startExtraction() }
        btnCopy.setOnClickListener { copyTokenToClipboard() }
        btnScanQr.setOnClickListener { scanQrCode() }

        requestPermissions()
    }

    private fun requestPermissions() {
        val perms = mutableListOf(
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.INTERNET
        )
        if (Build.VERSION.SDK_INT >= 31) {
            perms.add(Manifest.permission.BLUETOOTH_CONNECT)
            perms.add(Manifest.permission.BLUETOOTH_SCAN)
        }
        val needed = perms.filter {
            ActivityCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), REQUEST_PERMISSIONS)
        }
    }

    private fun scanQrCode() {
        val options = GmsBarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_QR_CODE)
            .enableAutoZoom()
            .build()
        val scanner = GmsBarcodeScanning.getClient(this, options)
        scanner.startScan()
            .addOnSuccessListener { barcode ->
                val rawValue = barcode.rawValue
                if (!rawValue.isNullOrEmpty()) {
                    editQrToken.setText(rawValue)
                    log("QR scanned: ${rawValue.take(30)}...")
                }
            }
            .addOnFailureListener { e ->
                log("QR scan failed: ${e.message}")
                Toast.makeText(this, "QR scan failed. Enter token manually.", Toast.LENGTH_SHORT).show()
            }
    }

    private fun log(msg: String) {
        Log.d(TAG, msg)
        runOnUiThread {
            txtLog.append("\n$msg")
        }
    }

    private fun startExtraction() {
        val brand = "hymer" // Default brand; works for all EHG brands
        val email = editEmail.text.toString().trim()
        val password = editPassword.text.toString().trim()
        val qrToken = editQrToken.text.toString().trim()

        if (email.isEmpty() || password.isEmpty() || qrToken.isEmpty()) {
            log("ERROR: Email, password, and QR token are required")
            return
        }

        btnStart.isEnabled = false
        txtLog.text = "Starting token extraction..."

        lifecycleScope.launch {
            try {
                // Step 1: Login
                log("Step 1: Logging into EHG API ($brand)...")
                val api = EhgApi(brand)
                if (!api.login(email, password)) {
                    log("ERROR: Login failed. Check credentials.")
                    return@launch
                }
                log("✅ Login successful")

                // Step 2: Get confirmation token
                log("Step 2: Getting confirmation token...")
                log("  QR token: ${qrToken.take(20)}... (${qrToken.length} chars)")
                val confirmationToken = api.getConfirmationToken(qrToken)
                if (confirmationToken == null) {
                    log("ERROR: Could not get confirmation token.")
                    api.lastError?.let { log("  Detail: $it") }
                    log("  Check QR code — should start with 'eyJ' (JWT format)")
                    return@launch
                }
                log("✅ Confirmation token received (${confirmationToken.length} chars)")

                // Step 3: BLE connect + bond
                log("Step 3: Scanning for SCU via BLE...")
                log("⚠️ Press CONNECTION on the SCU touch panel NOW!")
                val device = scanForScu()
                if (device == null) {
                    log("ERROR: Could not find SCU via BLE scan")
                    return@launch
                }
                log("Found SCU: ${device.address}")

                // Bond
                log("Step 4: Bonding with SCU...")
                log("⚠️ Press CONNECTION (Verbindung) on the SCU touch panel!")
                if (device.bondState != BluetoothDevice.BOND_BONDED) {
                    device.createBond()
                    // Wait for bonding — user must press CONNECTION on SCU display
                    repeat(60) {
                        delay(1000)
                        if (device.bondState == BluetoothDevice.BOND_BONDED) return@repeat
                        if (it % 5 == 4) {
                            log("  ⏳ Waiting for CONNECTION button... (${it + 1}/60s)")
                        }
                    }
                    if (device.bondState != BluetoothDevice.BOND_BONDED) {
                        log("❌ Bonding failed — CONNECTION was not pressed on SCU display")
                        log("   Press CONNECTION on the SCU touch panel, then try again")
                        return@launch
                    }
                }
                log("✅ Bonded with SCU")

                // GATT connect
                log("Step 5: GATT connecting...")
                servicesDiscovered = CompletableDeferred()
                mtuNegotiated = CompletableDeferred()
                connectGatt(device)
                // Wait for service discovery callback (up to 10s)
                try {
                    withTimeout(10000) { servicesDiscovered.await() }
                } catch (_: TimeoutCancellationException) {
                    log("❌ Service discovery timed out")
                    return@launch
                }
                if (rxCharacteristic == null) {
                    log("❌ NUS RX characteristic not found — is this an SCU?")
                    return@launch
                }
                log("✅ GATT connected")

                // Request MTU 245 (like the EHG app)
                bluetoothGatt?.requestMtu(245)
                try {
                    withTimeout(5000) { mtuNegotiated.await() }
                } catch (_: TimeoutCancellationException) {
                    log("  ⚠️ MTU negotiation timeout — using default MTU=23")
                }
                log("✅ MTU negotiated: $negotiatedMtu")

                // Enable NUS TX notifications
                enableNotifications()
                delay(500)

                // Step 6: TLS handshake
                log("Step 6: TLS handshake...")
                val tls = TlsOverBle()
                val clientHello = try {
                    tls.beginHandshake { m -> log(m) }
                } catch (e: Exception) {
                    log("❌ TLS init failed [${e.javaClass.simpleName}]: ${e.message ?: "(no message)"}")
                    e.stackTrace.take(4).forEach { log("    at $it") }
                    return@launch
                }
                writeToScu(clientHello)

                // Complete handshake (read server responses, send our responses)
                val deadline = System.currentTimeMillis() + 20000
                while (!tls.isHandshakeComplete && System.currentTimeMillis() < deadline) {
                    val incoming = waitForData(5000) ?: continue
                    val (outbound, _) = tls.feedEncrypted(incoming)
                    if (outbound.isNotEmpty()) {
                        writeToScu(outbound)
                    }
                }

                if (!tls.isHandshakeComplete) {
                    log("ERROR: TLS handshake timed out")
                    return@launch
                }
                log("✅ TLS session established")

                // Step 7: Send PairMobileRequest
                log("Step 7: Sending PairMobileRequest...")
                val request = PiaProtocol.buildPairMobileRequest(
                    activationToken = qrToken,
                    confirmationToken = confirmationToken
                )
                val piaFrame = PiaProtocol.wrapPiaFrame(request)
                val encrypted = tls.encrypt(piaFrame)
                writeToScu(encrypted)
                log("  Sent ${encrypted.size} bytes encrypted")

                // Step 8: Wait for PairMobileResponse
                log("Step 8: Waiting for PairMobileResponse...")
                log("  ⚠️ Press ALLOW on SCU touchscreen if prompted!")
                val responseDeadline = System.currentTimeMillis() + 120000 // 2 min
                // Incoming data is PIA-framed (magic + length + CRC + payload) and may
                // arrive split across TLS records or coalesced with periodic status
                // pushes from the SCU. The accumulator resyncs to each frame's magic
                // marker and yields header-stripped payloads ready for protobuf
                // parsing — feeding raw bytes straight to the parser makes it start on
                // the 0xA0 magic byte and never recognise PairMobileResponse.
                val frameAccumulator = PiaProtocol.PiaFrameAccumulator()
                var confirmationPrompted = false

                while (System.currentTimeMillis() < responseDeadline) {
                    val data = waitForData(5000) ?: continue
                    val decrypted = tls.decrypt(data)
                    if (decrypted.isEmpty()) continue
                    log("  Received ${decrypted.size} bytes decrypted")

                    for (framePayload in frameAccumulator.feed(decrypted)) {
                        val parsed = PiaProtocol.parsePairMobileResponse(framePayload) ?: continue
                        val refreshToken = parsed.remoteAccessRefreshToken
                        if (!refreshToken.isNullOrEmpty()) {
                            extractedToken = refreshToken

                            // Send PairMobileConfirmation to finalize pairing on SCU
                            log("  Sending PairMobileConfirmation...")
                            try {
                                val confirmMsg = PiaProtocol.buildPairMobileConfirmation(success = true)
                                val confirmFrame = PiaProtocol.wrapPiaFrame(confirmMsg)
                                val confirmEncrypted = tls.encrypt(confirmFrame)
                                writeToScu(confirmEncrypted)
                                log("  ✅ PairMobileConfirmation sent")
                            } catch (e: Exception) {
                                log("  ⚠️ PairMobileConfirmation failed: ${e.message}")
                                // Non-fatal — token was already received
                            }

                            log("")
                            log("🎉 SUCCESS! EHG Refresh Token extracted!")
                            log("Token: ${extractedToken!!.take(20)}...")
                            log("")
                            log("Copy this token and paste it into your")
                            log("Home Assistant HYMER Connect config.")
                            runOnUiThread {
                                btnCopy.isEnabled = true
                            }
                            return@launch
                        }
                        if (parsed.confirmationRequired == true && !confirmationPrompted) {
                            confirmationPrompted = true
                            log("  ⏳ SCU requires confirmation — press ALLOW on the SCU touchscreen now, then keep waiting…")
                        }
                    }
                }

                log("ERROR: Timed out waiting for PairMobileResponse")

            } catch (e: Exception) {
                // NOTE: NullPointerException has a null message — always log the
                // exception class + first stack frames so failures are actionable.
                log("ERROR [${e.javaClass.simpleName}]: ${e.message ?: "(no message)"}")
                e.stackTrace.take(5).forEach { log("    at $it") }
                Log.e(TAG, "Extraction failed", e)
            } finally {
                bluetoothGatt?.disconnect()
                bluetoothGatt?.close()
                bluetoothGatt = null
                runOnUiThread { btnStart.isEnabled = true }
            }
        }
    }

    private fun getBluetoothAdapter(): BluetoothAdapter? {
        val manager = getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
        return manager.adapter
    }

    private suspend fun scanForScu(): BluetoothDevice? = withContext(Dispatchers.Main) {
        val adapter = getBluetoothAdapter() ?: return@withContext null
        val scanner = adapter.bluetoothLeScanner ?: return@withContext null
        var found: BluetoothDevice? = null

        val callback = object : ScanCallback() {
            override fun onScanResult(callbackType: Int, result: ScanResult) {
                val name = result.device.name ?: result.scanRecord?.deviceName ?: ""
                // SCU advertises as "HYMER XXXXX" (e.g. "HYMER 00013970")
                // Also check for other EHG brand prefixes
                if (name.startsWith("HYMER", ignoreCase = true) ||
                    name.startsWith("Buerstner", ignoreCase = true) ||
                    name.startsWith("Dethleffs", ignoreCase = true) ||
                    name.startsWith("Eriba", ignoreCase = true) ||
                    name.startsWith("Carado", ignoreCase = true) ||
                    name.startsWith("Laika", ignoreCase = true) ||
                    name.startsWith("LMC", ignoreCase = true) ||
                    name.startsWith("Sunlight", ignoreCase = true) ||
                    name.startsWith("Niesmann", ignoreCase = true) ||
                    name.contains("SCU", ignoreCase = true) ||
                    name.contains("SIU", ignoreCase = true)) {
                    log("  Found: $name (${result.device.address}, RSSI=${result.rssi})")
                    found = result.device
                    scanner.stopScan(this)
                }
            }
        }

        // Don't filter by service UUID — many phones don't include UUIDs in
        // scan results until after GATT connection. The SCU advertises its
        // brand name (e.g. "HYMER 00013970"), so we match by name prefix.
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()

        scanner.startScan(null, settings, callback)
        delay(10000) // 10s scan
        try { scanner.stopScan(callback) } catch (_: Exception) {}

        found
    }

    private val gattCallback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothGatt.STATE_CONNECTED) {
                gatt.discoverServices()
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                log("  ⚠️ GATT disconnected (status=$status)")
            }
        }

        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            val nusService = gatt.getService(NUS_SERVICE_UUID)
            rxCharacteristic = nusService?.getCharacteristic(NUS_RX_UUID)
            log("  Services discovered: ${gatt.services.size}")
            if (!servicesDiscovered.isCompleted) servicesDiscovered.complete(Unit)
        }

        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            negotiatedMtu = mtu
            log("  MTU changed to $mtu (chunk size: ${maxOf(20, mtu - 3)})")
            if (!mtuNegotiated.isCompleted) mtuNegotiated.complete(Unit)
        }

        override fun onCharacteristicWrite(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int
        ) {
            if (characteristic.uuid == NUS_RX_UUID) {
                pendingWriteComplete?.complete(Unit)
            }
        }

        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray
        ) {
            if (characteristic.uuid == NUS_TX_UUID) {
                synchronized(rxQueue) {
                    rxQueue.add(value)
                    (rxQueue as Object).notifyAll()
                }
            }
        }

        @Deprecated("Deprecated in Java")
        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic
        ) {
            if (characteristic.uuid == NUS_TX_UUID) {
                val value = characteristic.value ?: return
                synchronized(rxQueue) {
                    rxQueue.add(value)
                    (rxQueue as Object).notifyAll()
                }
            }
        }
    }

    private fun connectGatt(device: BluetoothDevice) {
        bluetoothGatt = device.connectGatt(this, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
    }

    private fun enableNotifications() {
        val gatt = bluetoothGatt ?: return
        val nusService = gatt.getService(NUS_SERVICE_UUID) ?: return
        val txChar = nusService.getCharacteristic(NUS_TX_UUID) ?: return
        gatt.setCharacteristicNotification(txChar, true)
        val cccd = txChar.getDescriptor(CCCD_UUID)
        cccd?.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
        gatt.writeDescriptor(cccd)
    }

    private var negotiatedMtu: Int = 23  // Updated by onMtuChanged callback
    private val writeComplete = CompletableDeferred<Unit>()
    @Volatile private var pendingWriteComplete: CompletableDeferred<Unit>? = null

    private suspend fun writeToScu(data: ByteArray) = withContext(Dispatchers.IO) {
        val gatt = bluetoothGatt ?: return@withContext
        val char = rxCharacteristic ?: return@withContext
        val chunkSize = maxOf(20, negotiatedMtu - 3)  // ATT overhead = 3 bytes
        val chunks = data.toList().chunked(chunkSize)
        // Always use Write-With-Response for protocol-critical messages.
        // Write-Without-Response causes ATT 0x0e on the SCU's NUS RX buffer
        // at MTU=23 (confirmed on RPi4 and vehicle testing).
        log("  TX ${data.size} bytes -> ${chunks.size} chunks (chunkSize=$chunkSize, MTU=$negotiatedMtu)")
        for ((i, chunk) in chunks.withIndex()) {
            val ack = CompletableDeferred<Unit>()
            pendingWriteComplete = ack
            char.value = chunk.toByteArray()
            char.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            gatt.writeCharacteristic(char)
            // Wait for onCharacteristicWrite callback before sending next chunk
            try {
                withTimeout(5000) { ack.await() }
            } catch (_: TimeoutCancellationException) {
                log("  ⚠️ Write ACK timeout on chunk ${i + 1}/${chunks.size}")
            }
            // 100ms pacing between chunks (matches ble_client.py)
            if (i < chunks.size - 1) {
                delay(100)
            }
        }
    }

    private suspend fun waitForData(timeoutMs: Long): ByteArray? = withContext(Dispatchers.IO) {
        val deadline = System.currentTimeMillis() + timeoutMs
        synchronized(rxQueue) {
            while (rxQueue.isEmpty() && System.currentTimeMillis() < deadline) {
                val remaining = deadline - System.currentTimeMillis()
                if (remaining > 0) {
                    (rxQueue as Object).wait(remaining)
                }
            }
            if (rxQueue.isEmpty()) return@withContext null
            // Drain ALL available chunks and concatenate — a single TLS record
            // spans multiple BLE notifications at MTU=23
            val accumulated = ByteArrayOutputStream()
            while (rxQueue.isNotEmpty()) {
                accumulated.write(rxQueue.poll())
            }
            accumulated.toByteArray()
        }
    }

    private fun copyTokenToClipboard() {
        val token = extractedToken ?: return
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("EHG Token", token))
        Toast.makeText(this, "Token copied to clipboard!", Toast.LENGTH_LONG).show()
    }

    override fun onDestroy() {
        super.onDestroy()
        bluetoothGatt?.disconnect()
        bluetoothGatt?.close()
    }
}
