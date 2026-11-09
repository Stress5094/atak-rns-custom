package io.github.stress5094.customrnsbridge

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * Where Android sends USB_DEVICE_ATTACHED for the radios in
 * res/xml/device_filter.xml. Never seen: it opens the app only if the radio
 * bridge is not already running, and otherwise just closes.
 *
 * Why it exists (2026-10-14): once this app is a radio's "always open" handler,
 * Android starts the attach activity EVERY time the radio appears -- and a
 * Meshtastic board re-appears each time a settings change restarts it. With
 * MainActivity as that activity, every restart stacked a second copy of the
 * main screen on top of the first (seen on the OnePlus: one task, two
 * MainActivity instances, the apply result hidden under the new one), and it
 * would come up over ATAK as well. The bridge service already notices the
 * radio on its own (its attach receiver and its 3 s retry); this activity is
 * only what has to answer Android.
 */
class UsbAttachActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!UsbSerialBridgeService.running) {
            // The old behaviour, kept for the case it was for: plugging a
            // radio in starts the app when nothing is running yet.
            packageManager.getLaunchIntentForPackage(packageName)?.let {
                it.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                startActivity(it)
            }
        }
        finish()
    }
}
