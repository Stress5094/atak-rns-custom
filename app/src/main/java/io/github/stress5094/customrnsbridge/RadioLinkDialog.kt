package io.github.stress5094.customrnsbridge

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import java.util.concurrent.Executors

/**
 * Choosing how the phone reaches the radio, and — for Bluetooth — which
 * radio.
 *
 * Its own screen rather than a section of the radio dialog for one reason:
 * the radio dialog is built from settings read out of Python, so it is
 * unavailable exactly when the link is broken. This one must work when
 * nothing else does, so it asks Android for everything it shows and asks
 * Python only for the one action that genuinely needs the radio.
 *
 * ANDROID HIDES BLE DEVICES FROM ITS OWN BLUETOOTH SETTINGS SCREEN, which is
 * why there is a scan and a pairing flow in here at all rather than a line of
 * documentation pointing at the system settings. Sideband carries the same
 * screen for the same reason.
 */
class RadioLinkDialog(
    private val activity: Activity,
    private val onSaved: (restartNeeded: Boolean) -> Unit,
) {

    companion object {
        private const val TAG = "RadioLinkDialog"
        private const val SCAN_MS = 10_000L

        /** Everything the picker needs; the bridge service itself needs only CONNECT. */
        fun requiredPermissions(): Array<String> =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                arrayOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
            } else {
                // Before Android 12 a BLE scan was treated as a location
                // capability, and refusing this made scans return nothing at
                // all -- silently, with no error.
                arrayOf(Manifest.permission.ACCESS_FINE_LOCATION)
            }

        fun hasPermissions(context: Context): Boolean = requiredPermissions().all {
            ContextCompat.checkSelfPermission(context, it) == PackageManager.PERMISSION_GRANTED
        }

    }

    private val COLOR_IDLE by lazy { Ui.res(activity, R.color.status_idle) }
    private val COLOR_UP by lazy { Ui.res(activity, R.color.status_good) }
    private val COLOR_WARN by lazy { Ui.res(activity, R.color.status_warn) }

    private val adapter: BluetoothAdapter? =
        (activity.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter

    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()

    private var transport = RadioLink.transport(activity)
    private var chosenAddress: String? = RadioLink.bleAddress(activity)
    private var chosenName: String? = RadioLink.bleName(activity)
    private var fmBackup = RadioLink.fmBackupEnabled(activity)
    private lateinit var fmBackupCheckbox: CheckBox

    // The LoRa board runs Meshtastic firmware rather than RNode. Offered over
    // USB and Bluetooth, not FM (see RadioLink.radioFirmware()). Initialised
    // from the STORED choice, not the effective one, so a value saved before a
    // trip through FM is still there when LoRa comes back.
    private var meshtastic = RadioLink.storedRadioFirmware(activity) == RadioLink.RADIO_MESHTASTIC
    private lateinit var meshtasticCheckbox: CheckBox

    // The FM tone placement. Shown whenever FM could carry traffic at all (as
    // the transport itself, or as backup), hidden otherwise -- on a LoRa-only
    // node it is a setting with no effect, and this dialog already hides the
    // controls that do not apply.
    private var toneProfile = RadioLink.fmToneProfile(activity)
    private lateinit var toneProfileSection: View

    // The FM cable has no PTT pin (K1 PRO class): VOX keys the radio. Lives in
    // the FM section, so it shows and hides with the tone picker.
    private var voxCable = RadioLink.fmVoxCable(activity)
    private lateinit var voxSection: View

    // The custom FM settings (fm_custom JSON), edited in FmCustomDialog and
    // saved with the rest of this dialog. "" until first set up.
    private var customSpec = RadioLink.fmCustomSpec(activity)
    private lateinit var customLabel: TextView
    private lateinit var customEditButton: View

    // The Debug tab's "Advanced features" switch: off hides the WiFi mesh and
    // the whole FM tone section (placement, custom FM, no-PTT cable, test
    // mode) below. Hidden settings keep their
    // values -- this only decides what is shown.
    private val advanced = RadioLink.advancedEnabled(activity)

    // Test mode: receive only. Read per burst by the audio service, so unlike
    // the settings around it this one does NOT need a restart and is left out
    // of the `changed` comparison on purpose.
    private var txOff = RadioLink.fmTxOff(activity)

    // The WiFi link (802.11s BLOS mesh) -- independent of transport/FM backup
    // entirely, so it lives outside the RadioGroup below rather than as a
    // fourth option in it. See RadioLink.wifiEnabled()'s own comment.
    private var wifiEnabled = RadioLink.wifiEnabled(activity)
    private var wifiIfname = RadioLink.wifiIfname(activity)
    private lateinit var wifiIfnameField: EditText

    /** Address -> name, paired radios and anything a scan has turned up. */
    private val found = LinkedHashMap<String, String>()

    /** The addresses in [found] known to be Meshtastic boards: they advertise
     * the Meshtastic service, or Android has it cached for a paired one. The
     * picker shows these or the others, matching the firmware checkbox. */
    private val meshFound = HashSet<String>()
    private var scanning = false

    private lateinit var deviceBox: LinearLayout
    private lateinit var statusLine: TextView
    private lateinit var scanButton: Button
    private lateinit var screenButton: Button
    private var screenOn = false
    private lateinit var bluetoothSection: View
    private var dialog: AlertDialog? = null

    /** Refreshes the list the moment a pairing completes, so it stops lying. */
    private val bondReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == BluetoothDevice.ACTION_BOND_STATE_CHANGED) renderDevices()
        }
    }

    fun show() {
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(12), dp(20), 0)
        }

        // Every RadioButton needs its own explicit id. RadioGroup's
        // exclusivity (unchecking the previous selection when a new one is
        // tapped) looks up the previously-checked child by id via
        // findViewById() -- left at the default NO_ID (-1, what a
        // programmatically-created View has until something sets it),
        // View.findViewById(-1) short-circuits to null before it even
        // searches, so the lookup silently fails and the old button is
        // never unchecked. Found by hand while adding the third (FM) button:
        // tapping it left BOTH it and "USB cable" showing checked.
        val group = RadioGroup(activity).apply { orientation = RadioGroup.VERTICAL }
        val usbButton = RadioButton(activity).apply {
            id = View.generateViewId()
            text = "USB cable"
            isChecked = transport == RadioLink.TRANSPORT_USB
        }
        val bleButton = RadioButton(activity).apply {
            id = View.generateViewId()
            text = "Bluetooth"
            isChecked = transport == RadioLink.TRANSPORT_BLE
        }
        // The FM/codec2 audio link instead of a KISS-speaking radio -- see
        // FmAudioBridgeService/fm_link.py. No pairing or scanning of its own,
        // so it needs none of the Bluetooth section below; the USB cable
        // carries the radio's audio the same way it already carries an
        // RNode's serial.
        val fmButton = RadioButton(activity).apply {
            id = View.generateViewId()
            text = "FM audio (USB)"
            isChecked = transport == RadioLink.TRANSPORT_FM
        }
        group.addView(usbButton)
        group.addView(bleButton)
        group.addView(fmButton)
        group.setOnCheckedChangeListener { _, checked ->
            transport = when (checked) {
                bleButton.id -> RadioLink.TRANSPORT_BLE
                fmButton.id -> RadioLink.TRANSPORT_FM
                else -> RadioLink.TRANSPORT_USB
            }
            bluetoothSection.visibility =
                if (transport == RadioLink.TRANSPORT_BLE) View.VISIBLE else View.GONE
            meshtasticCheckbox.visibility =
                if (transport == RadioLink.TRANSPORT_FM) View.GONE else View.VISIBLE
            // Only meaningful alongside USB/BLE -- FM chosen as the transport
            // itself has no LoRa to back up.
            fmBackupCheckbox.visibility =
                if (transport == RadioLink.TRANSPORT_FM) View.GONE else View.VISIBLE
            toneProfileSection.visibility =
                if (fmInUse() && advanced) View.VISIBLE else View.GONE
        }
        body.addView(group)

        // Which firmware is on the board behind the USB cable. A checkbox and
        // not a fourth radio button: it is not another way of reaching the
        // radio, it is what the radio IS, and it only exists for USB.
        meshtasticCheckbox = com.google.android.material.checkbox.MaterialCheckBox(activity).apply {
            text = "The board runs Meshtastic firmware (not RNode)"
            isChecked = meshtastic
            visibility = if (transport == RadioLink.TRANSPORT_FM) View.GONE else View.VISIBLE
            setOnCheckedChangeListener { _, checked ->
                meshtastic = checked
                // A radio chosen as the other kind is not a choice any more:
                // connecting to an RNode as a Meshtastic board (or back) finds
                // the wrong service and retries forever.
                val chosen = chosenAddress
                if (chosen != null && chosen in found && (chosen in meshFound) != checked) {
                    chosenAddress = null
                    chosenName = null
                }
                renderDevices()
            }
        }
        body.addView(meshtasticCheckbox)

        // LoRa-primary/FM-backup routing: FM stays attached and keeps
        // listening even while LoRa is carrying traffic, and the bridge fails
        // over to it automatically if LoRa goes unhealthy. See
        // RadioLink.fmBackupEnabled() and rns_bridge.py's
        // set_fm_backup_enabled().
        fmBackupCheckbox = com.google.android.material.checkbox.MaterialCheckBox(activity).apply {
            text = "Use FM as automatic backup"
            isChecked = fmBackup
            visibility = if (transport == RadioLink.TRANSPORT_FM) View.GONE else View.VISIBLE
            setOnCheckedChangeListener { _, checked ->
                fmBackup = checked
                // Turning backup on is the other way FM starts carrying
                // traffic, so it reveals the tone picker too.
                toneProfileSection.visibility =
                    if (fmInUse() && advanced) View.VISIBLE else View.GONE
            }
        }
        body.addView(fmBackupCheckbox)

        toneProfileSection = buildToneProfileSection().apply {
            visibility = if (fmInUse() && advanced) View.VISIBLE else View.GONE
        }
        body.addView(toneProfileSection)

        // Not a fourth RadioButton above: WiFi is not another way of reaching
        // the LoRa radio, it is its own separate link that runs alongside
        // whichever of USB/BLE/FM is selected -- the same relationship FM
        // backup has to LoRa, wired into rns_bridge.py's N-way failover (LoRa
        // primary, then FM, then WiFi last) rather than exclusive with any of
        // them. WiFi being last means enabling this checkbox cannot preempt a
        // working LoRa/FM link; it only becomes active when nothing else is.
        val wifiCheckbox = com.google.android.material.checkbox.MaterialCheckBox(activity).apply {
            text = "Use a WiFi mesh as an additional link"
            isChecked = wifiEnabled
            visibility = if (advanced) View.VISIBLE else View.GONE
            setOnCheckedChangeListener { _, checked ->
                wifiEnabled = checked
                wifiIfnameField.visibility = if (checked) View.VISIBLE else View.GONE
            }
        }
        body.addView(wifiCheckbox)

        wifiIfnameField = EditText(activity).apply {
            hint = "Mesh interface name, e.g. wlan0 -- blank uses every interface"
            textSize = 12f
            setText(wifiIfname)
            visibility = if (advanced && wifiEnabled) View.VISIBLE else View.GONE
        }
        body.addView(wifiIfnameField)

        bluetoothSection = buildBluetoothSection().apply {
            visibility = if (transport == RadioLink.TRANSPORT_BLE) View.VISIBLE else View.GONE
        }
        body.addView(bluetoothSection)

        body.addView(buildRadioBluetoothControls())

        dialog = MaterialAlertDialogBuilder(activity)
            .setTitle("Radio link")
            .setView(ScrollView(activity).apply { addView(body) })
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Save", null) // wired below so it can validate
            .create()

        dialog?.setOnShowListener {
            dialog?.getButton(AlertDialog.BUTTON_POSITIVE)?.setOnClickListener { save() }
        }
        dialog?.setOnDismissListener {
            stopScan()
            unregisterPinReceiver()
            try { activity.unregisterReceiver(bondReceiver) } catch (e: Exception) { /* never registered */ }
            executor.shutdownNow()
        }

        ContextCompat.registerReceiver(
            activity,
            bondReceiver,
            IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED),
            ContextCompat.RECEIVER_NOT_EXPORTED
        )
        loadPairedDevices()
        renderDevices()
        dialog?.show()
    }

    /** Whether FM would carry traffic at all with the current selections. The
     * dialog's own version of RadioLink.fmInUse(), reading the UNSAVED choices
     * -- the picker has to appear the moment FM is selected, not after a
     * save. */
    private fun fmInUse(): Boolean =
        transport == RadioLink.TRANSPORT_FM || fmBackup

    /**
     * The FM tone placement picker.
     *
     * CARRIES A WARNING LINE rather than just a list, because this is the only
     * setting in this dialog that can take a node off the air with no symptom
     * an operator can read off their own phone: two nodes on different
     * profiles recover nothing from each other, and neither one can be told
     * why. Everything else here fails visibly (no such radio, no paired
     * device, a dead USB cable).
     */
    private fun buildToneProfileSection(): View {
        val box = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        box.addView(TextView(activity).apply {
            text = "FM tone placement"
            textSize = 12f
        })
        box.addView(TextView(activity).apply {
            text = "Every node on the FM net must use the same settings: " +
                "compare the net code on the status screen before splitting " +
                "up. A different rate, tone count or code is total silence, " +
                "and the link cannot report it."
            textSize = 12f
        })

        // Same explicit-id requirement as the transport group above -- see its
        // comment: RadioGroup unchecks the previous selection by
        // findViewById(), which silently no-ops at the default NO_ID.
        val group = RadioGroup(activity).apply { orientation = RadioGroup.VERTICAL }
        val names = RadioLink.TONE_PROFILES + RadioLink.PROFILE_CUSTOM
        val buttons = names.map { name ->
            RadioButton(activity).apply {
                id = View.generateViewId()
                text = if (name == RadioLink.PROFILE_CUSTOM)
                    "Custom - your own tones, rate and keying"
                else RadioLink.toneProfileLabel(name)
                textSize = 12f
                isChecked = name == toneProfile
                // Custom is advanced -- but stays listed while it is the one
                // in use, so a hidden setting is never the selected one.
                if (name == RadioLink.PROFILE_CUSTOM && !advanced &&
                    toneProfile != RadioLink.PROFILE_CUSTOM) visibility = View.GONE
                group.addView(this)
            }
        }
        group.setOnCheckedChangeListener { _, checked ->
            val index = buttons.indexOfFirst { it.id == checked }
            if (index < 0) return@setOnCheckedChangeListener
            toneProfile = names[index]
            showCustomState()
            // Nothing to run on yet: open the editor straight away.
            if (toneProfile == RadioLink.PROFILE_CUSTOM && customSpec.isBlank()) {
                openCustomEditor()
            }
        }
        box.addView(group)

        customLabel = TextView(activity).apply {
            textSize = 12f
            setPadding(dp(32), 0, 0, 0)
        }
        box.addView(customLabel)
        customEditButton = Ui.outlined(activity, "Edit custom settings\u2026").apply {
            setOnClickListener { openCustomEditor() }
        }
        box.addView(customEditButton)

        // Custom settings carry their own keying (chosen in the editor), so
        // this checkbox applies to the presets only and hides for Custom.
        voxSection = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            addView(com.google.android.material.checkbox.MaterialCheckBox(activity).apply {
                text = "Cable has no PTT pin (K1 PRO / VOX radio)"
                isChecked = voxCable
                setOnCheckedChangeListener { _, checked -> voxCable = checked }
            })
            addView(TextView(activity).apply {
                text = "Leave OFF for a Digirig Lite. ON sends a longer lead-in " +
                    "tone (about 1 s) so the radio's VOX has switched on before " +
                    "the data starts; bursts run about 0.9 s longer."
                textSize = 12f
            })
        }
        box.addView(voxSection)
        showCustomState()

        if (advanced) box.addView(com.google.android.material.checkbox.MaterialCheckBox(activity).apply {
            text = "Test mode: receive only, do not transmit"
            isChecked = txOff
            setOnCheckedChangeListener { _, checked -> txOff = checked }
        })
        if (advanced) box.addView(TextView(activity).apply {
            text = "For measuring this link from another radio. ON stops EVERY " +
                "burst from this phone -- beacons, messages, position resends " +
                "-- so nobody can hear it and it will drop off other nodes' " +
                "lists. A half-duplex radio hears nothing while it transmits, " +
                "so this removes that as a cause of lost bursts. Takes effect " +
                "at once. Turn it OFF when the test is done."
            textSize = 12f
        })
        return box
    }

    /** The custom line under the picker: its net code once set up, and
     * whether the VOX checkbox applies. */
    private fun showCustomState() {
        val custom = toneProfile == RadioLink.PROFILE_CUSTOM
        voxSection.visibility = if (custom || !advanced) View.GONE else View.VISIBLE
        val showCustom = advanced || custom
        customLabel.visibility = if (showCustom) View.VISIBLE else View.GONE
        customEditButton.visibility = if (showCustom) View.VISIBLE else View.GONE
        customLabel.text = when {
            customSpec.isBlank() -> "Custom: not set up yet"
            else -> "Custom: " + customNetCode()
        }
        customLabel.setTextColor(if (custom) COLOR_WARN else COLOR_IDLE)
    }

    private fun customNetCode(): String = try {
        if (!com.chaquo.python.Python.isStarted()) {
            com.chaquo.python.Python.start(
                com.chaquo.python.android.AndroidPlatform(activity.applicationContext))
        }
        val d = org.json.JSONObject(com.chaquo.python.Python.getInstance()
            .getModule("fm_custom").callAttr("describe_json", customSpec, false).toString())
        if (d.optBoolean("ok")) d.optString("net_code")
        else "UNUSABLE - " + d.optString("error")
    } catch (e: Exception) {
        Log.w(TAG, "could not describe the custom FM settings", e)
        "(could not read)"
    }

    private fun openCustomEditor() {
        FmCustomDialog(activity, customSpec) { spec ->
            customSpec = spec
            showCustomState()
        }.show()
    }

    private fun buildBluetoothSection(): View {
        val box = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        box.addView(TextView(activity).apply {
            text = "Which radio"
            textSize = 12f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(COLOR_IDLE)
        })
        deviceBox = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        box.addView(deviceBox)

        statusLine = TextView(activity).apply {
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(6), 0, dp(2))
        }
        box.addView(statusLine)

        scanButton = Button(activity).apply {
            text = "Scan for radios"
            textSize = 12f
            setOnClickListener { if (scanning) stopScan() else startScan() }
        }
        box.addView(scanButton)
        return box
    }

    /**
     * The radio ships with Bluetooth switched off, and the command to switch
     * it on has to travel down a wire. That is a genuine chicken and egg, and
     * the usual answer is a laptop and rnodeconf — but this app already owns
     * the byte pipe to the radio, so it is one short command over whatever
     * link is currently up.
     */
    private fun buildRadioBluetoothControls(): View {
        val box = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(18), 0, dp(4))
        }
        box.addView(TextView(activity).apply {
            text = "On the radio itself"
            textSize = 12f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(COLOR_IDLE)
        })
        box.addView(TextView(activity).apply {
            text = "All of these need a working link right now."
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(2), 0, dp(6))
        })

        // First, because it answers the question you have before any of the
        // others: which of the radios on the table is the one this phone is
        // talking to. The cable used to answer that by existing.
        box.addView(Button(activity).apply {
            text = "Identify — flash this phone's radio"
            textSize = 12f
            setOnClickListener { identifyRadio() }
        })

        // The radio's screen is the radio's by default. Taking it over is a
        // choice, and reversible from the same button.
        screenButton = Button(activity).apply {
            text = "Radio screen…"
            textSize = 12f
            setOnClickListener { toggleRadioScreen() }
        }
        box.addView(screenButton)
        refreshScreenButton()

        box.addView(TextView(activity).apply {
            text = "Bluetooth ships switched off. Turning it on is once per radio — " +
                    "normally over the USB cable — and the setting is stored in the " +
                    "radio itself."
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(10), 0, dp(6))
        })

        val row = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        fun cell(button: Button) = button.apply {
            textSize = 12f
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                .apply { marginEnd = dp(6) }
        }
        row.addView(cell(Button(activity).apply {
            text = "Turn on"
            setOnClickListener { radioBluetooth("on") }
        }))
        row.addView(cell(Button(activity).apply {
            text = "Pairing mode"
            setOnClickListener { radioBluetooth("pair") }
        }))
        box.addView(row)
        return box
    }

    /** Reads the current setting out of Python so the button states a fact. */
    private fun refreshScreenButton() {
        executor.execute {
            val on = PythonRnsService.call("get_radio_display") == "on"
            handler.post {
                screenOn = on
                screenButton.text =
                    if (on) "Give the screen back to the radio"
                    else "Show this node on the radio's screen"
            }
        }
    }

    private fun toggleRadioScreen() {
        val want = !screenOn
        executor.execute {
            val reply = PythonRnsService.call("set_radio_display", if (want) "on" else "off")
            handler.post {
                Toast.makeText(
                    activity,
                    reply ?: "The bridge is not running, so the radio cannot be told",
                    Toast.LENGTH_LONG
                ).show()
                refreshScreenButton()
            }
        }
    }

    private fun identifyRadio() {
        Toast.makeText(activity, "Asking the radio to flash…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val reply = PythonRnsService.call("identify_radio")
            handler.post {
                Toast.makeText(
                    activity,
                    reply ?: "The bridge is not running, so there is no link to the radio",
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }

    private fun radioBluetooth(mode: String) {
        Toast.makeText(activity, "Sending…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val reply = PythonRnsService.call("enable_rnode_bluetooth", mode)
            handler.post {
                // A dialog, not a toast: this reply can contain the pairing
                // code, and a code that vanishes after three seconds while
                // Android is waiting for you to type it is no better than no
                // code at all.
                MaterialAlertDialogBuilder(activity)
                    .setTitle("Radio Bluetooth")
                    .setMessage(reply ?: "The bridge is not running, so there is no link to the radio")
                    .setPositiveButton("OK", null)
                    .show()
            }
        }
    }

    @SuppressLint("MissingPermission") // gated on hasPermissions() before the dialog opens
    private fun loadPairedDevices() {
        val adapter = this.adapter ?: return
        if (!hasPermissions(activity)) return
        adapter.bondedDevices.orEmpty().forEach { device ->
            val name = device.name ?: device.address
            when {
                device.uuids?.any { it.uuid == BleSerialBridgeService.MESH_SERVICE } == true ||
                        // Android does not always cache an LE device's services,
                        // so a paired board under its stock name counts too.
                        device.name?.startsWith("Meshtastic") == true -> {
                    found[device.address] = name
                    meshFound.add(device.address)
                }
                device.name?.startsWith(BleSerialBridgeService.RNODE_NAME_PREFIX) == true ->
                    found[device.address] = name
            }
        }
        // The saved choice is always listed, so it can be seen and changed even
        // when nothing above recognised it -- a board renamed by its owner.
        val chosen = chosenAddress
        if (chosen != null && chosen !in found) {
            found[chosen] = chosenName ?: chosen
            if (meshtastic) meshFound.add(chosen)
        }
    }

    /** The radios in [found] of the kind the firmware checkbox says. */
    private fun shownDevices(): Map<String, String> =
        found.filterKeys { (it in meshFound) == meshtastic }

    @SuppressLint("MissingPermission")
    private fun renderDevices() {
        if (!::deviceBox.isInitialized) return
        deviceBox.removeAllViews()

        // The zero-configuration case, kept as an explicit choice rather than
        // an implicit one: a phone that has only ever been paired with one
        // radio should not need this screen at all, and this row says so.
        // RNode only -- see BleSerialBridgeService.chooseDevice().
        if (!meshtastic) deviceBox.addView(deviceRow(
            label = "Use the only paired radio",
            detail = "Fine until a second RNode is paired with this phone",
            selected = chosenAddress == null,
            bonded = true,
        ) {
            chosenAddress = null
            chosenName = null
            renderDevices()
        })

        val shown = shownDevices()
        if (shown.isEmpty()) {
            deviceBox.addView(TextView(activity).apply {
                text = if (meshtastic)
                    "No Meshtastic boards found yet. Make sure the board's Bluetooth is " +
                        "on and no other phone or app is connected to it, then scan."
                else "No radios paired yet. Switch the radio's Bluetooth on, then scan."
                textSize = 12f
                setTextColor(COLOR_WARN)
                setPadding(0, dp(6), 0, dp(2))
            })
            return
        }

        shown.forEach { (address, name) ->
            val device = try { adapter?.getRemoteDevice(address) } catch (e: Exception) { null }
            val bonded = device?.bondState == BluetoothDevice.BOND_BONDED
            deviceBox.addView(deviceRow(
                label = name,
                detail = if (bonded) "$address · paired" else "$address · tap to pair",
                selected = address == chosenAddress,
                bonded = bonded,
            ) {
                chosenAddress = address
                chosenName = name
                if (!bonded && device != null) pair(device)
                renderDevices()
            })
        }
    }

    private fun deviceRow(
        label: String,
        detail: String,
        selected: Boolean,
        bonded: Boolean,
        onClick: () -> Unit,
    ): View {
        val box = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(10), dp(8), dp(10), dp(8))
            isClickable = true
            setOnClickListener { onClick() }
        }
        box.addView(TextView(activity).apply {
            // The marker is a character, not just a colour: this screen gets
            // read outdoors.
            text = (if (selected) "● " else "○ ") + label
            textSize = 14f
            setTypeface(typeface, if (selected) Typeface.BOLD else Typeface.NORMAL)
        })
        box.addView(TextView(activity).apply {
            text = detail
            textSize = 12f
            setTextColor(if (bonded) COLOR_IDLE else COLOR_WARN)
            setPadding(dp(16), 0, 0, 0)
        })
        return box
    }

    /**
     * Pairing is deliberately two taps, and the code comes first.
     *
     * Android asks for a six-digit code that only the radio knows. It shows it
     * on its own screen, but only while it is in pairing mode, and it also
     * reports it back over the link — which is the copy this app can read and
     * the operator cannot. Starting the bond before showing it puts the system
     * prompt on top of the one thing you need in order to answer it.
     */
    @SuppressLint("MissingPermission")
    private fun pair(device: BluetoothDevice) {
        if (meshtastic) {
            pairMeshtastic(device)
            return
        }
        executor.execute {
            val pin = PythonRnsService.call("get_bt_pairing_pin").orEmpty()
            handler.post {
                val message = if (pin.isNotEmpty()) {
                    "Pairing code: $pin\n\nEnter this when Android asks."
                } else {
                    "Android will ask for a six-digit code that only the radio " +
                            "knows.\n\nIf you do not have it: tap Pairing mode " +
                            "below with the radio connected, or hold the radio's " +
                            "own button for five seconds and read the code off " +
                            "its screen."
                }
                MaterialAlertDialogBuilder(activity)
                    .setTitle("Pair with ${device.name ?: device.address}")
                    .setMessage(message)
                    .setNegativeButton("Not now", null)
                    .setPositiveButton("Pair") { _, _ ->
                        if (!device.createBond()) {
                            Toast.makeText(
                                activity, "Android refused to start pairing", Toast.LENGTH_LONG
                            ).show()
                        }
                    }
                    .show()
            }
        }
    }

    /**
     * A board with no screen cannot show a PIN, so the app supplies it: the
     * managed profile ("Use the app's settings") gives each board its own PIN,
     * derived from the mesh passphrase and the board's node number
     * (mt_config.derive_bt_pin). The node number is in the board's Bluetooth
     * address, so every phone with the passphrase can work out the PIN here.
     *
     * The PIN is also typed into Android's prompt for you when Android allows
     * it (setPin on the pairing request). Whether it does depends on the phone,
     * so the PIN is always shown as well.
     */
    @SuppressLint("MissingPermission")
    private fun pairMeshtastic(device: BluetoothDevice) {
        executor.execute {
            val pin = PythonRnsService.call(
                "get_mesh_bt_pin", device.address, device.name ?: ""
            ).orEmpty()
            handler.post {
                val message = if (pin.isNotEmpty()) {
                    "PIN: $pin\n\nThis is the PIN this board gets from \"Use " +
                        "the app's settings\" (it comes from the mesh passphrase " +
                        "and the board's own number, so each board has a " +
                        "different one). The app will try to enter it for you; if " +
                        "Android asks, type it.\n\nA board that has never had " +
                        "the app's settings applied still has the factory PIN, " +
                        "123456 (or, if it has a screen, the one it shows). " +
                        "Plug it in by USB once and apply the settings to " +
                        "switch it over.\n\nA board talks to one phone or app " +
                        "at a time: disconnect the Meshtastic app first."
                } else {
                    "There is no mesh passphrase yet, so the app has no PIN to " +
                        "offer. A board never set up by this app uses 123456, or " +
                        "the PIN on its screen if it has one."
                }
                MaterialAlertDialogBuilder(activity)
                    .setTitle("Pair with ${device.name ?: device.address}")
                    .setMessage(message)
                    .setNegativeButton("Not now", null)
                    .setPositiveButton("Pair") { _, _ ->
                        if (pin.isNotEmpty()) autoEnterPin(device, pin)
                        if (!device.createBond()) {
                            Toast.makeText(
                                activity, "Android refused to start pairing", Toast.LENGTH_LONG
                            ).show()
                        }
                    }
                    .show()
            }
        }
    }

    private var pinReceiver: BroadcastReceiver? = null

    /** Answers Android's PIN prompt for this one device, once, if allowed. */
    private fun autoEnterPin(device: BluetoothDevice, pin: String) {
        unregisterPinReceiver()
        val receiver = object : BroadcastReceiver() {
            @SuppressLint("MissingPermission")
            override fun onReceive(context: Context, intent: Intent) {
                if (intent.action != BluetoothDevice.ACTION_PAIRING_REQUEST) return
                val asked: BluetoothDevice? =
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                        intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java)
                    else @Suppress("DEPRECATION") intent.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE)
                if (asked?.address != device.address) return
                val variant = intent.getIntExtra(BluetoothDevice.EXTRA_PAIRING_VARIANT, -1)
                val entered = try {
                    // 0 is PAIRING_VARIANT_PIN; 1 (passkey) is hidden API but
                    // takes the same digits through the same call.
                    (variant == BluetoothDevice.PAIRING_VARIANT_PIN || variant == 1) &&
                            device.setPin(pin.toByteArray())
                } catch (e: Exception) {
                    Log.w(TAG, "Could not enter the PIN automatically", e)
                    false
                }
                Log.i(TAG, "Pairing request variant=$variant, PIN entered automatically: $entered")
                if (entered && isOrderedBroadcast) abortBroadcast()
                unregisterPinReceiver()
            }
        }
        val filter = IntentFilter(BluetoothDevice.ACTION_PAIRING_REQUEST).apply {
            priority = IntentFilter.SYSTEM_HIGH_PRIORITY - 1
        }
        ContextCompat.registerReceiver(activity, receiver, filter, ContextCompat.RECEIVER_EXPORTED)
        pinReceiver = receiver
        // Not left listening: a pairing that never starts must not leave a
        // receiver answering some later request.
        handler.postDelayed({ unregisterPinReceiver() }, 60_000L)
    }

    private fun unregisterPinReceiver() {
        val r = pinReceiver ?: return
        pinReceiver = null
        try { activity.unregisterReceiver(r) } catch (e: Exception) { /* already gone */ }
    }

    @SuppressLint("MissingPermission")
    private fun startScan() {
        val adapter = this.adapter
        if (adapter == null) {
            statusLine.text = "This phone has no Bluetooth"
            return
        }
        if (!adapter.isEnabled) {
            statusLine.text = "Bluetooth is switched off on this phone"
            return
        }
        val scanner = adapter.bluetoothLeScanner
        if (scanner == null) {
            statusLine.text = "Bluetooth scanning is unavailable"
            return
        }
        // Deliberately unfiltered. Filtering on the serial service UUID would
        // be tidier, but it only matches radios that advertise that UUID, and
        // whether they do depends on the firmware build -- a filter that
        // silently finds nothing is worse than a short noisy scan.
        scanner.startScan(
            null,
            ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build(),
            scanCallback
        )
        scanning = true
        scanButton.text = "Stop scanning"
        statusLine.text = "Scanning…"
        statusLine.setTextColor(COLOR_UP)
        handler.postDelayed({ stopScan() }, SCAN_MS)
    }

    @SuppressLint("MissingPermission")
    private fun stopScan() {
        if (!scanning) return
        scanning = false
        try {
            adapter?.bluetoothLeScanner?.stopScan(scanCallback)
        } catch (e: Exception) {
            Log.w(TAG, "Could not stop the scan", e)
        }
        handler.removeCallbacksAndMessages(null)
        scanButton.text = "Scan for radios"
        statusLine.setTextColor(COLOR_IDLE)
        statusLine.text =
            if (shownDevices().isEmpty()) "No radios found. Is the radio's Bluetooth switched on?"
            else "${shownDevices().size} radio(s) known"
    }

    private val scanCallback = object : ScanCallback() {
        @SuppressLint("MissingPermission")
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val record = result.scanRecord
            val address = result.device.address
            val advertisesMesh =
                record?.serviceUuids?.any { it.uuid == BleSerialBridgeService.MESH_SERVICE } == true
            val name = record?.deviceName ?: result.device.name
                ?: (if (advertisesMesh) address else return)
            val advertisesUart =
                record?.serviceUuids?.any { it.uuid == BleSerialBridgeService.UART_SERVICE } == true
            if (!advertisesMesh && !name.startsWith(BleSerialBridgeService.RNODE_NAME_PREFIX) &&
                !advertisesUart) return

            // Posted, so the maps are only ever touched on the main thread.
            handler.post {
                val isNew = found.put(address, name) == null
                if (advertisesMesh) meshFound.add(address)
                if (isNew) {
                    renderDevices()
                    statusLine.text = "Scanning… ${shownDevices().size} found"
                }
            }
        }

        override fun onScanFailed(errorCode: Int) {
            handler.post {
                scanning = false
                scanButton.text = "Scan for radios"
                statusLine.text = "Scan failed (code $errorCode)"
                statusLine.setTextColor(COLOR_WARN)
            }
        }
    }

    private fun save() {
        val newWifiIfname = wifiIfnameField.text.toString().trim()

        // A SETTING ONLY COUNTS AS CHANGED WHEN IT WOULD ACTUALLY TAKE EFFECT.
        // The FM backup toggle is compared through
        // RadioLink.fmBackupEnabled() rather than against the raw checkbox
        // state, because that function already forces false whenever
        // transport == TRANSPORT_FM -- comparing against the raw field would
        // flag a change that setFmBackupEnabled() below would not actually
        // produce (e.g. switching straight from "USB + backup on" to "FM").
        // The tone profile is compared only while FM is going to run, for the
        // same class of reason: picking a placement on a LoRa-only node
        // changes nothing that is carrying traffic. Both traps apply to any
        // per-link setting added here later; see RadioLink.pendingRestart().
        val changed = transport != RadioLink.transport(activity) ||
                chosenAddress != RadioLink.bleAddress(activity) ||
                (fmBackup && transport != RadioLink.TRANSPORT_FM) != RadioLink.fmBackupEnabled(activity) ||
                wifiEnabled != RadioLink.wifiEnabled(activity) ||
                // Compared as it would take effect (not on FM), the same trap
                // the FM backup toggle documents above.
                ((meshtastic && transport != RadioLink.TRANSPORT_FM) !=
                        RadioLink.isMeshtastic(activity)) ||
                (wifiEnabled && newWifiIfname != RadioLink.wifiIfname(activity)) ||
                (fmInUse() && toneProfile != RadioLink.fmToneProfile(activity)) ||
                (fmInUse() && voxCable != RadioLink.fmVoxCable(activity)) ||
                (fmInUse() && toneProfile == RadioLink.PROFILE_CUSTOM &&
                        customSpec != RadioLink.fmCustomSpec(activity))

        if (fmInUse() && toneProfile == RadioLink.PROFILE_CUSTOM && customSpec.isBlank()) {
            Toast.makeText(
                activity,
                "Set up the custom FM settings first, or pick a preset",
                Toast.LENGTH_LONG
            ).show()
            return
        }

        if (transport == RadioLink.TRANSPORT_BLE && meshtastic && chosenAddress == null) {
            Toast.makeText(
                activity,
                "Pick the Meshtastic board — scan if it is not listed",
                Toast.LENGTH_LONG
            ).show()
            return
        }
        if (transport == RadioLink.TRANSPORT_BLE && chosenAddress == null && found.isEmpty()) {
            Toast.makeText(
                activity,
                "No paired radio to fall back on — pick one, or pair one first",
                Toast.LENGTH_LONG
            ).show()
            return
        }

        RadioLink.setTransport(activity, transport)
        RadioLink.setBleDevice(activity, chosenAddress, chosenName)
        RadioLink.setFmBackupEnabled(activity, fmBackup)
        RadioLink.setRadioFirmware(
            activity,
            if (meshtastic) RadioLink.RADIO_MESHTASTIC else RadioLink.RADIO_RNODE
        )
        RadioLink.setWifiEnabled(activity, wifiEnabled)
        RadioLink.setWifiIfname(activity, newWifiIfname)
        // Saved UNCONDITIONALLY, unlike the restart decision above: the choice
        // is worth remembering even on a node where FM is currently off, so
        // enabling FM later does not silently revert it.
        RadioLink.setFmToneProfile(activity, toneProfile)
        RadioLink.setFmVoxCable(activity, voxCable)
        RadioLink.setFmCustomSpec(activity, customSpec)
        RadioLink.setFmTxOff(activity, txOff)
        dialog?.dismiss()
        // Changing the wire under a running bridge means two services fighting
        // over one loopback port and RNS talking to a radio that is no longer
        // there. The restart is not politeness, it is the only coherent way to
        // land the change.
        onSaved(changed)
    }

    private fun dp(value: Int) = (value * activity.resources.displayMetrics.density).toInt()
}

/** Asks for the Bluetooth permissions the picker needs. */
fun Activity.requestBluetoothPermissions(requestCode: Int) {
    ActivityCompat.requestPermissions(this, RadioLinkDialog.requiredPermissions(), requestCode)
}
