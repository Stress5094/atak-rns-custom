package io.github.stress5094.customrnsbridge

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.os.Build
import android.util.Log

/**
 * Keys a radio's PTT through a Digirig Lite (or any other C-Media
 * CM108-family USB audio codec): not a serial line, not VOX, but a GPIO pin
 * on the codec's own HID interface, set via a raw USB control transfer --
 * there is no /dev/hidraw on Android, so this talks to the device the way
 * hidraw itself would underneath.
 *
 * A PORT, NOT NEW WORK. This is `0355f92^`'s version of this class, which
 * keyed a real radio and carried real byte-perfect traffic; the FM transport
 * around it was rebuilt and this was not. What changed here is the HID
 * interface lookup (see findHidInterface) and the comments that referred to
 * modules the rebuild replaced.
 *
 * WHY THE REBUILD NEEDS IT AT ALL, which the old transport did not: the
 * operating point measured in Phase 0 is CM108-keyed with a 0.10 s lead-in,
 * against the old transport's 1.6 s VOX lead-in. Deterministic keying is
 * where a large part of the burst-duration saving came from -- fm_config's
 * KEYING_LEAD_IN_S has the measurements -- so keying is now part of the
 * configuration rather than something the cable happens to do.
 *
 * REPORT FORMAT: tools/ptt_test.py's Cm108Ptt (this project's desktop tool,
 * confirmed keying a real radio) writes 5 bytes to /dev/hidraw --
 * [0, 0, iodata, iomask, 0] -- and direwolf's cm108.c (the reference
 * implementation this convention is drawn from) writes the same 5 bytes.
 * Linux's hidraw convention is that byte 0 of a write() is the report ID, and
 * 0 means "this device has no numbered reports" -- the kernel strips it
 * before putting the remaining 4 bytes on the wire. A raw SET_REPORT control
 * transfer is that same wire step done by hand: wValue's low byte IS the
 * report ID (0), so the data stage carries just those 4 bytes,
 * [0, iodata, iomask, 0], not all 5.
 *
 * CONFIRMED ON HARDWARE 2026-09-20 (OnePlus 8T, Digirig Lite): the
 * controlTransfer succeeds AND actually keys the radio -- verified two ways
 * on the laptop's own receiving Digirig Lite: (a) a 2 s test key produces a
 * real ~2 s carrier (RMS rising from a ~1.5 noise floor to 300-1200) that
 * starts and ends within a few hundred ms of the logged key()/unkey() calls;
 * (b) two real chat messages sent that same session both decoded byte-perfect
 * on the receiving end. **This directly contradicts the 2026-09-19 finding
 * recorded in setGpio()'s comment below and in the project bench notes ("no
 * real transmission ever got through... points at the cable's physical PTT
 * wiring instead")** -- on THAT phone/cable/radio combination, that day, the
 * wiring was fine and PTT worked cleanly. The earlier finding is not
 * retracted (it was real, on whatever hardware was in use) but evidently does
 * not generalize across every Digirig Lite unit / radio pairing -- consistent
 * with this project's repeated finding that individual cable and radio units
 * vary. **Practical consequence: don't assume CM108 PTT is broken on a new
 * phone/cable/radio combination just because it failed on a different one --
 * test it fresh before falling back to VOX.**
 *
 * NOT EVERY CABLE HAS THIS AT ALL, and the record was wrong about which do.
 * `99-digirig.rules` claimed the K1 PRO has CM108 GPIO PTT; its HID report
 * descriptor has no output item, so there is no GPIO to drive and no pin
 * number can key it. Such a cable is VOX-only. connect() reports that as an
 * error rather than appearing to succeed, because a keying path that accepts
 * commands and transmits nothing is the single most expensive failure mode in
 * this project's record -- see FmAudioBridgeService's own fallback, which
 * says so on the status screen rather than going quiet.
 */
class Cm108Ptt(private val context: Context) {

    companion object {
        private const val TAG = "Cm108Ptt"
        private const val ACTION_USB_PERMISSION = "io.github.stress5094.customrnsbridge.CM108_USB_PERMISSION"

        const val VENDOR_ID = 0x0D8C   // C-Media Electronics -- 3468 decimal
        const val PRODUCT_ID = 0x0012  // matches device_filter.xml -- 18 decimal

        // Digirig wires PTT to GPIO3 (see tools/ptt_test.py's
        // CM108_DEFAULT_GPIO and this project's whole PTT history) -- bit 2
        // (0-indexed).
        const val DEFAULT_GPIO = 3

        // What `adb shell dumpsys usb` reported once, 2026-09-20, for this
        // device: class=3 (HID) at interface id 3. Kept only as the tie-break
        // when more than one interface claims to be HID -- see
        // findHidInterface for why it is no longer the primary lookup.
        private const val EXPECTED_HID_INTERFACE = 3

        // USB HID class-specific SET_REPORT request (HID 1.11 spec section 7.2.2).
        private const val REQUEST_SET_REPORT = 0x09
        private const val REPORT_TYPE_OUTPUT = 0x02
        private const val REQUEST_TYPE_HOST_TO_DEVICE_CLASS_INTERFACE =
            (UsbConstants.USB_DIR_OUT or UsbConstants.USB_TYPE_CLASS or 0x01) // 0x21
    }

