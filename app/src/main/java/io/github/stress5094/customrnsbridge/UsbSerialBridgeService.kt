package io.github.stress5094.customrnsbridge

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import com.hoho.android.usbserial.driver.UsbSerialDriver
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber
import com.hoho.android.usbserial.util.SerialInputOutputManager
import java.io.IOException

/**
 * Owns the RNode's USB serial connection and republishes it as the byte pipe
 * RadioBridgeService serves to Python. Everything about the pipe itself — the
 * loopback server, the pumps, what a dead radio has to do to the Python
 * connection — lives in the base class and is shared with the Bluetooth
 * transport; this file is only the USB half.
 */
class UsbSerialBridgeService : RadioBridgeService() {

    companion object {
        private const val TAG = "UsbSerialBridge"
        private const val ACTION_USB_PERMISSION = "io.github.stress5094.customrnsbridge.USB_PERMISSION"
        private const val BAUD_RATE = 115200 // confirm against your RNode firmware config

        // How long a re-attach waits before putting up its own permission
        // prompt. Android grants permission by itself to the app chosen to
        // "always open" a device (res/xml/device_filter.xml is what makes this
        // app eligible), but it sends the attach broadcast BEFORE it grants, so
        // asking at once would put a second, redundant prompt on screen. The
        // 3 s retry opens the port as soon as the grant lands either way.
        private const val PERMISSION_PROMPT_DELAY_MS = 2000L

        /**
         * The product name of a radio Android is asking the operator about,
         * or null. Set while this service's permission prompt is waiting, so
         * the screens can say why the radio is down: a Meshtastic board
         * re-enumerates when a settings change restarts it, and Android drops
         * the permission with it (seen 2026-10-14). Same idea as
         * FmAudioBridgeService.keylessReason: a fact about this phone's USB,
         * kept on the Kotlin side.
         */
        @Volatile var awaitingPermissionFor: String? = null
            private set

        /** True between prepareLink() and releaseLink(): read by
         * UsbAttachActivity to decide whether a radio attaching should start
         * the app or is already being looked after. */
        @Volatile var running = false
            private set
    }

    override val tag = TAG
    override val linkDescription: String
        get() = if (RadioLink.isMeshtastic(this)) "Meshtastic link active (USB)"
        else "RNode link active (USB)"
    override val bridgePort = LORA_BRIDGE_PORT

    private lateinit var usbManager: UsbManager
    private val handler = Handler(Looper.getMainLooper())
    @Volatile private var serialPort: UsbSerialPort? = null
    private var ioManager: SerialInputOutputManager? = null

