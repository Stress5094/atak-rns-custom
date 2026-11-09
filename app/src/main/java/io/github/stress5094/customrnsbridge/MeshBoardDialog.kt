package io.github.stress5094.customrnsbridge

import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Color
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.PersistableBundle
import android.text.InputFilter
import android.text.InputType
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.appcompat.app.AppCompatActivity
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * The settings of a Meshtastic board, changed from here so the Meshtastic app is
 * not needed to set one up: region, modulation preset, frequency slot, transmit
 * power, whether the board repeats other nodes' packets, and channel 0's name
 * and key. Replaces the RNode "Radio" screen when the board runs Meshtastic.
 *
 * All the logic is in Python (mt_config.py, rns_bridge.py); this only draws it.
 * Three rules that came from using it on real boards:
 *
 *  - NOTHING IS APPLIED WITHOUT BEING SHOWN FIRST. "Review changes" asks Python
 *    what would change (preview_mesh_board_config) and lists it, because these
 *    are changes to a radio's key and frequency and a wrong one silently stops
 *    two boards hearing each other.
 *  - THE KEY IS NEVER SHOWN. The screen says what KIND of key the channel has.
 *    The key itself only ever appears inside a share link, on request, behind
 *    a warning, and the clipboard copy is marked sensitive.
 *  - "APPLIED" MEANS THE BOARD SAYS SO. Python reconnects and reads the board
 *    back (and waits out a restart when the change causes one) before it
 *    reports, so the result line is what the board holds, not what was sent.
 */
