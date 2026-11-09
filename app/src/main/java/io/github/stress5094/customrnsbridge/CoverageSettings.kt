package io.github.stress5094.customrnsbridge

import android.content.Context
import org.json.JSONObject

/**
 * The Coverage tab's own model settings: the FM frequency and power, and the
 * transmit antenna's height. SharedPreferences, so they survive a restart
 * and a reboot. Stored as the text the operator typed, and only after
 * coverage.check_settings() has accepted it -- Python owns the ranges, the
 * same way it owns every other number on that tab.
 *
 * LoRa is deliberately NOT here: it comes from the radio in use
 * (rns_bridge.coverage_radio -- what an RNode reports, what a Meshtastic
 * board is set to, else the app's Radio settings), so the coverage drawn for
 * LoRa is the coverage of the settings actually transmitting.
 */
object CoverageSettings {
    private const val PREFS = "coverage_model"
    const val KEY_FM_MHZ = "fm_mhz"
    const val KEY_FM_W = "fm_w"
    const val KEY_TX_HEIGHT_M = "tx_height_m"

    // The model's own defaults (coverage.RADIOS, coverage.ANT_HEIGHT_M),
    // shown in an empty field. Nothing is stored until the operator saves.
    const val DEFAULT_FM_MHZ = "146.000"
    const val DEFAULT_TX_HEIGHT_M = "1.5"
    const val DEFAULT_FM_W = "10"

    private fun prefs(c: Context) =
        c.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .also { migrate(it) }

    /**
     * Until 2026-10-20 the tab had two FM entries, VHF and UHF, each with its
     * own frequency and power, and picker index 2 was UHF. Carried over once:
     * the pair from whichever band was picked (VHF if neither), so a saved
     * setting survives the update, and a UHF pick becomes the FM entry.
     */
    private fun migrate(p: android.content.SharedPreferences) {
        if (p.contains(KEY_FM_MHZ) || p.contains(KEY_FM_W)) return
        val band = if (p.getInt(KEY_RADIO_INDEX, 0) == 2) "uhf" else "vhf"
        val mhz = p.getString("fm_${band}_mhz", null)
        val w = p.getString("fm_${band}_w", null)
        if (mhz == null && w == null) return
        p.edit().apply {
            if (mhz != null) putString(KEY_FM_MHZ, mhz)
            if (w != null) putString(KEY_FM_W, w)
            if (p.getInt(KEY_RADIO_INDEX, 0) == 2) putInt(KEY_RADIO_INDEX, 1)
            for (b in listOf("vhf", "uhf")) { remove("fm_${b}_mhz"); remove("fm_${b}_w") }
        }.commit()
    }

    fun fmMhz(c: Context): String = prefs(c).getString(KEY_FM_MHZ, null) ?: DEFAULT_FM_MHZ
    fun fmW(c: Context): String = prefs(c).getString(KEY_FM_W, null) ?: DEFAULT_FM_W
    fun txHeightM(c: Context): String = prefs(c).getString(KEY_TX_HEIGHT_M, null) ?: DEFAULT_TX_HEIGHT_M

    /** The model settings as coverage.compute()'s params, without LoRa. */
    fun params(c: Context, mhz: String = fmMhz(c), w: String = fmW(c),
               height: String = txHeightM(c)): JSONObject = JSONObject().apply {
        put(KEY_FM_MHZ, mhz)
        put(KEY_FM_W, w)
        put(KEY_TX_HEIGHT_M, height)
    }

    /** commit(), not apply(): the save is what the operator was promised
     *  survives a reboot, so it is on disk before the dialog says so. */
    fun save(c: Context, mhz: String, w: String, height: String): Boolean =
        prefs(c).edit()
            .putString(KEY_FM_MHZ, mhz)
            .putString(KEY_FM_W, w)
            .putString(KEY_TX_HEIGHT_M, height)
            .commit()

    // --- the tab's view choices, also kept across reboots ----------------
    private const val KEY_RADIO_INDEX = "radio_index"
    private const val KEY_SATELLITE = "satellite"
    private const val KEY_SHOW_PEERS = "show_peers"

    fun radioIndex(c: Context): Int = prefs(c).getInt(KEY_RADIO_INDEX, 0)
    fun setRadioIndex(c: Context, i: Int) = prefs(c).edit().putInt(KEY_RADIO_INDEX, i).apply()
    fun satellite(c: Context): Boolean = prefs(c).getBoolean(KEY_SATELLITE, true)
    fun setSatellite(c: Context, on: Boolean) = prefs(c).edit().putBoolean(KEY_SATELLITE, on).apply()
    fun showPeers(c: Context): Boolean = prefs(c).getBoolean(KEY_SHOW_PEERS, true)
    fun setShowPeers(c: Context, on: Boolean) = prefs(c).edit().putBoolean(KEY_SHOW_PEERS, on).apply()
}