    private val usbPermissionReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            synchronized(this) {
                val device: UsbDevice? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
                }
                when (intent.action) {
                    ACTION_USB_PERMISSION -> {
                        awaitingPermissionFor = null
                        val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
                        if (granted && device != null) {
                            openDevice(device)
                        } else {
                            Log.w(TAG, "USB permission denied for $device")
                        }
                    }
                    // The RNode being physically unplugged is the one case we
                    // can learn about immediately rather than by waiting for a
                    // transfer to fail. Worth handling: the failure mode this
                    // whole path exists to prevent is the bridge continuing to
                    // look healthy after the radio is gone.
                    UsbManager.ACTION_USB_DEVICE_DETACHED ->
                        handleLinkFailure("USB device detached")
                    // ...and on replug, permission may have been revoked with
                    // the disconnect, so go through the request path again
                    // rather than the silent retry.
                    UsbManager.ACTION_USB_DEVICE_ATTACHED -> {
                        Log.i(TAG, "USB device attached, looking for an RNode")
                        // Without prompting first: the grant for an "always
                        // open" device may still be on its way (see
                        // PERMISSION_PROMPT_DELAY_MS).
                        findAndRequestRNode(promptForPermission = false)
                        if (!isLinkOpen()) {
                            handler.postDelayed({
                                if (!isLinkOpen()) findAndRequestRNode(promptForPermission = true)
                            }, PERMISSION_PROMPT_DELAY_MS)
                        } else { /* opened on the spot: permission already held */ }
                    }
                    else -> { /* not one of ours */ }
                }
            }
        }
    }

    override fun prepareLink() {
        running = true
        usbManager = getSystemService(Context.USB_SERVICE) as UsbManager
        val filter = IntentFilter(ACTION_USB_PERMISSION).apply {
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
            addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
        }
        registerReceiver(
            usbPermissionReceiver,
            filter,
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                Context.RECEIVER_NOT_EXPORTED else 0
        )
    }

    override fun openLink(firstAttempt: Boolean) =
        findAndRequestRNode(promptForPermission = firstAttempt)

    override fun isLinkOpen() = serialPort != null

    override fun writeToLink(data: ByteArray) {
        val port = serialPort ?: throw IOException("RNode port is not open")
        port.write(data, 1000)
    }

    override fun closeLink() {
        val port = serialPort
        serialPort = null
        try { ioManager?.stop() } catch (e: Exception) { /* already stopping */ }
        ioManager = null
        // CLOSE the port too, which this did not do until 2026-10-14. stop()
        // only asks the reader thread to exit at its next turn, and that
        // thread is parked in a USB read with no timeout: on a radio that has
        // gone silent but not away, it never gets a turn, and the device
        // connection (with its claimed interface) is never released. Every
        // reconnect leaked one more. Closing cancels the pending read. A
        // suspected part of the "zombie app" of 2026-10-13 (a USB I/O thread
        // stuck until the board was unplugged) -- NOT proven to be its cause.
        try { port?.close() } catch (e: Exception) { /* already gone */ }
    }

    override fun releaseLink() {
        running = false
        awaitingPermissionFor = null
        handler.removeCallbacksAndMessages(null)
        ioManager?.stop()
        serialPort?.close()
        serialPort = null
        unregisterReceiver(usbPermissionReceiver)
    }

    /**
     * Find a connected RNode-compatible USB-serial device and open it.
     *
     * `promptForPermission` is false on the automatic retry path: the retry
     * runs every few seconds while the port is down, and a permission request
     * per attempt would put the system dialog on screen in a loop. Permission,
     * once granted, survives until the device is unplugged — and a replug
     * fires ACTION_USB_DEVICE_ATTACHED, which is where prompting belongs.
     */
    private fun findAndRequestRNode(promptForPermission: Boolean) {
        val availableDrivers: List<UsbSerialDriver> =
            UsbSerialProber.getDefaultProber().findAllDrivers(usbManager)
        if (availableDrivers.isEmpty()) {
            // Quiet on the retry path. This fires every few seconds for as
            // long as the RNode is unplugged, and at warning level it drowns
            // out the lines that actually explain an outage.
            if (promptForPermission) Log.w(TAG, "No USB serial devices found")
            else Log.d(TAG, "Still no USB serial device")
            return
        }
        // Milestone 1: just take the first one. Once you have your board's
        // real VID/PID, filter on driver.device instead.
        val driver = availableDrivers[0]
        if (usbManager.hasPermission(driver.device)) {
            openDevice(driver.device)
        } else if (promptForPermission) {
            val permissionIntent = PendingIntent.getBroadcast(
                this, 0, Intent(ACTION_USB_PERMISSION),
                PendingIntent.FLAG_MUTABLE
            )
            awaitingPermissionFor = driver.device.productName ?: "the radio"
            Log.i(TAG, "Asking for permission to use ${driver.device.productName}")
            usbManager.requestPermission(driver.device, permissionIntent)
        } else {
            Log.w(TAG, "RNode found but no USB permission; waiting for replug")
        }
    }

    private fun openDevice(device: UsbDevice) {
        if (serialPort != null) {
            Log.i(TAG, "RNode port already open, ignoring open request")
            return
        }
        // Some RNode boards (e.g. ESP32-S3's native USB Serial/JTAG
        // peripheral) present a CDC-ACM-shaped descriptor but don't
        // implement the CDC control endpoint fully -- the standard
        // CdcAcmSerialDriver opens fine but then fails on read() with
        // "USB get_status request failed" from testConnection(). Bypass
        // the control endpoint entirely for those by talking to the bulk
        // data endpoints directly.
        val driver: UsbSerialDriver = if (BulkOnlyCdcDriver.probe(device)) {
            Log.i(TAG, "Using BulkOnlyCdcDriver (no CDC control endpoint) for $device")
            BulkOnlyCdcDriver(device)
        } else {
            UsbSerialProber.getDefaultProber().probeDevice(device) ?: run {
                Log.e(TAG, "No driver for $device")
                return
            }
        }
        val connection = usbManager.openDevice(driver.device) ?: run {
            Log.e(TAG, "Failed to open USB connection")
            return
        }
        val port = driver.ports[0]
        try {
            port.open(connection)
            port.setParameters(
                BAUD_RATE, UsbSerialPort.DATABITS_8,
                UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE
            )
        } catch (e: IOException) {
            Log.e(TAG, "Failed to configure serial port", e)
            return
        }
        // A Meshtastic board on nRF52 (RAK4631) stays silent until the host
        // raises DTR -- the serial library does this on a desktop without being
        // asked, and nothing does it here unless we do. DTR only, RTS left low:
        // that is the combination measured to work, and boards with the usual
        // ESP32 auto-reset wiring reset on some DTR/RTS combinations, so this is
        // NOT done for RNodes, which have never needed it.
        if (RadioLink.isMeshtastic(this)) {
            try {
                port.dtr = true
            } catch (e: Exception) {
                Log.w(TAG, "Could not raise DTR: ${e.message}")
            }
        }
        serialPort = port
        awaitingPermissionFor = null
        Log.i(TAG, "${if (RadioLink.isMeshtastic(this)) "Meshtastic" else "RNode"} " +
            "serial port open at ${BAUD_RATE}bps")
        pumpSerialToSocket(port)
    }

    /** RNode -> socket -> Python */
    private fun pumpSerialToSocket(port: UsbSerialPort) {
        val listener = object : SerialInputOutputManager.Listener {
            override fun onNewData(data: ByteArray) {
                deliverFromLink(data)
            }

            override fun onRunError(e: Exception) {
                // closeLink() closing the port is what ends a retired reader:
                // the library reports that as an error too. By then this port
                // is not the current one, and the failure it describes has
                // already been handled -- passing it on could tear down the
                // link that replaced it.
                if (serialPort !== port) {
                    Log.i(TAG, "Retired serial reader ended (${e.message})")
                    return
                }
                Log.e(TAG, "Serial IO error", e)
                handleLinkFailure("serial read failed: ${e.message}")
            }
        }
        ioManager = SerialInputOutputManager(port, listener).also {
            ioExecutor.execute(it)
        }
    }
}
