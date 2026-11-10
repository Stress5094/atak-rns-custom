package io.github.stress5094.customrnsbridge

import android.content.Context
import android.content.Intent

/**
 * How this phone reaches its radio: down a USB cable, or over Bluetooth.
 *
 * Kept in Kotlin's own preferences rather than in the Python config, because
 * this is the one setting that has to be readable BEFORE anything else works
 * — the bridge service reads it to decide which wire to open, and Python is
 * not running yet when it does. It is also the setting most likely to be
 * changed while the radio is unreachable, which is exactly when the Python
 * side has nothing to say.
 */
object RadioLink {

    const val TRANSPORT_USB = "usb"
    const val TRANSPORT_BLE = "ble"
    // The FM/codec2 audio link: a USB sound card instead of a KISS-speaking
    // radio. Removed 2026-10-01 and rebuilt from the ground up -- see
    // docs/fm-lessons.md for the seven weeks of bench work it inherits, and
    // FmAudioBridgeService/fm_link.py for the rebuild. The value has to match
    // rns_bridge.py's own _transport, which is why it is a constant and not a
    // literal at three call sites.
    const val TRANSPORT_FM = "fm"

    // What firmware the LoRa board runs, which is a separate question from how
    // it is reached. Both ride the SAME USB byte pipe (RadioBridgeService moves
    // raw bytes and does not care what they mean); only the Python side differs,
    // building an RNode interface or a Meshtastic one. The values have to match
    // rns_bridge.py's RADIO_RNODE / RADIO_MESHTASTIC. Over Bluetooth a Meshtastic
    // board is a GATT service with separate to/from mailboxes, not a serial
    // stream; BleSerialBridgeService translates it back into the USB stream
    // framing, so Python still sees the same bytes either way.
    const val RADIO_RNODE = "rnode"
    const val RADIO_MESHTASTIC = "meshtastic"

    private const val PREFS = "radio_link"
    private const val KEY_TRANSPORT = "transport"
    private const val KEY_BLE_ADDRESS = "ble_address"
    private const val KEY_BLE_NAME = "ble_name"
    private const val KEY_RADIO_FIRMWARE = "radio_firmware"
    // What the bridge was started with, for pendingRestart() -- see the other
    // KEY_ACTIVE_* comments below for why the selected value is not enough.
    private const val KEY_ACTIVE_RADIO_FIRMWARE = "active_radio_firmware"

    // Whether FM should run alongside USB/BLE as an automatic backup link,
    // for LoRa-primary/FM-backup routing. Meaningless when the transport
    // itself is TRANSPORT_FM -- there is no LoRa there to back up.
    private const val KEY_FM_BACKUP = "fm_backup"

    // Whether the WiFi link (an 802.11s BLOS mesh, via RNS's own stock
    // AutoInterface) should run alongside whichever of USB/BLE/FM is
    // selected above. Unlike TRANSPORT_*, this is not a way of reaching the
    // LoRa radio at all -- it needs no bridge service, no loopback socket,
    // nothing in serviceClasses()/stopBridgeServices() below. It is its own
    // independent link, wired straight into rns_bridge.py's N-way failover
    // (see set_wifi_enabled()/set_wifi_ifname()), the same relationship FM
    // backup has to LoRa.
    private const val KEY_WIFI_ENABLED = "wifi_enabled"
    // The 802.11s mesh interface name (e.g. "wlan0"), so rns_bridge.py's
    // AutoInterface joins only the mesh NIC, not every interface on the
    // phone -- see _wifi_ifname's own comment in rns_bridge.py.
    private const val KEY_WIFI_IFNAME = "wifi_ifname"
    // "" used to be the default (meaning "let AutoInterface's own
    // ANDROID_IGNORE_IFS decide") until live testing (2026-09-24, OnePlus
    // 8T) found that list only recognizes cellular interfaces literally
    // named "rmnetN" -- this ROM's are "rmnet_dataN", which AutoInterface
    // does not ignore, so it joined the WiFi multicast group on cellular
    // data AND on ifb0/ifb1/ifb2 (traffic-shaping virtual devices that also
    // exposed usable fe80: addresses here) right alongside wlan0. "wlan0"
    // is the WiFi interface name on effectively every real Android device,
    // so it is a safe default -- an operator who genuinely needs a
    // different NIC name can still override it in the WiFi mesh dialog.
    private const val DEFAULT_WIFI_IFNAME = "wlan0"