class MeshBoardDialog(
    private val activity: AppCompatActivity,
    private val call: (String, Array<String>) -> String?,
    private val onDone: () -> Unit = {},
    // Opens the app's own preset / frequency / power screen, which is where the
    // managed profile's radio half comes from.
    private val onEditAppRadio: () -> Unit = {}
) {
    private val handler = Handler(Looper.getMainLooper())
    // A cached pool, never shut down by hand: the form closes before the apply
    // step it launches has run, and idle threads here expire on their own.
    private val executor = Executors.newCachedThreadPool()

    // What the operator has chosen but not yet applied.
    private var pendingKey: String? = null
    private var pendingUrl: String? = null

    // The form, kept so the request can be built from it.
    private lateinit var regions: List<String>
    private lateinit var presets: List<String>
    private lateinit var repeatValues: List<String>
    private lateinit var regionSpinner: Spinner
    private lateinit var presetSpinner: Spinner
    private lateinit var repeatSpinner: Spinner
    private lateinit var slotField: EditText
    private lateinit var overrideField: EditText
    private lateinit var powerField: EditText
    private lateinit var nameField: EditText
    private lateinit var keyStatus: TextView
    private lateinit var linkStatus: TextView
    // The open form, so a button inside it can close it once an apply starts.
    private var dialogRef: AlertDialog? = null

    fun show() {
        Toast.makeText(activity, "Reading the board…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val reply = parse(call("get_mesh_board_config", emptyArray()))
            handler.post {
                if (reply == null || !reply.optBoolean("ok")) {
                    message(
                        "Meshtastic board",
                        reply?.optString("reason").orEmpty().ifEmpty { "The board's settings are unavailable" }
                    )
                } else {
                    build(reply).show()
                }
            }
        }
    }

    private fun parse(text: String?): JSONObject? = try {
        if (text.isNullOrEmpty()) null else JSONObject(text)
    } catch (e: Exception) {
        null
    }

    private fun strings(array: JSONArray?): List<String> =
        (0 until (array?.length() ?: 0)).map { array!!.optString(it) }

    private fun heading(text: String) = TextView(activity).apply {
        this.text = text
        textSize = 12f
        setTextColor(Ui.res(activity, R.color.status_heading))
        setPadding(0, dp(12), 0, 0)
    }

    private fun spinner(items: List<String>, selected: Int) = Spinner(activity).apply {
        adapter = ArrayAdapter(activity, android.R.layout.simple_spinner_dropdown_item, items)
        setSelection(selected.coerceAtLeast(0))
    }

    private fun build(config: JSONObject): AlertDialog {
        val snap = config.getJSONObject("snapshot")
        val layout = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(12), dp(24), 0)
        }

        // The ONE-TAP APPLY, first because it is what most people want: every
        // board set from the same passphrase and the same radio settings, the
        // way an RNode is. The key is derived in Python and never comes here.
        layout.addView(Button(activity).apply {
            text = "Use the app's settings"
            setOnClickListener { reviewProfile(dialogRef) }
        })
        layout.addView(Button(activity).apply {
            text = "App's radio settings…"
            setOnClickListener {
                dialogRef?.dismiss()
                onEditAppRadio()
            }
        })
        layout.addView(TextView(activity).apply {
            text = "Sets this board's preset, frequency and power from the app's radio " +
                "settings, its channel from the mesh passphrase, and turns repeating off. " +
                "Every phone with the same passphrase sets its board the same way."
            textSize = 12f
            setTextColor(Ui.res(activity, R.color.status_idle))
        })

        // Region. A board with none set will not transmit at all, which looks
        // exactly like a dead radio, so it leads the form.
        val boardRegion = snap.optString("region")
        val regionList = strings(config.optJSONArray("regions"))
        val regionItems = if (boardRegion == "UNSET") listOf("(not set - choose one)") + regionList else regionList
        regions = if (boardRegion == "UNSET") listOf("") + regionList else regionList
        layout.addView(heading("Region (a board with none set will not transmit)"))
        regionSpinner = spinner(regionItems, regions.indexOf(boardRegion))
        layout.addView(regionSpinner)

        // Preset. A board can be on one the app has no name for; that is shown
        // and left alone unless a different one is picked.
        val presetNames = strings(config.optJSONArray("presets"))
        val presetLabels = strings(config.optJSONArray("preset_labels"))
            .takeIf { it.size == presetNames.size } ?: presetNames
        val boardPreset = snap.optString("preset_name", "")
        val unknownPreset = !snap.optBoolean("use_preset") || boardPreset.isEmpty() || boardPreset == "null"
        presets = if (unknownPreset) listOf("") + presetNames else presetNames
        val presetItems = if (unknownPreset) {
            listOf("(board is on ${snap.optString("preset")} - leave as is)") + presetLabels
        } else presetLabels
        layout.addView(heading("Preset (every board must match)"))
        presetSpinner = spinner(presetItems, presets.indexOf(boardPreset))
        layout.addView(presetSpinner)

        layout.addView(heading("Frequency slot (0 = automatic)"))
        slotField = EditText(activity).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
            setText(snap.optInt("channel_num").toString())
        }
        layout.addView(slotField)

        layout.addView(heading("Frequency override in MHz (0 = none; takes precedence over the slot)"))
        overrideField = EditText(activity).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            setSingleLine()
            setText(snap.optDouble("override_frequency_mhz").let { if (it == 0.0) "0" else it.toString() })
        }
        layout.addView(overrideField)

        layout.addView(heading("Transmit power (dBm, max ${config.optInt("tx_power_max", 30)}; 0 = board default)"))
        powerField = EditText(activity).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
            setText(snap.optInt("tx_power_dbm").toString())
        }
        layout.addView(powerField)

        // Repeating. "Never repeat" on every board is the clean way to keep a
        // board from adding its own flood on top of this app's relay logic.
        val choices = config.optJSONArray("rebroadcast_choices")
        val values = (0 until (choices?.length() ?: 0)).map { choices!!.getJSONObject(it).optString("value") }
        val labels = (0 until (choices?.length() ?: 0)).map { choices!!.getJSONObject(it).optString("label") }
        val boardRepeat = snap.optString("rebroadcast")
        val knownRepeat = boardRepeat in values
        repeatValues = if (knownRepeat) values else listOf("") + values
        val repeatItems = if (knownRepeat) labels else listOf("(board is on $boardRepeat - leave as is)") + labels
        layout.addView(heading("Repeating other nodes' packets"))
        repeatSpinner = spinner(repeatItems, repeatValues.indexOf(boardRepeat))
        layout.addView(repeatSpinner)

        val channel = snap.getJSONObject("channel")
        layout.addView(heading("Channel name (up to ${config.optInt("name_max", 11)} characters)"))
        nameField = EditText(activity).apply {
            setSingleLine()
            filters = arrayOf(InputFilter.LengthFilter(config.optInt("name_max", 11)))
            setText(channel.optString("name"))
        }
        layout.addView(nameField)

        layout.addView(heading("Channel key"))
        keyStatus = TextView(activity).apply { text = channel.optString("key_description") }
        layout.addView(keyStatus)
        val keyRow = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        keyRow.addView(Button(activity).apply {
            text = "New random key"
            setOnClickListener {
                pendingKey = "random"
                keyStatus.text = "A new random 256-bit key will be set. Share it to the other boards afterwards."
            }
        })
        keyRow.addView(Button(activity).apply {
            text = "Public default"
            setOnClickListener {
                pendingKey = "default"
                keyStatus.text = "The public default key will be set - anyone can read this channel."
            }
        })
        layout.addView(keyRow)

        layout.addView(heading("Share these settings with another board"))
        linkStatus = TextView(activity).apply {
            textSize = 12f
            setTextColor(Ui.res(activity, R.color.status_idle))
        }
        val linkRow = LinearLayout(activity).apply { orientation = LinearLayout.HORIZONTAL }
        linkRow.addView(Button(activity).apply {
            text = "Show link"
            setOnClickListener { showShareLink() }
        })
        linkRow.addView(Button(activity).apply {
            text = "Paste link"
            setOnClickListener { pasteLink() }
        })
        layout.addView(linkRow)
        layout.addView(linkStatus)

        layout.addView(TextView(activity).apply {
            text = "Every board on a network needs the same region, preset, " +
                "frequency slot and channel key, or they will not hear each " +
                "other. Must be legal where you are - receiving is not transmitting."
            textSize = 12f
            setTextColor(Ui.res(activity, R.color.status_idle))
            setPadding(0, dp(12), 0, dp(12))
        })

        val dialog = MaterialAlertDialogBuilder(activity)
            .setTitle("Meshtastic board")
            .setView(ScrollView(activity).apply { addView(layout) })
            .setNegativeButton("Close", null)
            .setPositiveButton("Review changes", null) // wired below: stays open until confirmed
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener { review(dialog) }
        }
        dialogRef = dialog
        return dialog
    }

    /** "Use the app's settings": list what would change, then apply on a yes. */
    private fun reviewProfile(form: AlertDialog?) {
        Toast.makeText(activity, "Checking…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val reply = parse(call("preview_mesh_board_profile", emptyArray()))
            handler.post {
                if (reply == null || !reply.optBoolean("ok")) {
                    message("Cannot apply", reply?.optString("message").orEmpty().ifEmpty { "No answer from the board" })
                    return@post
                }
                val changes = reply.optJSONArray("changes")
                if (changes == null || changes.length() == 0) {
                    Toast.makeText(activity, "The board is already on the app's settings", Toast.LENGTH_LONG).show()
                    return@post
                }
                val lines = (0 until changes.length()).map {
                    val c = changes.getJSONObject(it)
                    "${c.optString("label")}:\n    ${c.opt("old")}  →  ${c.opt("new")}"
                }
                MaterialAlertDialogBuilder(activity)
                    .setTitle("Use the app's settings?")
                    .setMessage(lines.joinToString("\n") +
                        "\n\nBoards set this way from the same passphrase hear each other; " +
                        "a board set any other way will not. The board may restart.")
                    .setNegativeButton("Back", null)
                    .setPositiveButton("Apply") { _, _ -> apply("apply_mesh_board_profile", emptyArray(), form) }
                    .show()
            }
        }
    }

    /** The form as a request. Python works out what actually differs, so every
     * field is sent; only a key or a link the operator chose is added. */
    private fun request(): JSONObject {
        val r = JSONObject()
        // A link carries its own channel and radio settings; sending the form's
        // values as well would override the link with the board's current ones.
        val url = pendingUrl
        if (url != null) {
            r.put("url", url)
        } else {
            regions.getOrNull(regionSpinner.selectedItemPosition)?.takeIf { it.isNotEmpty() }
                ?.let { r.put("region", it) }
            presets.getOrNull(presetSpinner.selectedItemPosition)?.takeIf { it.isNotEmpty() }
                ?.let { r.put("preset", it) }
            slotField.text.toString().trim().takeIf { it.isNotEmpty() }?.let { r.put("channel_num", it) }
            overrideField.text.toString().trim().takeIf { it.isNotEmpty() }
                ?.let { r.put("override_frequency_mhz", it) }
            r.put("channel_name", nameField.text.toString())
            pendingKey?.let { r.put("psk", it) }
        }
        powerField.text.toString().trim().takeIf { it.isNotEmpty() }?.let { r.put("tx_power_dbm", it) }
        repeatValues.getOrNull(repeatSpinner.selectedItemPosition)?.takeIf { it.isNotEmpty() }
            ?.let { r.put("rebroadcast", it) }
        return r
    }

    private fun review(form: AlertDialog) {
        val req = request().toString()
        Toast.makeText(activity, "Checking…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val reply = parse(call("preview_mesh_board_config", arrayOf(req)))
            handler.post {
                if (reply == null || !reply.optBoolean("ok")) {
                    message("Cannot apply", reply?.optString("message").orEmpty().ifEmpty { "No answer from the board" })
                    return@post
                }
                val changes = reply.optJSONArray("changes")
                if (changes == null || changes.length() == 0) {
                    Toast.makeText(activity, "Nothing to change - the board already has these settings", Toast.LENGTH_LONG).show()
                    return@post
                }
                val lines = (0 until changes.length()).map {
                    val c = changes.getJSONObject(it)
                    "${c.optString("label")}:\n    ${c.opt("old")}  →  ${c.opt("new")}"
                }
                val affectsMatching = (0 until changes.length()).any {
                    changes.getJSONObject(it).optString("field") !in setOf("tx_power_dbm", "rebroadcast")
                }
                val warning = if (affectsMatching)
                    "\n\nThe other boards on this network must be given the same region, preset, " +
                        "slot and key afterwards, or they will stop hearing this one. The board may restart."
                else ""
                MaterialAlertDialogBuilder(activity)
                    .setTitle("Change these settings?")
                    .setMessage(lines.joinToString("\n") + warning)
                    .setNegativeButton("Back", null)
                    .setPositiveButton("Apply") { _, _ -> apply("set_mesh_board_config", arrayOf(req), form) }
                    .show()
            }
        }
    }

    private fun apply(function: String, args: Array<String>, form: AlertDialog?) {
        form?.dismiss()
        val progress = MaterialAlertDialogBuilder(activity)
            .setTitle("Applying")
            .setMessage("Changing the board's settings and reading them back. If the board " +
                "restarts this can take up to half a minute.")
            .setCancelable(false)
            .show()
        executor.execute {
            val reply = parse(call(function, args))
            handler.post {
                progress.dismiss()
                val ok = reply?.optBoolean("ok") ?: false
                // "unconfirmed": the board took the values and restarted, and
                // has not come back yet to show it kept them. Neither "Done"
                // nor "Not applied" is true of that.
                val unconfirmed = reply?.optBoolean("unconfirmed") ?: false
                var text = reply?.optString("message").orEmpty().ifEmpty { "No answer from the board" }
                UsbSerialBridgeService.awaitingPermissionFor?.let {
                    text += "\n\nAndroid is waiting for permission to use $it - " +
                        "tap OK on its prompt."
                }
                message(
                    when {
                        ok -> "Done"
                        unconfirmed -> "Sent, not yet confirmed"
                        else -> "Not applied"
                    },
                    text
                )
                onDone()
            }
        }
    }

    private fun showShareLink() {
        executor.execute {
            val url = call("get_mesh_board_url", emptyArray()).orEmpty()
            handler.post {
                if (url.isEmpty()) {
                    message("Share link", "The board is not connected right now")
                    return@post
                }
                val text = TextView(activity).apply {
                    this.text = url
                    textSize = 12f
                    setTextIsSelectable(true)
                    setPadding(dp(24), dp(12), dp(24), 0)
                }
                MaterialAlertDialogBuilder(activity)
                    .setTitle("Share link - contains the channel key")
                    .setMessage("Anyone who has this link can join this network. Open it in the Meshtastic " +
                        "app, or paste it into another board's settings here.")
                    .setView(text)
                    .setPositiveButton("Copy") { _, _ -> copy(url) }
                    .setNegativeButton("Close", null)
                    .show()
            }
        }
    }

    private fun copy(url: String) {
        val clip = ClipData.newPlainText("Meshtastic share link", url)
        // Hidden from clipboard previews on Android 13+: it holds a key.
        if (Build.VERSION.SDK_INT >= 33) {
            clip.description.extras = PersistableBundle().apply {
                putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true)
            }
        }
        (activity.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(clip)
        Toast.makeText(activity, "Link copied", Toast.LENGTH_SHORT).show()
    }

    private fun pasteLink() {
        val field = EditText(activity).apply {
            hint = "https://meshtastic.org/e/#…"
            setPadding(dp(24), dp(12), dp(24), dp(12))
        }
        MaterialAlertDialogBuilder(activity)
            .setTitle("Paste a share link")
            .setMessage("Sets this board's channel, preset and region from the link.")
            .setView(field)
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Use link") { _, _ ->
                val url = field.text.toString().trim()
                if (url.contains("/#")) {
                    pendingUrl = url
                    pendingKey = null
                    linkStatus.text = "A link is loaded: Review will set the channel, preset and region from it."
                } else {
                    Toast.makeText(activity, "That is not a Meshtastic share link", Toast.LENGTH_LONG).show()
                }
            }
            .show()
    }

    private fun message(title: String, text: String) {
        MaterialAlertDialogBuilder(activity)
            .setTitle(title)
            .setMessage(text)
            .setPositiveButton("OK", null)
            .show()
    }

    private fun dp(value: Int) = (value * activity.resources.displayMetrics.density).toInt()
}