    private val usbManager = context.getSystemService(Context.USB_SERVICE) as UsbManager
    private var connection: UsbDeviceConnection? = null
    private var iface: UsbInterface? = null
    private var receiverRegistered = false

    private val permissionReceiver = object : BroadcastReceiver() {
        override fun onReceive(ctx: Context, intent: Intent) {
            if (intent.action != ACTION_USB_PERMISSION) return
            val device: UsbDevice? = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                intent.getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
            } else {
                @Suppress("DEPRECATION")
                intent.getParcelableExtra(UsbManager.EXTRA_DEVICE)
            }
            val granted = intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)
            val cb = pendingCallback
            pendingCallback = null
            if (granted && device != null) {
                val opened = openAndClaim(device)
                if (opened) cb?.onReady() else cb?.onError("failed to open/claim the CM108 HID interface")
            } else {
                cb?.onError("USB permission denied for the Digirig/CM108 device")
            }
        }
    }

    interface Callback {
        fun onReady()
        fun onError(message: String)
    }

    private var pendingCallback: Callback? = null

    /** True once the HID interface is claimed and setGpio() can be expected
     * to reach the device. Not a claim that the cable's GPIO is wired to the
     * radio's PTT input -- nothing on this side can tell. */
    fun isReady(): Boolean = connection != null

    /** Find the CM108 device, request permission if needed, open + claim its
     * HID interface. `callback` fires exactly once, on whichever path
     * applies (already had permission, just granted, or an error). */
    fun connect(callback: Callback) {
        val device = usbManager.deviceList.values.firstOrNull {
            it.vendorId == VENDOR_ID && it.productId == PRODUCT_ID
        }
        if (device == null) {
            callback.onError(
                "no CM108/Digirig device found (vendor=0x%04X product=0x%04X)"
                    .format(VENDOR_ID, PRODUCT_ID))
            return
        }
        if (usbManager.hasPermission(device)) {
            if (openAndClaim(device)) callback.onReady()
            else callback.onError("failed to open/claim the CM108 HID interface")
            return
        }
        pendingCallback = callback
        if (!receiverRegistered) {
            val filter = IntentFilter(ACTION_USB_PERMISSION)
            context.registerReceiver(
                permissionReceiver, filter,
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                    Context.RECEIVER_NOT_EXPORTED else 0
            )
            receiverRegistered = true
        }
        val permissionIntent = PendingIntent.getBroadcast(
            context, 0, Intent(ACTION_USB_PERMISSION), PendingIntent.FLAG_MUTABLE
        )
        usbManager.requestPermission(device, permissionIntent)
    }

    /**
     * Which interface carries the HID report, BY CLASS rather than by its
     * index.
     *
     * The previous version of this looked for interface id 3 and nothing
     * else, on the strength of one `adb shell dumpsys usb` reading -- and its
     * own comment recorded that dumpsys then started throwing on this OS
     * build (descriptor parser exception) and could no longer re-check it. A
     * hardcoded index that cannot be verified, on a device family rather than
     * one device, is a wrong assumption waiting to key nothing silently; the
     * class is in the descriptor the OS has already parsed for us.
     *
     * The expected index is still preferred when several interfaces report
     * HID, so a device that does match the one reading taken on hardware
     * keeps behaving exactly as it did.
     */
    private fun findHidInterface(device: UsbDevice): UsbInterface? {
        val hid = (0 until device.interfaceCount)
            .map { device.getInterface(it) }
            .filter { it.interfaceClass == UsbConstants.USB_CLASS_HID }
        return hid.firstOrNull { it.id == EXPECTED_HID_INTERFACE } ?: hid.firstOrNull()
    }

    private fun openAndClaim(device: UsbDevice): Boolean {
        // Diagnostic dump, kept from 2026-09-20: log every interface's real
        // id/class/subclass/protocol/endpoint count, once, so a wrong
        // assumption about which interface actually carries the HID report
        // shows up in logcat rather than silently keying nothing. Still worth
        // having now the lookup is class-based: it is also the only record of
        // what a NEW cable's descriptor looks like.
        for (i in 0 until device.interfaceCount) {
            val f = device.getInterface(i)
            Log.i(TAG, "iface[$i]: id=${f.id} class=${f.interfaceClass} " +
                    "subclass=${f.interfaceSubclass} protocol=${f.interfaceProtocol} " +
                    "endpoints=${f.endpointCount} alt=${f.alternateSetting}")
        }
        val hid = findHidInterface(device) ?: run {
            Log.e(TAG, "no HID-class interface on $device -- this cable cannot " +
                    "be keyed by GPIO at any pin number")
            return false
        }
        val conn = usbManager.openDevice(device) ?: run {
            Log.e(TAG, "openDevice failed for $device")
            return false
        }
        if (!conn.claimInterface(hid, true)) {
            Log.e(TAG, "claimInterface failed for HID interface ${hid.id}")
            conn.close()
            return false
        }
        connection = conn
        iface = hid
        Log.i(TAG, "CM108 HID interface ${hid.id} claimed")
        return true
    }

    /** Raw SET_REPORT: iodata/iomask are full GPIO bytes (bit 0 = GPIO1,
     * ... bit 3 = GPIO4), matching tools/ptt_test.py's Cm108Ptt._report().
     * Not private, so a future caller can drive more than one GPIO without
     * this class growing a method per pin. */
    fun setGpio(iodata: Int, iomask: Int): Boolean {
        val conn = connection ?: return false
        val hidId = iface?.id ?: return false
        // report ID 0 ("no numbered reports") -> wValue's low byte is 0.
        //
        // EXPERIMENT, 2026-09-19 -- BOTH of this class's own documented
        // remedies for "keys nothing despite controlTransfer succeeding"
        // were tried on real hardware and both FAILED, more decisively than
        // the original:
        //   - 5-byte data stage (matching ptt_test.py's desktop encoding):
        //     REJECTED outright, controlTransfer itself failed (sent<0).
        //   - REPORT_TYPE_FEATURE (0x03) instead of OUTPUT, 4 bytes: ALSO
        //     rejected outright, same failure.
        // Back to the ORIGINAL 4-byte OUTPUT-report form below -- the only
        // one this device's descriptor actually accepts (controlTransfer
        // succeeds every time). On 2026-09-19's hardware, accepting the
        // command never produced a real transmission -- pointed at that
        // cable's physical PTT wiring, see the project bench notes, "the
        // OPPOSITE transmit-direction asymmetry" section. **On 2026-09-20's
        // hardware (a different phone/cable/radio pairing), this exact
        // 4-byte form DOES key the radio and carries real, byte-perfect
        // traffic -- see the class doc comment above.** The format itself was
        // never the problem; whether a given cable's GPIO3 is actually wired
        // to its radio's PTT input varies by unit.
        val wValue = (REPORT_TYPE_OUTPUT shl 8) or 0x00
        val data = byteArrayOf(0, iodata.toByte(), iomask.toByte(), 0)
        val sent = conn.controlTransfer(
            REQUEST_TYPE_HOST_TO_DEVICE_CLASS_INTERFACE, REQUEST_SET_REPORT,
            wValue, hidId, data, data.size, 1000
        )
        if (sent < 0) {
            Log.e(TAG, "controlTransfer (SET_REPORT) failed, gpio data=$iodata mask=$iomask")
            return false
        }
        // DEBUG, not INFO as this once was: it fires twice per burst, and the
        // thing it reports -- "the command was accepted" -- is precisely the
        // one that was measured NOT to imply a transmission. Still logged,
        // because "accepted every time and keyed nothing" is the diagnosis it
        // exists to support, and that needs the line to be there.
        Log.d(TAG, "controlTransfer OK, sent=$sent bytes, gpio data=$iodata " +
                "mask=$iomask, wValue=$wValue")
        return true
    }

    fun key(gpio: Int = DEFAULT_GPIO): Boolean {
        val bit = 1 shl (gpio - 1)
        return setGpio(bit, bit)
    }

    fun unkey(gpio: Int = DEFAULT_GPIO): Boolean {
        val bit = 1 shl (gpio - 1)
        return setGpio(0, bit)
    }

    fun close() {
        try { unkey() } catch (e: Exception) { /* best effort */ }
        try { iface?.let { connection?.releaseInterface(it) } } catch (e: Exception) { /* ignore */ }
        connection?.close()
        connection = null
        iface = null
        if (receiverRegistered) {
            try { context.unregisterReceiver(permissionReceiver) } catch (e: Exception) { /* not registered */ }
            receiverRegistered = false
        }
    }
}
