package io.github.stress5094.customrnsbridge

import androidx.appcompat.app.AlertDialog
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Color
import android.os.Handler
import android.text.InputType
import android.util.Log
import android.view.View
import android.widget.CheckBox
import android.widget.EditText
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import org.json.JSONArray
import org.json.JSONObject
import java.text.DateFormat
import java.util.Date
import java.util.concurrent.Executors

/**
 * Settings sharing, the screens (the settings-sharing notes: step 2, QR;
 * step 3, a host phone on the same WiFi).
 * The rules live in Python (settings_share.py, wired by settings_link.py);
 * this only shows what Python says and passes back what the operator chose.
 *
 * Kotlin owns ONE group of shared settings, the Coverage tab's
 * (CoverageSettings): its current values go in with every call (kotlinJson),
 * and a "coverage" in a result is saved here (saveCoverage).
 */
class SettingsShareUi(
    private val activity: MainActivity,
    private val handler: Handler,
    private val startScan: () -> Unit,
) {
    private val executor = Executors.newSingleThreadExecutor()

    companion object {
        private const val TAG = "SettingsShare"
        private const val CHANNEL_ID = "settings_share"
        private const val NOTIF_ID = 5094
        private const val ROSTER_REFRESH_MS = 2000L
        // settings_net.CARD_PREFIX: a server's code rather than settings.
        private const val SERVER_CODE_PREFIX = "RTAKSRV1."

        /** An offer arrived over WiFi and nobody has looked at it yet: the
         *  main screen opens it on its next resume. */
        @Volatile var unseenOffer = false

        /** A host was heard on WiFi that this phone has not approved: the main
         *  screen asks about it on its next resume. */
        @Volatile var unseenHost = false

        /** Host keys as people compare them: "ab12 cd34 ef56 7890". */
        fun fingerprint(hostId: String) = hostId.chunked(4).joinToString(" ")

        /** True when an offer (a JSON offer view) turns "send FM unencrypted"
         *  ON -- worth saying so in the notification, not just on the screen. */
        private fun offerTurnsOffEncryption(offer: JSONObject?): Boolean {
            if (offer == null) return false
            val changes = offer.optJSONArray("changes") ?: return false
            for (i in 0 until changes.length()) {
                val c = changes.getJSONObject(i)
                if (c.optString("key") == "fm_clear" &&
                    c.optString("new").lowercase() in listOf("true", "yes", "1")) return true
            }
            return false
        }

        private fun notify(c: Context, title: String, body: String) {
            val nm = c.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            if (android.os.Build.VERSION.SDK_INT >= 26) {
                nm.createNotificationChannel(NotificationChannel(
                    CHANNEL_ID, "Shared settings", NotificationManager.IMPORTANCE_HIGH))
            }
            val open = PendingIntent.getActivity(c, 0,
                Intent(c, MainActivity::class.java)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val n = Notification.Builder(c, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_notify_sync)
                .setContentTitle(title)
                .setContentText(body)
                .setStyle(Notification.BigTextStyle().bigText(body))
                .setContentIntent(open)
                .setAutoCancel(true)
                .build()
            // Without the permission Android drops it without an error.
            if (!nm.areNotificationsEnabled()) Log.w(TAG, "notifications are off, not shown: $title")
            try { nm.notify(NOTIF_ID, n) } catch (e: SecurityException) {
                Log.w(TAG, "no permission to notify: $title")
            }
        }

        /** At service start: listen for settings hosts and/or host, as last
         *  chosen on this phone. */
        fun netStart(c: Context) {
            val out = PythonRnsService.callModule("settings_link", "net_start", kotlinJson(c))
            Log.i(TAG, "settings over WiFi: $out")
        }

        fun kotlinJson(c: Context): String =
            JSONObject().put("coverage", CoverageSettings.params(c))
                .put("fm", JSONObject()
                    .put("profile", RadioLink.fmToneProfile(c))
                    .put("custom_spec", RadioLink.fmCustomSpec(c))
                    .put("vox", RadioLink.fmVoxCable(c))
                    // "Send FM unencrypted": shared with the FM group, since a
                    // phone with it off ignores what a phone with it on sends.
                    .put("clear", RadioLink.fmClear(c)))
                .toString()

        /** Save the FM settings a result hands back, if any. Python has
         *  ALREADY switched the running link (settings_link applies FM live),
         *  so they are recorded as running too: no restart is pending. */
        fun saveFm(c: Context, result: JSONObject) {
            val fm = result.optJSONObject("fm") ?: return
            RadioLink.setFmToneProfile(c, fm.optString("profile", RadioLink.fmToneProfile(c)))
            if (fm.has("custom_spec")) RadioLink.setFmCustomSpec(c, fm.optString("custom_spec"))
            if (fm.has("clear")) RadioLink.setFmClear(c, fm.optBoolean("clear"))
            RadioLink.markFmActive(c)
            Log.i(TAG, "FM settings from a share saved: ${fm.optString("profile")}")
        }

        /** Save the Coverage settings a result hands back, if any. */
        fun saveCoverage(c: Context, result: JSONObject) {
            val cov = result.optJSONObject("coverage") ?: return
            CoverageSettings.save(c,
                cov.optString(CoverageSettings.KEY_FM_MHZ, CoverageSettings.fmMhz(c)),
                cov.optString(CoverageSettings.KEY_FM_W, CoverageSettings.fmW(c)),
                cov.optString(CoverageSettings.KEY_TX_HEIGHT_M, CoverageSettings.txHeightM(c)))
        }

        /** The service's tick: scheduled switches, which must happen while
         *  ATAK is in front, so not from a screen. */
        fun tick(c: Context) {
            val out = PythonRnsService.callModule("settings_link", "tick", kotlinJson(c))
                ?: return
            val obj = try { JSONObject(out) } catch (e: Exception) { return }
            saveCoverage(c, obj)
            saveFm(c, obj)
            val events = obj.optJSONArray("events") ?: return
            for (i in 0 until events.length()) {
                val e = events.getJSONArray(i)
                val kind = e.optString(0)
                Log.i(TAG, "settings $kind: ${e.optString(1)}")
                onEvent(c, kind, e.optString(1))
            }
        }

        /** What happened outside a screen, said where the operator will see
         *  it: a notification always (ATAK is usually in front), and the
         *  main screen as well when it is showing. */
        private fun onEvent(c: Context, kind: String, detail: String) {
            val offer = if (detail.startsWith("{")) try { JSONObject(detail) } catch (e: Exception) { null } else null
            val from = offer?.optString("host_name") ?: ""
            val shown = MainActivity.visible
            when (kind) {
                "offer" -> {
                    unseenOffer = true
                    notify(c, "Settings from $from are waiting",
                        "Open the app to review them. Accepting needs this phone's PIN.")
                    shown?.runOnUiThread { shown.showSharedSettingsOffers() }
                }
                "applied" -> {
                    val warn = if (offerTurnsOffEncryption(offer))
                        "\n⚠ FM is now sent UNENCRYPTED: anyone listening can read every message."
                        else ""
                    notify(c, "Settings from $from applied",
                        "$from is a trusted host. Open Share settings to undo.$warn")
                    shown?.runOnUiThread { shown.onSharedSettingsApplied() }
                }
                "scheduled" -> {
                    val warn = if (offerTurnsOffEncryption(offer))
                        "\n⚠ This turns OFF encryption on FM."
                        else ""
                    notify(c, "Settings from $from accepted",
                        "This phone switches at ${offer?.let { switchTime(it) } ?: "the time the host set"}.$warn")
                }
                "switched" -> {
                    notify(c, "Switched to the shared settings", detail)
                    shown?.runOnUiThread { shown.onSharedSettingsApplied() }
                }
                "failed" -> notify(c, "Shared settings could not be applied", detail)
                "host_found" -> {
                    unseenHost = true
                    notify(c, "Settings host found: ${offer?.optString("name").orEmpty()}",
                        "Not connected. Open the app to connect to it or ignore it.")
                    shown?.runOnUiThread { shown.showFoundSettingsHosts() }
                }
            }
        }

        private fun switchTime(offer: JSONObject): String? {
            if (offer.isNull("switch_at")) return null
            return DateFormat.getTimeInstance(DateFormat.SHORT)
                .format(Date(offer.optLong("switch_at") * 1000))
        }
    }

    private fun dp(v: Int) = (v * activity.resources.displayMetrics.density).toInt()

    /** Python off the main thread, the answer back on it. */
    private fun py(fn: String, vararg args: String, then: (JSONObject) -> Unit) {
        executor.execute {
            val raw = PythonRnsService.callModule("settings_link", fn, *args)
            val obj = try {
                raw?.let { JSONObject(it) } ?: JSONObject().put("error", "The bridge is not up yet")
            } catch (e: Exception) {
                JSONObject().put("error", "Bad answer from the bridge")
            }
            handler.post {
                if (obj.has("coverage")) saveCoverage(activity, obj)
                if (obj.has("fm")) saveFm(activity, obj)
                then(obj)
            }
        }
    }

    /** The FM group, labelled with what this phone would actually send: the
     *  preset's name, or the custom settings' net code. */
    private fun fmCheckbox() = com.google.android.material.checkbox.MaterialCheckBox(activity).apply {
        val profile = RadioLink.fmToneProfile(activity)
        val tones = if (profile == RadioLink.PROFILE_CUSTOM) "FM settings: Custom (net code)"
                    else "FM settings: ${profile.replaceFirstChar { it.uppercase() }} preset"
        text = tones + if (RadioLink.fmClear(activity)) ", sent unencrypted" else ", encrypted"
    }

    private fun fmNote() = text("FM sends only what every radio must share (tones, rate, " +
        "code, and whether FM is sent unencrypted); each phone keeps its own keying and " +
        "lead-in. Phones switch FM without restarting.", 12f, Ui.onSurfaceVariant(activity))

    private fun toast(msg: String) = Toast.makeText(activity, msg, Toast.LENGTH_LONG).show()

    private fun column() = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(24), dp(12), dp(24), 0)
    }

    private fun text(s: String, size: Float = 14f, color: Int? = null) = TextView(activity).apply {
        text = s
        textSize = size
        color?.let { setTextColor(it) }
        setPadding(0, dp(6), 0, 0)
    }

    private fun pinField(hint: String) = EditText(activity).apply {
        this.hint = hint
        inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
        setSingleLine()
        // Explicitly: setSingleLine() resets the transformation, and the PIN
        // then showed in clear on the OnePlus (seen 2026-10-20).
        transformationMethod = android.text.method.PasswordTransformationMethod.getInstance()
    }

    private fun scrolling(v: View) = ScrollView(activity).apply { addView(v) }

    // -- the Share tab ------------------------------------------------------------

    // Everything about sharing settings, as a page of its own (MainActivity's
    // Share tab); the Status tab keeps only "Share settings by QR code".
    private var pageSummary: TextView? = null
    private var pageWaiting: LinearLayout? = null
    private var pageHostButton: android.widget.Button? = null
    private var pageListen: com.google.android.material.materialswitch.MaterialSwitch? = null
    private var pageListenSetting = false   // set from code, not a tap
    private var pageServersButton: android.widget.Button? = null
    private var pageUndoButton: android.widget.Button? = null
    private var pageId: TextView? = null
    private var lastStatus: JSONObject? = null
    private var pageShowing = false

    private val pageRefresh = object : Runnable {
        override fun run() {
            if (!pageShowing) return
            refreshPage()
            handler.postDelayed(this, ROSTER_REFRESH_MS)
        }
    }

    private fun heading(t: String) = TextView(activity).apply {
        text = t
        Ui.heading(this)
        setPadding(0, dp(20), 0, dp(4))
    }

    private fun note(t: String) = text(t, 12f, Ui.onSurfaceVariant(activity))

    /** Outlined unless said otherwise: see Ui for the emphasis rule. */
    private fun button(label: String, make: (android.content.Context, String) -> android.widget.Button =
                           Ui::outlined, onClick: () -> Unit) =
        make(activity, label).apply { setOnClickListener { onClick() } }

    fun buildPage(): View {
        val box = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(10), dp(20), dp(24))
        }
        pageSummary = text("Loading…", 13f).also { box.addView(it) }
        pageWaiting = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
            .also { box.addView(it) }

        // Sharing by QR code in person is not repeated here: its two buttons,
        // Share settings and Scan QR, are on the Status tab.

        box.addView(heading("This phone as a host"))
        pageHostButton = button("Host settings for phones on this WiFi", Ui::tonal) { showHost() }
            .also { box.addView(it) }
        box.addView(note("Phones on the same WiFi that approve this phone can then be sent " +
            "its settings. Each still needs its own PIN unless it trusts this phone."))

        box.addView(heading("Receiving"))
        // A switch: an on/off setting that takes effect at once.
        pageListen = com.google.android.material.materialswitch.MaterialSwitch(activity).apply {
            text = "Listen for hosts on this WiFi"
            minHeight = dp(48)
            setOnCheckedChangeListener { _, on ->
                if (pageListenSetting) return@setOnCheckedChangeListener
                py("net_set_listen", on.toString()) { r ->
                    toast(r.optString("error").ifEmpty {
                        if (r.optBoolean("listening")) "Listening for settings hosts" else "Not listening"
                    })
                    refreshPage()
                }
            }
        }.also { box.addView(it) }
        pageServersButton = button("Servers and hosts") { showServers() }.also { box.addView(it) }
        pageUndoButton = button("Undo the last shared change") { confirmUndo() }.apply {
            visibility = View.GONE
        }.also { box.addView(it) }

        box.addView(heading("Security"))
        box.addView(button("PIN and trusted hosts") {
            lastStatus?.let { showSecurity(it) } ?: refreshPage()
        })
        pageId = note("").also { box.addView(it) }
        return box
    }

    /** The Share tab opened (true) or left (false). */
    fun pageVisible(visible: Boolean) {
        pageShowing = visible
        handler.removeCallbacks(pageRefresh)
        if (visible) handler.post(pageRefresh)
    }

    fun refreshPage() {
        py("status", SettingsShareUi.kotlinJson(activity)) { st ->
            if (st.has("error")) {
                pageSummary?.text = st.optString("error")
                return@py
            }
            lastStatus = st
            renderPage(st)
        }
    }

    private fun renderPage(st: JSONObject) {
        val servers = st.optJSONArray("servers") ?: JSONArray()
        val wifi = st.optJSONArray("wifi_hosts") ?: JSONArray()
        val connected = (0 until servers.length()).count { servers.getJSONObject(it).optBoolean("connected") } +
            (0 until wifi.length()).count { wifi.getJSONObject(it).optBoolean("connected") }
        val total = servers.length() + wifi.length()
        val trusted = st.optJSONArray("trusted")?.length() ?: 0
        pageSummary?.text = listOf(
            if (st.optBoolean("has_pin")) "PIN set" else "No PIN yet (asked for with the first change)",
            "listening on WiFi: " + if (st.optBoolean("listening")) "on" else "off",
            "hosting: " + if (st.optBoolean("hosting")) "on" else "off",
            "servers and hosts: " + if (total == 0) "none" else "$connected of $total connected",
            "trusted: " + if (trusted == 0) "none" else "$trusted",
        ).joinToString(" · ")

        val waiting = pageWaiting ?: return
        waiting.removeAllViews()
        val pending = st.optJSONArray("pending") ?: JSONArray()
        val found = st.optJSONArray("found") ?: JSONArray()
        if (pending.length() + found.length() > 0) waiting.addView(heading("Waiting for you"))
        for (i in 0 until pending.length()) {
            val o = pending.getJSONObject(i)
            waiting.addView(button("Settings from ${o.optString("host_name")}: review", Ui::filled) {
                showApproval(o)
            })
        }
        for (i in 0 until found.length()) {
            val f = found.getJSONObject(i)
            waiting.addView(button("New host on WiFi: ${f.optString("name")}: connect or ignore",
                                   Ui::filled) {
                showFoundHost(f)
            })
        }

        pageHostButton?.text = if (st.optBoolean("hosting")) "Hosting on this WiFi: phones and pushes"
                               else "Host settings for phones on this WiFi"
        pageListen?.let {
            pageListenSetting = true
            it.isChecked = st.optBoolean("listening")
            pageListenSetting = false
        }
        pageServersButton?.text = if (total == 0) "Servers and hosts: none yet"
                                  else "Servers and hosts: $connected of $total connected"
        val undo = st.optJSONObject("undo")
        pageUndoButton?.apply {
            visibility = if (undo == null) View.GONE else View.VISIBLE
            if (undo != null) text = "Undo the last shared change (from ${undo.optString("host_name")})"
        }
        pageId?.text = "This phone's id, for a server's admin: ${fingerprint(st.optString("phone_id"))}\n" +
            "This phone's key as a host: ${fingerprint(st.optJSONObject("host")?.optString("host_id").orEmpty())}"
    }

    // -- sharing (this phone as the host) ---------------------------------------

    /** The camera, for another phone's settings QR or a server's code. */
    fun scanQr() = startScan()

    /** The Status tab's button, and the Share tab's. */
    fun showQrShare() {
        val col = column()
        col.addView(text("Which of this phone's settings should the QR code carry?"))
        val lora = com.google.android.material.checkbox.MaterialCheckBox(activity).apply { text = "LoRa radio: preset, frequency, power"; isChecked = true }
        val cov = com.google.android.material.checkbox.MaterialCheckBox(activity).apply { text = "Coverage model: FM frequency and power, antenna height" }
        val fm = fmCheckbox()
        val pass = com.google.android.material.checkbox.MaterialCheckBox(activity).apply { text = "Network passphrase" }
        val passWarn = text("Anyone who photographs this QR code gets the network's key. " +
            "Show it only to the phones that should join.", 12f, Ui.error(activity)).apply {
            visibility = View.GONE
        }
        pass.setOnCheckedChangeListener { _, on -> passWarn.visibility = if (on) View.VISIBLE else View.GONE }
        val fmClearWarn = text("⚠ The FM group turns OFF encryption on every phone that accepts " +
            "it — anyone listening can read their messages.", 12f, Ui.error(activity)).apply {
            visibility = View.GONE
        }
        fm.setOnCheckedChangeListener { _, on ->
            fmClearWarn.visibility = if (on && RadioLink.fmClear(activity)) View.VISIBLE else View.GONE
        }
        listOf(lora, cov, fm, pass, passWarn, fmClearWarn).forEach { col.addView(it) }
        col.addView(fmNote())
        MaterialAlertDialogBuilder(activity)
            .setTitle("Share settings")
            .setView(scrolling(col))
            .setPositiveButton("Show QR code") { _, _ ->
                val opts = JSONObject().put("lora", lora.isChecked)
                    .put("coverage", cov.isChecked).put("fm", fm.isChecked)
                    .put("passphrase", pass.isChecked)
                py("share_qr", opts.toString(), SettingsShareUi.kotlinJson(activity)) { r ->
                    if (r.has("error")) toast(r.optString("error")) else showQr(r)
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun qrBitmap(content: String, size: Int): Bitmap {
        val m = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE, size, size,
            mapOf(EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.M,
                  EncodeHintType.MARGIN to 2))
        val px = IntArray(size * size) { i ->
            if (m.get(i % size, i / size)) Color.BLACK else Color.WHITE
        }
        return Bitmap.createBitmap(px, size, size, Bitmap.Config.RGB_565)
    }

    private fun showQr(r: JSONObject) {
        val col = column()
        val size = (activity.resources.displayMetrics.widthPixels * 0.75).toInt()
        col.addView(ImageView(activity).apply { setImageBitmap(qrBitmap(r.optString("text"), size)) })
        val items = r.optJSONArray("items") ?: JSONArray()
        val lines = (0 until items.length()).joinToString("\n") {
            val c = items.getJSONObject(it)
            "• ${c.optString("label")}: ${c.optString("new")}"
        }
        col.addView(text("From ${r.optString("host_name")} (revision ${r.optInt("rev")}):\n$lines", 13f))
        col.addView(text("This phone's key: ${fingerprint(r.optString("host_id"))}", 15f))
        col.addView(text("On the other phone: Status → Scan QR. It shows what will change, " +
            "and this key to compare, and needs that phone's PIN.", 12f, Ui.onSurfaceVariant(activity)))
        MaterialAlertDialogBuilder(activity)
            .setTitle("Scan this on the other phone")
            .setView(scrolling(col))
            .setPositiveButton("Done", null)
            .show()
    }

    // -- hosting on this WiFi (step 3) ----------------------------------------------

    private fun stateLine(p: JSONObject): String {
        val state = p.optString("state")
        val detail = p.optString("detail")
        val words = when {
            state.isEmpty() || state == "null" -> "connected, nothing pushed yet"
            state == "sent" -> "sent, no answer yet"
            state == "not reached" -> "not reached: not connected"
            state == "pending" -> "waiting for its PIN"
            state == "scheduled" -> "accepted, switches at the set time"
            state == "applied" || state == "switched" -> "switched"
            state == "rejected" -> "rejected"
            // Only phones on builds before 2026-10-23 report this: the
            // automatic undo is gone.
            state == "reverted" -> "switched back by itself (heard nobody)"
            state == "undone" -> "undone by hand"
            else -> state
        }
        val rev = if (p.isNull("rev")) "" else " (rev ${p.optInt("rev")})"
        val away = if (p.optBoolean("connected")) "" else " — not connected now"
        val more = if (detail.isNotEmpty() && state != "reverted") "\n    $detail" else ""
        return "• ${p.optString("name")}: $words$rev$away$more"
    }

    private fun rosterText(r: JSONObject): String {
        val roster = r.optJSONArray("roster") ?: JSONArray()
        val lines = if (roster.length() == 0) "No phones connected yet. They find this host by " +
            "themselves when they are on the same WiFi and listening (on by default)."
            else (0 until roster.length()).joinToString("\n") { stateLine(roster.getJSONObject(it)) }
        val push = r.optJSONObject("last_push")
        val last = if (push == null) "" else "\n\nLast push: revision ${push.optInt("rev")}" +
            (if (push.isNull("switch_at")) ", applies when accepted" else ", switching at " +
                DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(push.optLong("switch_at") * 1000)))
        return "This host's key: ${fingerprint(r.optString("host_id"))}\n\n$lines$last"
    }

    private fun showHost() {
        py("host_roster") { r ->
            if (r.has("error")) return@py toast(r.optString("error"))
            if (!r.optBoolean("hosting")) {
                MaterialAlertDialogBuilder(activity)
                    .setTitle("Host settings on this WiFi")
                    .setMessage("Phones on the same WiFi (or on this phone's hotspot) that are " +
                        "listening connect to this phone. You then push settings to all of them; " +
                        "each phone still asks for its own PIN unless it trusts this host.\n\n" +
                        "Some WiFi networks (guest WiFi especially) block phones from reaching " +
                        "each other. This phone's own hotspot avoids that.\n\n" +
                        "This phone's key: ${fingerprint(r.optString("host_id"))}\n" +
                        "Other phones show it when they get settings from this one.")
                    .setPositiveButton("Start hosting") { _, _ ->
                        py("host_set", "true") { h ->
                            if (h.has("error")) toast(h.optString("error")) else showHost()
                        }
                    }
                    .setNegativeButton("Cancel", null)
                    .show()
                return@py
            }
            val dialog = MaterialAlertDialogBuilder(activity)
                .setTitle("Hosting: ${r.optString("name")}")
                .setMessage(rosterText(r))
                .setPositiveButton("Push settings…") { _, _ -> showPushOptions() }
                .setNeutralButton("Stop hosting") { _, _ ->
                    py("host_set", "false") { toast("Stopped hosting") }
                }
                .setNegativeButton("Close", null)
                .create()
            // The roster changes as phones answer: redrawn while it is open.
            val refresh = object : Runnable {
                override fun run() {
                    if (!dialog.isShowing) return
                    py("host_roster") { n -> if (!n.has("error") && dialog.isShowing) dialog.setMessage(rosterText(n)) }
                    handler.postDelayed(this, ROSTER_REFRESH_MS)
                }
            }
            dialog.setOnDismissListener { handler.removeCallbacks(refresh) }
            dialog.show()
            handler.postDelayed(refresh, ROSTER_REFRESH_MS)
        }
    }

    private fun showPushOptions() {
        val col = column()
        col.addView(text("Push this phone's settings to every connected phone (and to phones " +
            "that connect later):"))
        val lora = com.google.android.material.checkbox.MaterialCheckBox(activity).apply { text = "LoRa radio: preset, frequency, power"; isChecked = true }
        val cov = com.google.android.material.checkbox.MaterialCheckBox(activity).apply { text = "Coverage model: FM frequency and power, antenna height" }
        val fm = fmCheckbox()
        val pass = com.google.android.material.checkbox.MaterialCheckBox(activity).apply { text = "Network passphrase" }
        val passWarn = text("Encrypted separately to each phone, and used only by phones that " +
            "chose to accept the passphrase over the network. A wrong passphrase cuts a phone off " +
            "from everyone.", 12f, Ui.error(activity)).apply { visibility = View.GONE }
        pass.setOnCheckedChangeListener { _, on -> passWarn.visibility = if (on) View.VISIBLE else View.GONE }
        val fmClearWarn = text("⚠ The FM group turns OFF encryption on every phone that accepts " +
            "it — anyone listening can read their messages.", 12f, Ui.error(activity)).apply {
            visibility = View.GONE
        }
        fm.setOnCheckedChangeListener { _, on ->
            fmClearWarn.visibility = if (on && RadioLink.fmClear(activity)) View.VISIBLE else View.GONE
        }
        listOf(lora, cov, fm, pass, passWarn, fmClearWarn).forEach { col.addView(it) }
        col.addView(fmNote())
        col.addView(text("Switch in how many minutes? Every phone that accepted by then switches " +
            "together; later ones switch as they accept. 0 = each switches when it accepts.", 13f))
        val minutes = EditText(activity).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText("2")
            setSingleLine()
        }
        col.addView(minutes)
        MaterialAlertDialogBuilder(activity)
            .setTitle("Push settings")
            .setView(scrolling(col))
            .setPositiveButton("Push") { _, _ ->
                val m = minutes.text.toString().toIntOrNull()
                    ?: return@setPositiveButton toast("Enter the minutes as a number")
                val opts = JSONObject().put("lora", lora.isChecked)
                    .put("coverage", cov.isChecked).put("fm", fm.isChecked)
                    .put("passphrase", pass.isChecked)
                    .put("switch_in_s", if (m <= 0) JSONObject.NULL else m * 60)
                py("host_push", opts.toString(), SettingsShareUi.kotlinJson(activity)) { r ->
                    if (r.has("error")) toast(r.optString("error"))
                    else {
                        toast("Pushed revision ${r.optInt("rev")} to " +
                            "${r.optJSONArray("roster")?.length() ?: 0} phone(s)")
                        showHost()
                    }
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // -- servers (step 4) ---------------------------------------------------------

    private fun serversText(st: JSONObject): String {
        val servers = st.optJSONArray("servers") ?: JSONArray()
        val lines = if (servers.length() == 0) "No servers yet. A server's admin shows its code " +
            "(settings_server.py card); scan it, or type the address it gives."
            else (0 until servers.length()).joinToString("\n") {
                val s = servers.getJSONObject(it)
                val name = s.optString("name").ifEmpty { s.optString("addr") }
                val state = when {
                    s.optBoolean("connected") -> "connected"
                    s.optString("problem").isNotEmpty() -> "not connected: ${s.optString("problem")}"
                    else -> "connecting…"
                }
                val key = if (s.isNull("host")) "" else "\n    key ${fingerprint(s.optString("host"))}" +
                    when {
                        s.optBoolean("first_use") -> " (kept from its first answer: check it with the admin)"
                        s.optBoolean("pinned") -> ""
                        else -> " (typed address: check it with the admin)"
                    }
                "• $name — ${s.optString("addr")}:${s.optInt("port")} — $state$key"
            }
        val wifi = st.optJSONArray("wifi_hosts") ?: JSONArray()
        val wifiLines = if (wifi.length() == 0) "" else "\n\nHosts on WiFi this phone connects to:\n" +
            (0 until wifi.length()).joinToString("\n") {
                val h = wifi.getJSONObject(it)
                val state = when {
                    h.optBoolean("connected") -> "connected"
                    h.optString("problem").isNotEmpty() -> "not connected: ${h.optString("problem")}"
                    h.optBoolean("heard") -> "connecting…"
                    else -> "not heard now"
                }
                "• ${h.optString("name").ifEmpty { "(no name)" }} — $state\n    key ${fingerprint(h.optString("host"))}"
            }
        val ignored = st.optJSONArray("ignored") ?: JSONArray()
        val ignoredLines = if (ignored.length() == 0) "" else
            "\n\nIgnored hosts (never asked about; Remove… to stop ignoring one):\n" +
            (0 until ignored.length()).joinToString("\n") {
                val h = ignored.getJSONObject(it)
                "• ${h.optString("name").ifEmpty { "(no name)" }} — key ${fingerprint(h.optString("host"))}"
            }
        return "$lines$wifiLines$ignoredLines\n\nThis phone's id, for the server's admin to allow it:\n" +
            fingerprint(st.optString("phone_id"))
    }

    /** Servers this phone connects to: over the internet (mobile data too) or
     *  a network where its beacon does not reach. Pushes from them still need
     *  this phone's PIN unless it trusts them. */
    private fun showServers() {
        py("status", SettingsShareUi.kotlinJson(activity)) { st ->
            if (st.has("error")) return@py toast(st.optString("error"))
            val servers = st.optJSONArray("servers") ?: JSONArray()
            val wifi = st.optJSONArray("wifi_hosts") ?: JSONArray()
            val ignored = st.optJSONArray("ignored") ?: JSONArray()
            val b = MaterialAlertDialogBuilder(activity)
                .setTitle("Servers and hosts")
                .setMessage(serversText(st))
                .setPositiveButton("Scan a server code") { _, _ -> startScan() }
                .setNegativeButton("Type an address") { _, _ -> showTypeServer() }
            if (servers.length() + wifi.length() + ignored.length() > 0) {
                b.setNeutralButton("Remove…") { _, _ -> showRemoveServer(servers, wifi, ignored) }
            }
            val dialog = b.create()
            val refresh = object : Runnable {
                override fun run() {
                    if (!dialog.isShowing) return
                    py("status", SettingsShareUi.kotlinJson(activity)) { n ->
                        if (!n.has("error") && dialog.isShowing) dialog.setMessage(serversText(n))
                    }
                    handler.postDelayed(this, ROSTER_REFRESH_MS)
                }
            }
            dialog.setOnDismissListener { handler.removeCallbacks(refresh) }
            dialog.show()
            handler.postDelayed(refresh, ROSTER_REFRESH_MS)
        }
    }

    private fun showTypeServer() {
        val col = column()
        col.addView(text("The server's address, as its admin gives it: a name or an IP, " +
            "with :port if it is not 47812."))
        val field = EditText(activity).apply {
            hint = "settings.example.org"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI
            setSingleLine()
        }
        col.addView(field)
        col.addView(text("Scanning the server's code is better: it also checks that the right " +
            "server answers at that address.", 12f, Ui.onSurfaceVariant(activity)))
        MaterialAlertDialogBuilder(activity)
            .setTitle("Add a server")
            .setView(col)
            .setPositiveButton("Add") { _, _ -> addServer(field.text.toString()) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun addServer(text: String) {
        py("server_add", text) { r ->
            if (r.has("error")) return@py toast(r.optString("error"))
            val s = r.optJSONObject("server")
            toast("Added ${s?.optString("name")?.ifEmpty { null } ?: s?.optString("addr")}")
            showServers()
        }
    }

    private fun showRemoveServer(servers: JSONArray, wifi: JSONArray, ignored: JSONArray) {
        val items = mutableListOf<Pair<String, () -> Unit>>()
        for (i in 0 until servers.length()) {
            val s = servers.getJSONObject(i)
            items += "${s.optString("name").ifEmpty { s.optString("addr") }} " +
                "(${s.optString("addr")}:${s.optInt("port")})" to {
                py("server_remove", s.optString("addr"), s.optInt("port").toString()) { r ->
                    toast(r.optString("error").ifEmpty { "Removed" })
                }
            }
        }
        for (i in 0 until wifi.length()) {
            val h = wifi.getJSONObject(i)
            items += "${h.optString("name").ifEmpty { "(no name)" }} on WiFi " +
                "(key ${fingerprint(h.optString("host"))})" to {
                py("host_forget", h.optString("host")) { r ->
                    toast(r.optString("error").ifEmpty { "Disconnected. It asks again if heard." })
                }
            }
        }
        // Undo Ignore. Without this an ignored host stayed hidden for good:
        // the list of them was not shown anywhere (found 2026-10-23). It only
        // makes the phone ask again -- connecting is still the operator's answer.
        for (i in 0 until ignored.length()) {
            val h = ignored.getJSONObject(i)
            val name = h.optString("name").ifEmpty { "(no name)" }
            items += "Stop ignoring $name (key ${fingerprint(h.optString("host"))})" to {
                py("host_unignore", h.optString("host")) { r ->
                    when {
                        r.has("error") -> toast(r.optString("error"))
                        r.optBoolean("heard") -> {
                            toast("No longer ignored: $name")
                            showFoundHosts()
                        }
                        else -> toast("No longer ignored: you will be asked when $name is heard")
                    }
                }
            }
        }
        MaterialAlertDialogBuilder(activity)
            .setTitle("Remove which one?")
            .setItems(items.map { it.first }.toTypedArray()) { _, i -> items[i].second() }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // -- hosts heard on WiFi: this phone's operator decides ---------------------

    /** A host was heard on WiFi (from the notification, or at once when the
     *  main screen is showing): ask about the first one waiting. */
    fun showFoundHosts() {
        unseenHost = false
        py("status", SettingsShareUi.kotlinJson(activity)) { st ->
            val found = st.optJSONArray("found") ?: JSONArray()
            if (!st.has("error") && found.length() > 0) showFoundHost(found.getJSONObject(0))
        }
    }

    private fun showFoundHost(f: JSONObject) {
        val name = f.optString("name").ifEmpty { "A host" }
        MaterialAlertDialogBuilder(activity)
            .setTitle("Connect to $name?")
            .setMessage("Heard on this WiFi at ${f.optString("addr")}.\n" +
                "Key ${fingerprint(f.optString("host"))}\n\n" +
                "Check the key with whoever runs it. Once connected it can send settings " +
                "changes; each still needs this phone's PIN unless you trust it. Nothing is " +
                "connected until you say so.")
            .setPositiveButton("Connect") { _, _ ->
                py("host_connect", f.optString("host")) { r ->
                    toast(r.optString("error").ifEmpty { "Connecting to $name" })
                }
            }
            .setNegativeButton("Ignore") { _, _ ->
                py("host_ignore", f.optString("host")) { r ->
                    toast(r.optString("error").ifEmpty {
                        "Ignored: $name is not asked about again. Undo: Share → Servers and hosts → Remove…"
                    })
                }
            }
            .setNeutralButton("Later", null)
            .show()
    }

    // -- receiving ---------------------------------------------------------------

    /** An offer arrived over WiFi: open it (from the notification, or at once
     *  when the main screen is showing). */
    fun showWaitingOffers() {
        unseenOffer = false
        py("status", SettingsShareUi.kotlinJson(activity)) { st ->
            val pending = st.optJSONArray("pending") ?: JSONArray()
            when {
                st.has("error") -> toast(st.optString("error"))
                pending.length() == 1 -> showApproval(pending.getJSONObject(0))
                pending.length() > 1 -> activity.showShareTab()
            }
        }
    }

    /** The scanner's result. */
    fun onScanned(content: String) {
        if (content.trim().startsWith(SERVER_CODE_PREFIX)) return addServer(content)
        py("receive", content, "qr", SettingsShareUi.kotlinJson(activity)) { offer ->
            when {
                offer.has("error") -> toast(offer.optString("error"))
                offer.optString("outcome") == "pending" -> showApproval(offer)
                else -> showResult(offer, offer.optString("outcome"), trusted = true)
            }
        }
    }

    private fun changeLines(offer: JSONObject): String {
        val changes = offer.optJSONArray("changes") ?: JSONArray()
        if (changes.length() == 0) return "Nothing would change: this phone already has these settings."
        return (0 until changes.length()).joinToString("\n") {
            val c = changes.getJSONObject(it)
            val mark = if (c.optBoolean("network")) "  ⚠" else ""
            "• ${c.optString("label")}: ${c.opt("old") ?: "—"} → ${c.optString("new")}$mark"
        }
    }

    /** True when this offer turns "send FM unencrypted" ON -- the one shared
     *  change worth an extra-loud warning, because it removes encryption. */
    private fun turnsOffEncryption(offer: JSONObject): Boolean {
        val changes = offer.optJSONArray("changes") ?: return false
        for (i in 0 until changes.length()) {
            val c = changes.getJSONObject(i)
            if (c.optString("key") == "fm_clear" &&
                c.optString("new").lowercase() in listOf("true", "yes", "1")) return true
        }
        return false
    }

    private fun showApproval(offer: JSONObject) {
        py("status", SettingsShareUi.kotlinJson(activity)) { st ->
            val hasPin = st.optBoolean("has_pin")
            val col = column()
            // "network", not "WiFi": a server may be reached over mobile data.
            val via = if (offer.optString("route") == "network") "over the network" else "by QR code"
            col.addView(text("Sent $via, revision ${offer.optInt("rev")}" +
                (switchTime(offer)?.let { ", switching at $it" } ?: "") +
                ".\nHost key ${fingerprint(offer.optString("host"))} — check it matches the " +
                "key on the host's screen.", 12f, Ui.onSurfaceVariant(activity)))
            col.addView(text(changeLines(offer), 13f))
            val changes = offer.optJSONArray("changes") ?: JSONArray()
            if ((0 until changes.length()).any { changes.getJSONObject(it).optBoolean("network") }) {
                col.addView(text("⚠ Changes this phone's network: it stops hearing phones that do " +
                    "not change too. It keeps the new settings until you press Undo on the Share tab.",
                    12f, Ui.error(activity)))
            }
            if (turnsOffEncryption(offer)) {
                col.addView(text("⚠ This turns OFF encryption on FM: anyone listening with a " +
                    "receiver can read every message. Only accept if you mean to operate " +
                    "unencrypted (for example, where the rules require it).", 13f, Ui.error(activity)))
            }
            val notes = offer.optJSONArray("notes") ?: JSONArray()
            for (i in 0 until notes.length()) col.addView(text("Note: ${notes.optString(i)}", 12f, Ui.onSurfaceVariant(activity)))
            val pin = pinField(if (hasPin) "This phone's PIN" else "New PIN for this phone (4-12 digits)")
            val confirm = pinField("Type the new PIN again")
            col.addView(pin)
            if (!hasPin) col.addView(confirm)
            val trust = com.google.android.material.checkbox.MaterialCheckBox(activity).apply {
                text = "Trust ${offer.optString("host_name")} from now on (its future changes apply without asking)"
                textSize = 12f
            }
            col.addView(trust)
            val dialog = MaterialAlertDialogBuilder(activity)
                .setTitle("Settings from ${offer.optString("host_name")}")
                .setView(scrolling(col))
                .setPositiveButton("Accept", null)
                .setNegativeButton("Reject") { _, _ ->
                    py("reject", offer.optString("host")) { toast("Rejected") }
                }
                .setNeutralButton("Later", null)
                .create()
            dialog.setOnShowListener {
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                    val p = pin.text.toString()
                    fun accept() = py("approve", offer.optString("host"), p, trust.isChecked.toString(),
                        SettingsShareUi.kotlinJson(activity)) { r ->
                        if (r.has("error")) toast(r.optString("error"))
                        else {
                            dialog.dismiss()
                            showResult(offer, r.optString("outcome"), trusted = false)
                        }
                    }
                    if (hasPin) accept()
                    else if (p != confirm.text.toString()) toast("The two PINs differ")
                    else py("set_pin", p, "") { r ->
                        if (r.has("error")) toast(r.optString("error")) else accept()
                    }
                }
            }
            dialog.show()
        }
    }

    private fun showResult(offer: JSONObject, outcome: String, trusted: Boolean) {
        val head = when {
            outcome == "applied" && trusted ->
                "Applied from ${offer.optString("host_name")} (a trusted host):"
            outcome == "applied" -> "Applied:"
            outcome == "scheduled" -> "Accepted. It switches at the time the host set:"
            else -> "Not applied — $outcome. Nothing was changed:"
        }
        val b = MaterialAlertDialogBuilder(activity)
            .setTitle("Shared settings")
            .setMessage(head + "\n\n" + changeLines(offer) +
                if (turnsOffEncryption(offer))
                    "\n\n⚠ FM is now sent UNENCRYPTED: anyone listening can read every message."
                else "")
            .setPositiveButton("OK", null)
        if (outcome == "applied") b.setNeutralButton("Undo") { _, _ -> confirmUndo() }
        b.show()
        // A changed coverage setting shows on the Coverage tab's next run.
        activity.onSharedSettingsApplied()
    }

    private fun confirmUndo() {
        MaterialAlertDialogBuilder(activity)
            .setTitle("Undo the last shared change?")
            .setMessage("Puts back the settings this phone had before it.")
            .setPositiveButton("Undo") { _, _ ->
                py("undo", SettingsShareUi.kotlinJson(activity)) { r ->
                    val problems = r.optJSONArray("problems")
                    when {
                        r.has("error") -> toast(r.optString("error"))
                        problems != null && problems.length() > 0 -> toast(problems.join("; "))
                        else -> { toast("Previous settings restored"); activity.onSharedSettingsApplied() }
                    }
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // -- PIN and trusted hosts ---------------------------------------------------

    private fun showSecurity(st: JSONObject) {
        val hasPin = st.optBoolean("has_pin")
        val trusted = st.optJSONArray("trusted") ?: JSONArray()
        val items = mutableListOf<Pair<String, () -> Unit>>()
        items += (if (hasPin) "Change PIN" else "Set a PIN") to { showSetPin(hasPin) }
        val over = st.optBoolean("accept_passphrase_over_network")
        items += (if (over) "Stop accepting the passphrase over the network"
                  else "Accept the network passphrase over the network") to {
            askPin("Confirm with this phone's PIN") { p ->
                py("set_accept_passphrase_over_network", (!over).toString(), p) { r ->
                    toast(r.optString("error").ifEmpty { "Saved" })
                }
            }
        }
        for (i in 0 until trusted.length()) {
            val t = trusted.getJSONObject(i)
            items += "Stop trusting ${t.optString("name")} (key ${fingerprint(t.optString("host"))})" to {
                askPin("Stop trusting ${t.optString("name")}") { p ->
                    py("untrust_host", t.optString("host"), p) { r ->
                        toast(r.optString("error").ifEmpty { "No longer trusted" })
                    }
                }
            }
        }
        if (hasPin) items += "Forgot the PIN? Reset it" to { confirmResetPin() }
        // No setMessage here: a dialog with a message AND items shows only
        // the message, which left this one with no options at all (seen on
        // the OnePlus, 2026-10-22). The trusted hosts are the "Stop trusting"
        // items; the title says when there are none.
        MaterialAlertDialogBuilder(activity)
            .setTitle(if (trusted.length() == 0) "PIN and trusted hosts (none trusted)"
                      else "PIN and trusted hosts")
            .setItems(items.map { it.first }.toTypedArray()) { _, i -> items[i].second() }
            .setNegativeButton("Close", null)
            .show()
    }

    private fun askPin(title: String, then: (String) -> Unit) {
        val f = pinField("PIN")
        val col = column().apply { addView(f) }
        MaterialAlertDialogBuilder(activity)
            .setTitle(title)
            .setView(col)
            .setPositiveButton("OK") { _, _ -> then(f.text.toString()) }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun showSetPin(hasPin: Boolean) {
        val col = column()
        val old = pinField("Current PIN")
        val new = pinField("New PIN (4-12 digits)")
        val again = pinField("New PIN again")
        if (hasPin) col.addView(old)
        col.addView(new)
        col.addView(again)
        MaterialAlertDialogBuilder(activity)
            .setTitle(if (hasPin) "Change PIN" else "Set a PIN")
            .setView(col)
            .setPositiveButton("Save") { _, _ ->
                if (new.text.toString() != again.text.toString()) return@setPositiveButton toast("The two PINs differ")
                py("set_pin", new.text.toString(), old.text.toString()) { r ->
                    toast(r.optString("error").ifEmpty { "PIN saved" })
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    private fun confirmResetPin() {
        MaterialAlertDialogBuilder(activity)
            .setTitle("Reset the PIN?")
            .setMessage("This also forgets every trusted host, and drops any change accepted but " +
                "not yet switched. Then set a new PIN.")
            .setPositiveButton("Reset") { _, _ ->
                py("reset_pin") { r -> toast(r.optString("error").ifEmpty { "PIN reset — set a new one" }) }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }
}