    // Which FSK tone placement the FM link uses -- see fm_config.py's PROFILES
    // for what each name means in Hz and what it measured.
    //
    // NAMED PROFILES RATHER THAN TWO FREE-TEXT FREQUENCY FIELDS. This is the
    // one FM setting whose mismatch is completely undetectable from the air --
    // a node on a different placement recovers no frames at all, so there is
    // nothing to report an error with -- which makes a free-text frequency the
    // easiest possible way for one person to type themselves out of the net
    // with no symptom but silence. A short list of names also gives two
    // operators something they can compare out loud.
    //
    // EVERY OFFERED NAME IS A PLACEMENT SOMETHING HAS BEEN MEASURED ON, which
    // is fm_config's own rule for its table ("an unmeasured entry is a way for
    // an operator to select a dead link in the field, which is worse than
    // having no choice at all"). Keep these two lists the same length.
    //
    // The list is duplicated here rather than read from Python because the
    // dialog has to draw before the bridge is running. Drift is contained on
    // the Python side instead of prevented here: set_fm_profile() falls back
    // to the measured default on a name it does not have, so a stale
    // preference costs the default placement, not a dead link.
    private const val KEY_FM_TONE_PROFILE = "fm_tone_profile"
    private const val KEY_FM_VOX_CABLE = "fm_vox_cable"
    private const val KEY_FM_TX_OFF = "fm_tx_off"
    private const val KEY_FM_TX_GAIN = "fm_tx_gain"
    // The operator's own settings (fm_custom JSON), used when the profile is
    // PROFILE_CUSTOM. Kept even while a preset is selected, so switching to a
    // preset and back does not lose them.
    private const val KEY_FM_CUSTOM_SPEC = "fm_custom_spec"
    // "fast", matching fm_config.DEFAULT_PROFILE. The two defaults are
    // separate by necessity (this one has to be readable before Python runs)
    // and a disagreement is invisible, so if one moves the other has to.
    const val TONE_PROFILE_DEFAULT = "fast"
    val TONE_PROFILES = listOf(TONE_PROFILE_DEFAULT, "narrow", "wide")

    // Operator-entered settings (fm_custom.CUSTOM_NAME). NOT in TONE_PROFILES:
    // that list is the MEASURED presets and is test-enforced against
    // fm_config.PROFILES. Custom is offered beside them, with its own editor.
    const val PROFILE_CUSTOM = "custom"

    /** What each profile is, for the picker. Frequencies and symbol rate
     * spelled out because the name is what operators match, the frequencies
     * are what say whether an unqualified radio will pass it, and the rate is
     * what decides how long every burst on the link runs. */
    fun toneProfileLabel(name: String): String = when (name) {
        "narrow" -> "Narrow - 1500/1750/2000/2250 Hz @ 250 Bd " +
            "(measured on handhelds, longer bursts)"
        "wide" -> "Wide - 1500/2000/2500/3000 Hz @ 250 Bd " +
            "(only for radios qualified at this spacing)"
        else -> "Fast - 1000/1400/1800/2200 Hz @ 400 Bd " +
            "(measured on the desktop bench, default)"
    }

    // The operator's manual override of the active link ("auto"/"lora"/
    // "fm"/"wifi"), persisted ONLY so the status screen redraws with the right
    // buttons highlighted immediately after a process restart -- rns_bridge
    // itself is the runtime source of truth (set_link_override()'s own
    // _link_override global), not this. Re-applied by PythonRnsService on
    // every startBridge() call.
    private const val KEY_LINK_OVERRIDE = "link_override"
    const val LINK_OVERRIDE_AUTO = "auto"

