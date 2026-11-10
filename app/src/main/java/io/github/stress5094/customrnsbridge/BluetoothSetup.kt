package io.github.stress5094.customrnsbridge

import android.annotation.SuppressLint
import android.app.Activity
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.core.content.ContextCompat
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import java.util.concurrent.Executors

/**
 * "Switch to Bluetooth": the whole cable-to-Bluetooth move for an RNode in one
 * tap, while the radio is still on the USB cable.
 *
 *  1. Over the cable: switch the radio's Bluetooth on, then put it into
 *     pairing mode. The radio reports its pairing code back over the cable
 *     (rns_bridge._PinSniffingSerial), so the app has the code before Android
 *     asks for it.
 *  2. Scan for RNodes and take the STRONGEST one. The radio being set up is on
 *     a cable to this phone, so it is the closest; the name is shown so the
 *     operator can see which one was picked.
 *  3. Pair, typing the code into Android's prompt where the phone allows it
 *     (setPin); where it does not, the code is on screen in large type.
 *  4. Hand the address back. The caller saves it and restarts the bridge.
 *
 * Replaces about a dozen taps across Turn on / Pairing mode / Scan / Pair /
 * Save. Those manual controls stay in Radio link for the cases this cannot
 * cover (no cable, two radios side by side).
 */