    // What the bridge service was actually started on, as opposed to what is
    // now selected. The two differ between saving a change and restarting, and
    // a screen that shows the new setting while the old wire is still carrying
    // the traffic is the same class of lie as a radio that reports online
    // after it has been unplugged.
    private const val KEY_ACTIVE_TRANSPORT = "active_transport"
    // Same idea, for the backup toggle specifically -- turning it on or off
    // needs the same restart as a transport change, and pendingRestart() has
    // to notice either one.
    private const val KEY_ACTIVE_FM_BACKUP = "active_fm_backup"
    // Same idea again, for WiFi: set_wifi_enabled() only takes effect on the
    // next start(), same as set_fm_backup_enabled().
    private const val KEY_ACTIVE_WIFI_ENABLED = "active_wifi_enabled"
    // And for the tone profile. fm_link.set_profile() DOES rebuild the receive
    // modem when called live, so a change would partly land on its own -- but
    // "partly" is the problem: an in-flight burst was built on the old
    // placement, so for a few seconds the two directions of the link disagree,
    // and a mistuned FM link looks exactly like dead hardware (which is the
    // whole reason this setting needs announcing at all). The restart makes
    // the change atomic instead.
    private const val KEY_ACTIVE_FM_TONE_PROFILE = "active_fm_tone_profile"
    private const val KEY_ACTIVE_FM_VOX_CABLE = "active_fm_vox_cable"
    private const val KEY_ACTIVE_FM_CUSTOM_SPEC = "active_fm_custom_spec"

    private fun prefs(context: Context) =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /** USB unless told otherwise: it is what every bench so far has run on. */
    fun transport(context: Context): String =
        prefs(context).getString(KEY_TRANSPORT, TRANSPORT_USB) ?: TRANSPORT_USB

    fun isBluetooth(context: Context) = transport(context) == TRANSPORT_BLE

    /** The firmware the LoRa board runs. ALWAYS "rnode" when FM is the
     * transport: a stored "meshtastic" survives a switch to FM (so switching
     * back does not lose it) but is not reported, because there is no LoRa
     * board on that link at all. USB and Bluetooth both carry either. */
    fun radioFirmware(context: Context): String =
        if (transport(context) == TRANSPORT_FM) RADIO_RNODE
        else prefs(context).getString(KEY_RADIO_FIRMWARE, RADIO_RNODE)
            .takeIf { it == RADIO_MESHTASTIC } ?: RADIO_RNODE

    fun isMeshtastic(context: Context) = radioFirmware(context) == RADIO_MESHTASTIC

    /** The stored choice itself, for the dialog, regardless of transport. */
    fun storedRadioFirmware(context: Context): String =
        prefs(context).getString(KEY_RADIO_FIRMWARE, RADIO_RNODE)
            .takeIf { it == RADIO_MESHTASTIC } ?: RADIO_RNODE

    fun setRadioFirmware(context: Context, firmware: String) {
        val clean = if (firmware == RADIO_MESHTASTIC) RADIO_MESHTASTIC else RADIO_RNODE
        prefs(context).edit().putString(KEY_RADIO_FIRMWARE, clean).apply()
    }

    fun isFm(context: Context) = transport(context) == TRANSPORT_FM

    /** FM as an automatic backup alongside USB/BLE. Off by default: this is
     * an opt-in change to a running node's behaviour, not a new default. */
    fun fmBackupEnabled(context: Context): Boolean =
        !isFm(context) && prefs(context).getBoolean(KEY_FM_BACKUP, false)

    fun setFmBackupEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_FM_BACKUP, enabled).apply()
    }

    /** The WiFi link. Off by default, same reasoning as FM backup: an opt-in
     * change to a running node's behaviour, not a new default. */
    fun wifiEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_WIFI_ENABLED, false)

    fun setWifiEnabled(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_WIFI_ENABLED, enabled).apply()
    }

    /** "wlan0" unless told otherwise -- see DEFAULT_WIFI_IFNAME's own
     * comment for why an empty string is no longer the default. */
    fun wifiIfname(context: Context): String =
        prefs(context).getString(KEY_WIFI_IFNAME, DEFAULT_WIFI_IFNAME) ?: DEFAULT_WIFI_IFNAME

    fun setWifiIfname(context: Context, ifname: String) {
        prefs(context).edit().putString(KEY_WIFI_IFNAME, ifname.trim()).apply()
    }

    /** The FM tone profile name. Always one this build knows: an unknown
     * stored value reads back as the default rather than being passed
     * through, so a downgrade cannot hand Python a name it will only reject
     * later. */
    fun fmToneProfile(context: Context): String {
        val stored = prefs(context).getString(KEY_FM_TONE_PROFILE, TONE_PROFILE_DEFAULT)
        return if (stored in TONE_PROFILES || stored == PROFILE_CUSTOM) stored!!
        else TONE_PROFILE_DEFAULT
    }

    fun setFmToneProfile(context: Context, name: String) {
        val clean = if (name in TONE_PROFILES || name == PROFILE_CUSTOM) name
        else TONE_PROFILE_DEFAULT
        prefs(context).edit().putString(KEY_FM_TONE_PROFILE, clean).apply()
    }

    /** The custom settings as fm_custom JSON, "" if never set. Not validated
     * here: Python validates, and an unusable one costs the default preset
     * with an ERROR line (fm_link.set_custom), not a failed start. */
    fun fmCustomSpec(context: Context): String =
        prefs(context).getString(KEY_FM_CUSTOM_SPEC, "") ?: ""

    fun setFmCustomSpec(context: Context, specJson: String) {
        prefs(context).edit().putString(KEY_FM_CUSTOM_SPEC, specJson).apply()
    }

    /** Record the CURRENT FM profile and custom settings as the ones
     * running, so pendingRestart() does not ask for a restart. Only for a
     * change the bridge has already applied live -- a shared FM setting
     * (settings_link applies it through set_fm_profile at the switch time,
     * because a restart cannot be started from the background service). */
    fun markFmActive(context: Context) {
        prefs(context).edit()
            .putString(KEY_ACTIVE_FM_TONE_PROFILE, fmToneProfile(context))
            .putString(KEY_ACTIVE_FM_CUSTOM_SPEC, fmCustomSpec(context))
            .apply()
    }

    /** The "no PTT pin" flag as the bridge should get it. Custom settings
     * carry their own keying, chosen in the editor, so the checkbox (hidden
     * while Custom is selected) must not force VOX onto them. */
    fun fmVoxForBridge(context: Context): Boolean =
        fmVoxCable(context) && fmToneProfile(context) != PROFILE_CUSTOM

    /** The FM cable has NO PTT pin (K1 PRO class), so the audio itself keys
     * the radio's VOX. Off by default: the Digirig Lite keys by CM108 GPIO and
     * needs none of this. On makes Python build bursts with the long VOX
     * lead-in (fm_config.VOX_CABLE_LEAD_IN_S) -- the radio starts 0.3-0.7 s
     * after the audio and would otherwise lose the front of every burst. */
    fun fmVoxCable(context: Context): Boolean =
        prefs(context).getBoolean(KEY_FM_VOX_CABLE, false)

    fun setFmVoxCable(context: Context, enabled: Boolean) {
        prefs(context).edit().putBoolean(KEY_FM_VOX_CABLE, enabled).apply()
    }

    /** TEST MODE: FM receives but never transmits. Every outgoing burst is
     * dropped just before the radio would key (see FmAudioBridgeService), so
     * the node sends no beacons, no messages and no resends. Off by default
     * and meant to be switched on only while measuring the link from another
     * node: a half-duplex radio hears nothing while it transmits, and on
     * 2026-10-09 five of the six bursts a laptop sent to this phone that did
     * not arrive were lost to this node's own beacons (86% delivered with it
     * transmitting, 26/26 without). It looks healthy while on -- online,
     * receiving -- and reaches nobody, so the status screen says so loudly.
     * Read per burst, so it takes effect without a restart. */
    fun fmTxOff(context: Context): Boolean =
        prefs(context).getBoolean(KEY_FM_TX_OFF, false)

    fun setFmTxOff(context: Context, off: Boolean) {
        prefs(context).edit().putBoolean(KEY_FM_TX_OFF, off).apply()
    }

    /** DIAGNOSTIC ONLY, no dialog entry: the AudioTrack gain for FM bursts,
     * 1.0 (full) unless the pref is set by hand, e.g. through adb run-as. For
     * testing whether the phone's output level is overdriving a radio's mic.
     * Clamped so a typo cannot silence the link entirely. */
    fun fmTxGain(context: Context): Float =
        prefs(context).getFloat(KEY_FM_TX_GAIN, 1.0f).coerceIn(0.02f, 1.0f)

    /** Whether FM would carry traffic at all as currently configured -- as
     * the transport itself, or alongside LoRa as the backup link. The tone
     * profile only means anything when this is true. */
    fun fmInUse(context: Context): Boolean =
        isFm(context) || fmBackupEnabled(context)

    /** "auto"/"lora"/"fm"/"wifi" -- see set_link_override() in rns_bridge.py. */
    fun linkOverride(context: Context): String =
        prefs(context).getString(KEY_LINK_OVERRIDE, LINK_OVERRIDE_AUTO) ?: LINK_OVERRIDE_AUTO

    fun setLinkOverride(context: Context, link: String) {
        prefs(context).edit().putString(KEY_LINK_OVERRIDE, link).apply()
    }

    /** The Debug tab's "Advanced features" switch. Off by default, it only
     * HIDES controls: the link override buttons, and in the Radio link dialog
     * the WiFi mesh, custom FM settings, the no-PTT-pin cable and receive-only
     * test mode. Settings already made keep their values while hidden; the
     * Status tab still warns about a forced link or receive-only mode. */
    private const val KEY_ADVANCED = "advanced_features"

    fun advancedEnabled(context: Context): Boolean =
        prefs(context).getBoolean(KEY_ADVANCED, false)

    fun setAdvancedEnabled(context: Context, on: Boolean) {
        prefs(context).edit().putBoolean(KEY_ADVANCED, on).apply()
    }

    /** Debug → Advanced features → "Show detailed status". Off by default:
     * the Status tab then leaves out its informational lines (build stamp,
     * FM net code and operating point, receive summary, re-send counts) and
     * keeps only the warnings. */
    private const val KEY_STATUS_DETAILS = "status_details"

    fun statusDetails(context: Context): Boolean =
        prefs(context).getBoolean(KEY_STATUS_DETAILS, false)

    fun setStatusDetails(context: Context, on: Boolean) {
        prefs(context).edit().putBoolean(KEY_STATUS_DETAILS, on).apply()
    }

    /** Bluetooth address of the chosen radio, or null to use the only paired one. */
    fun bleAddress(context: Context): String? =
        prefs(context).getString(KEY_BLE_ADDRESS, null)

    fun bleName(context: Context): String? =
        prefs(context).getString(KEY_BLE_NAME, null)

    fun setTransport(context: Context, transport: String) {
        prefs(context).edit().putString(KEY_TRANSPORT, transport).apply()
    }

    /** Remember the radio the operator picked. Null address clears the choice. */
    fun setBleDevice(context: Context, address: String?, name: String?) {
        prefs(context).edit()
            .putString(KEY_BLE_ADDRESS, address)
            .putString(KEY_BLE_NAME, name)
            .apply()
    }

    /** One line for the button on the status screen. */
    fun label(context: Context): String = when {
        isFm(context) -> "FM audio (USB)"
        isMeshtastic(context) && !isBluetooth(context) -> "Meshtastic board (USB)"
        isMeshtastic(context) ->
            "Meshtastic · Bluetooth · " + (bleName(context) ?: bleAddress(context) ?: "none chosen")
        !isBluetooth(context) -> "USB cable"
        bleAddress(context) == null -> "Bluetooth · paired RNode"
        else -> "Bluetooth · " + (bleName(context) ?: bleAddress(context))
    }

    /** Just the radio, for a line that has already said which kind of link it is. */
    fun radioName(context: Context): String =
        bleName(context) ?: bleAddress(context) ?: "the only paired radio"

    fun serviceClass(context: Context): Class<out RadioBridgeService> = when {
        isFm(context) -> FmAudioBridgeService::class.java
        isBluetooth(context) -> BleSerialBridgeService::class.java
        else -> UsbSerialBridgeService::class.java
    }

    /**
     * Every service that should be running right now: the primary transport
     * always, plus FM alongside it when backup is enabled. For
     * LoRa-primary/FM-backup routing -- both bridge services genuinely run
     * concurrently (each subclass holds its own loopback port, see
     * RadioBridgeService.kt), not the single mutually-exclusive choice
     * serviceClass() alone implies.
     */
    fun serviceClasses(context: Context): List<Class<out RadioBridgeService>> {
        val primary = serviceClass(context)
        return if (fmBackupEnabled(context)) {
            listOf(primary, FmAudioBridgeService::class.java)
        } else {
            listOf(primary)
        }
    }

    fun startBridgeServices(context: Context) {
        serviceClasses(context).forEach {
            context.startForegroundService(Intent(context, it))
        }
        prefs(context).edit()
            .putString(KEY_ACTIVE_TRANSPORT, transport(context))
            .putBoolean(KEY_ACTIVE_FM_BACKUP, fmBackupEnabled(context))
            .putBoolean(KEY_ACTIVE_WIFI_ENABLED, wifiEnabled(context))
            .putString(KEY_ACTIVE_FM_TONE_PROFILE, fmToneProfile(context))
            .putBoolean(KEY_ACTIVE_FM_VOX_CABLE, fmVoxCable(context))
            .putString(KEY_ACTIVE_FM_CUSTOM_SPEC, fmCustomSpec(context))
            .putString(KEY_ACTIVE_RADIO_FIRMWARE, radioFirmware(context))
            .apply()
    }

    /** True when the selected link (or the backup/WiFi toggle, or the FM
     * tone profile) is not what is currently carrying traffic.
     *
     * A SETTING ONLY COUNTS HERE WHEN IT IS ACTUALLY IN USE, and both halves
     * of that have cost a bug. The tone profile is compared only while FM is
     * in use, because on a LoRa-only node it is saved and never applied, and
     * comparing it unconditionally leaves the screen permanently claiming a
     * restart is due over a setting carrying nothing. And it is defaulted to
     * the CURRENT value rather than to TONE_PROFILE_DEFAULT, so an install
     * predating the setting does not report a pending restart on every poll.
     * Both traps apply to any per-link setting added here later.
     */
    fun pendingRestart(context: Context): Boolean {
        val active = prefs(context).getString(KEY_ACTIVE_TRANSPORT, null) ?: return false
        if (active != transport(context)) return true
        if (prefs(context).getBoolean(KEY_ACTIVE_FM_BACKUP, false) != fmBackupEnabled(context)) return true
        if (prefs(context).getBoolean(KEY_ACTIVE_WIFI_ENABLED, false) != wifiEnabled(context)) return true
        // Defaulted to the CURRENT value, the same trap as the tone profile: an
        // install predating this setting must not report a pending restart.
        if (prefs(context).getString(
                KEY_ACTIVE_RADIO_FIRMWARE, radioFirmware(context)
            ) != radioFirmware(context)) return true
        if (!fmInUse(context)) return false
        // Defaulted to the CURRENT value, same trap as the tone profile: an
        // install predating this setting must not report a pending restart.
        if (prefs(context).getBoolean(
                KEY_ACTIVE_FM_VOX_CABLE, fmVoxCable(context)
            ) != fmVoxCable(context)) return true
        // The custom settings count only while Custom is selected -- the same
        // "only when in use" rule as everything above -- and default to the
        // CURRENT value for an install predating them.
        if (fmToneProfile(context) == PROFILE_CUSTOM && prefs(context).getString(
                KEY_ACTIVE_FM_CUSTOM_SPEC, fmCustomSpec(context)
            ) != fmCustomSpec(context)) return true
        return prefs(context).getString(
            KEY_ACTIVE_FM_TONE_PROFILE, fmToneProfile(context)
        ) != fmToneProfile(context)
    }

    /**
     * All of them, always. Simpler than computing which ones changed, and
     * cheap: each service now holds its own loopback port (see
     * RadioBridgeService.kt), so stopping one that was never running is a
     * harmless no-op rather than something that needs avoiding. Used both
     * for an ordinary transport/backup change and to guarantee a clean
     * slate before startBridgeServices() re-launches whatever is selected.
     */
    fun stopBridgeServices(context: Context) {
        context.stopService(Intent(context, UsbSerialBridgeService::class.java))
        context.stopService(Intent(context, BleSerialBridgeService::class.java))
        context.stopService(Intent(context, FmAudioBridgeService::class.java))
    }
}