class BluetoothSetup(
    private val activity: Activity,
    private val onDone: (address: String, name: String) -> Unit,
) {
    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()
    private val adapter = (activity.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
    private lateinit var status: TextView
    private var dialog: AlertDialog? = null
    private var cancelled = false
    private var scanning = false
    // address -> (device, best RSSI, advertised name). The name is kept from
    // the advertisement: an unpaired device's own .name is often null.
    private val seen = LinkedHashMap<String, Triple<BluetoothDevice, Int, String>>()
    private var bondReceiver: BroadcastReceiver? = null
    private var pinReceiver: BroadcastReceiver? = null

    fun start() {
        status = TextView(activity).apply {
            textSize = 15f
            setPadding(dp(24), dp(12), dp(24), dp(4))
        }
        dialog = MaterialAlertDialogBuilder(activity)
            .setTitle("Switch to Bluetooth")
            .setView(status)
            .setNegativeButton("Cancel") { _, _ -> cancel() }
            .setCancelable(false)
            .show()
        say("Turning on the radio's Bluetooth…")
        executor.execute { radioSteps() }
    }

    private fun say(text: String) = handler.post { if (::status.isInitialized) status.text = text }

    /** Background thread: the two commands over the cable. */
    private fun radioSteps() {
        val on = PythonRnsService.call("enable_rnode_bluetooth", "on").orEmpty()
        if (cancelled) return
        if (!on.startsWith("Bluetooth switched on")) {
            fail(on.ifEmpty { "The bridge is not running, so the radio cannot be reached." } +
                "\n\nThis needs the radio on the USB cable and showing as connected.")
            return
        }
        // Give the radio a moment to bring its Bluetooth up before asking it
        // to pair; a pairing request on a radio still starting is ignored.
        Thread.sleep(1500)
        if (cancelled) return
        say("Putting the radio into pairing mode…")
        PythonRnsService.call("enable_rnode_bluetooth", "pair")
        val pin = PythonRnsService.call("get_bt_pairing_pin").orEmpty()
        if (cancelled) return
        handler.post { scan(pin) }
    }

    @SuppressLint("MissingPermission")
    private fun scan(pin: String) {
        val scanner = adapter?.bluetoothLeScanner
        if (adapter == null || !adapter.isEnabled || scanner == null) {
            fail("Bluetooth is off on this phone. Switch it on and try again.")
            return
        }
        say("Looking for the radio…")
        seen.clear()
        scanner.startScan(null,
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(),
            scanCallback)
        scanning = true
        // Long enough for a few advertisements from every radio in range, so
        // the strongest is a fair pick; give up after 15 s with none.
        handler.postDelayed({ pick(pin, finalTry = false) }, SCAN_MS)
        handler.postDelayed({ pick(pin, finalTry = true) }, SCAN_GIVE_UP_MS)
    }

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val name = result.scanRecord?.deviceName ?: result.device.name ?: return
            if (!name.startsWith(BleSerialBridgeService.RNODE_NAME_PREFIX)) return
            handler.post {
                val best = seen[result.device.address]?.second ?: Int.MIN_VALUE
                seen[result.device.address] = Triple(result.device, maxOf(best, result.rssi), name)
            }
        }
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        try { adapter?.bluetoothLeScanner?.stopScan(scanCallback) } catch (e: Exception) { }
    }

    @SuppressLint("MissingPermission")
    private fun pick(pin: String, finalTry: Boolean) {
        if (cancelled || !scanning) return
        val strongest = seen.values.maxByOrNull { it.second }
        if (strongest == null) {
            if (finalTry) {
                stopScan()
                fail("No RNode found. Check the radio is powered and on the cable, " +
                    "then try again.")
            }
            return
        }
        stopScan()
        handler.removeCallbacksAndMessages(null)
        val device = strongest.first
        val name = strongest.third
        if (device.bondState == BluetoothDevice.BOND_BONDED) {
            finish(device, name)
            return
        }
        bond(device, name, pin)
    }

    @SuppressLint("MissingPermission")
    private fun bond(device: BluetoothDevice, name: String, pin: String) {
        say("Pairing with $name…")
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                val d = deviceExtra(intent) ?: return
                if (d.address != device.address) return
                when (intent.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, -1)) {
                    BluetoothDevice.BOND_BONDED -> finish(device, name)
                    BluetoothDevice.BOND_NONE -> fail("Pairing with $name did not complete. " +
                        "The radio stays in pairing mode for about a minute: try again, " +
                        "or check the code.")
                }
            }
        }
        ContextCompat.registerReceiver(activity, receiver,
            IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED),
            ContextCompat.RECEIVER_EXPORTED)
        bondReceiver = receiver
        autoEnterPin(device, name, pin)
        if (!device.createBond()) fail("Android refused to start pairing with $name.")
    }

    /**
     * Answers Android's PIN prompt for this one device, where the phone allows.
     *
     * MEASURED (OnePlus 8T + Heltec v3, 2026-11-10): the radio reports its code
     * over the cable only once pairing has STARTED -- 0.4 s after createBond(),
     * not when it entered pairing mode. So the code is fetched when Android's
     * request arrives, waiting up to a few seconds for the radio, and typed in
     * with setPin. Android's prompt is already showing by then; a successful
     * setPin completes the pairing and closes it. Where setPin is refused, the
     * code is shown in this dialog to type by hand.
     */
    private fun autoEnterPin(device: BluetoothDevice, name: String, known: String) {
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) {
                if (intent.action != BluetoothDevice.ACTION_PAIRING_REQUEST) return
                if (deviceExtra(intent)?.address != device.address) return
                val variant = intent.getIntExtra(BluetoothDevice.EXTRA_PAIRING_VARIANT, -1)
                Thread { answer(device, name, known, variant) }.start()
            }
        }
        ContextCompat.registerReceiver(activity, receiver,
            IntentFilter(BluetoothDevice.ACTION_PAIRING_REQUEST).apply {
                priority = IntentFilter.SYSTEM_HIGH_PRIORITY - 1
            }, ContextCompat.RECEIVER_EXPORTED)
        pinReceiver = receiver
    }

    @SuppressLint("MissingPermission")
    private fun answer(device: BluetoothDevice, name: String, known: String, variant: Int) {
        var pin = known
        val deadline = System.currentTimeMillis() + PIN_WAIT_MS
        while (pin.isEmpty() && !cancelled && System.currentTimeMillis() < deadline) {
            pin = PythonRnsService.call("get_bt_pairing_pin").orEmpty()
            if (pin.isEmpty()) Thread.sleep(100)
        }
        if (cancelled) return
        if (pin.isEmpty()) {
            say("Pairing with $name…\n\nThe radio did not report its code. Read it off " +
                "the radio's screen and type it into Android's prompt.")
            return
        }
        val entered = try {
            (variant == BluetoothDevice.PAIRING_VARIANT_PIN || variant == 1) &&
                device.setPin(pin.toByteArray())
        } catch (e: Exception) {
            Log.w(TAG, "Could not enter the PIN automatically", e)
            false
        }
        Log.i(TAG, "Pairing request variant=$variant, PIN entered automatically: $entered")
        say(if (entered) "Pairing with $name… code $pin entered."
            else "Pairing with $name…\n\nType this code into Android's prompt:\n\n$pin")
    }

    private fun deviceExtra(intent: Intent): BluetoothDevice? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
            intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
        else @Suppress("DEPRECATION") intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)

    private fun finish(device: BluetoothDevice, name: String) = handler.post {
        if (cancelled) return@post
        cleanup()
        dialog?.dismiss()
        Log.i(TAG, "Bluetooth setup paired with $name (${device.address})")
        onDone(device.address, name)
    }

    private fun fail(message: String) = handler.post {
        if (cancelled) return@post
        cleanup()
        dialog?.dismiss()
        MaterialAlertDialogBuilder(activity)
            .setTitle("Switch to Bluetooth")
            .setMessage(message)
            .setPositiveButton("OK", null)
            .show()
    }

    private fun cancel() {
        cancelled = true
        cleanup()
    }

    private fun cleanup() {
        stopScan()
        handler.removeCallbacksAndMessages(null)
        for (r in listOfNotNull(bondReceiver, pinReceiver)) {
            try { activity.unregisterReceiver(r) } catch (e: Exception) { }
        }
        bondReceiver = null
        pinReceiver = null
        executor.shutdown()
    }

    private fun dp(v: Int) = (v * activity.resources.displayMetrics.density).toInt()

    companion object {
        private const val TAG = "BluetoothSetup"
        private const val SCAN_MS = 5_000L
        private const val SCAN_GIVE_UP_MS = 15_000L
        private const val PIN_WAIT_MS = 5_000L
    }
}
