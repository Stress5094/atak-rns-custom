package io.github.stress5094.customrnsbridge

import android.app.Activity
import android.graphics.Typeface
import android.text.InputType
import android.util.Log
import android.view.View
import android.widget.AdapterView
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import org.json.JSONArray
import org.json.JSONObject

/**
 * The editor for the "Custom" FM choice: the operator's own tones, rate, code
 * and keying, for radios none of the presets were measured on.
 *
 * ALL THE RULES ARE PYTHON'S (fm_custom.py), asked live: which rates and codes
 * exist, whether a setting can be built, what it costs, and what the record
 * has against it. Nothing here restates a limit, because a second copy of a
 * codec2 limit in Kotlin is a copy that can drift -- and the failure it would
 * hide is a process abort, not an error message.
 *
 * Pickers and steppers rather than free text, so a typo cannot reach the
 * radio. The one typed field is the net code, which is the point of having
 * one: a code read out by another operator gives the same net settings here.
 *
 * Errors block Save; warnings do not. A warning is something legal that the
 * bench record has evidence against, and the operator may know their radios
 * better than the record does.
 */
class FmCustomDialog(
    private val activity: Activity,
    initialSpec: String,
    private val onSaved: (String) -> Unit
) {
    companion object {
        private const val TAG = "FmCustomDialog"
        private const val TONE_STEP_HZ = 50
        private const val LEAD_STEP_S = 0.05
    }

    private val colorIdle by lazy { Ui.res(activity, R.color.status_idle) }
    private val colorWarn by lazy { Ui.res(activity, R.color.status_warn) }
    private val colorBad by lazy { Ui.res(activity, R.color.status_bad) }
    private val colorGood by lazy { Ui.res(activity, R.color.status_good) }

    private val py: PyObject by lazy {
        // The dialog can open before the bridge has started Python (the
        // settings screen is reachable with no link running), and fm_custom
        // needs no native library, so starting it here is safe.
        if (!Python.isStarted()) {
            Python.start(com.chaquo.python.android.AndroidPlatform(activity.applicationContext))
        }
        Python.getInstance().getModule("fm_custom")
    }

    private var spec: JSONObject = parseOrDefault(initialSpec)
    private var described = JSONObject()
    private var rates: List<Int> = emptyList()
    private var codes: List<JSONObject> = emptyList()
    private var maxLeadInS = 3.0
    private var dialog: AlertDialog? = null

    // Views updated on every change.
    private lateinit var netCodeView: TextView
    private lateinit var previewView: TextView
    private lateinit var problemView: TextView
    private lateinit var evidenceView: TextView
    private lateinit var firstToneValue: TextView
    private lateinit var spacingValue: TextView
    private lateinit var leadInValue: TextView
    private lateinit var rateSpinner: Spinner
    private lateinit var codeSpinner: Spinner
    private lateinit var tonesGroup: RadioGroup
    private lateinit var copiesGroup: RadioGroup
    private lateinit var keyingGroup: RadioGroup
    private lateinit var deemphGroup: RadioGroup
    // Set while the form writes its own controls, so their listeners do not
    // read the redraw back as an operator's edit.
    private var syncing = false

    private fun parseOrDefault(text: String): JSONObject {
        if (text.isNotBlank()) {
            try { return JSONObject(text) } catch (e: Exception) {
                Log.w(TAG, "stored custom FM settings unreadable, starting from the default", e)
            }
        }
        return JSONObject(py.callAttr("preset_spec_json", "fast").toString())
    }

    fun show() {
        loadChoices()
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), 0)
        }

        netCodeView = TextView(activity).apply {
            textSize = 18f
            setTypeface(Typeface.MONOSPACE, Typeface.BOLD)
            setTextIsSelectable(true)
        }
        body.addView(heading("Net code"))
        body.addView(netCodeView)
        body.addView(note("Every radio on the net must show the same code. A " +
            "different rate, tone count or code is total silence, with no error " +
            "at either end."))
        body.addView(buildNetCodeEntry())

        body.addView(heading("Start from"))
        body.addView(LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            RadioLink.TONE_PROFILES.forEach { name ->
                addView(compact(Ui.outlined(activity, name.replaceFirstChar { it.uppercase() })).apply {
                    layoutParams = LinearLayout.LayoutParams(0,
                        LinearLayout.LayoutParams.WRAP_CONTENT, 1f).apply { marginEnd = dp(4) }
                    setOnClickListener { startFrom(name) }
                })
            }
        })
        body.addView(Ui.outlined(activity, "Measure my radios\u2026").apply {
            // The tone sweep (FmTuneDialog): measures the radios' audio path
            // and offers settings that fit; picking one lands here, where its
            // warnings still show and it still needs Save.
            setOnClickListener {
                FmTuneDialog(activity) { specJson ->
                    spec = JSONObject(specJson)
                    loadChoices()
                    fillRateSpinner()
                    refresh()
                }.show()
            }
        })

        body.addView(heading("Tones"))
        tonesGroup = radioRow(listOf("2 tones" to 2, "4 tones" to 4)) { v ->
            edit { put("tones", v) }
            loadChoices()
            fillRateSpinner()
        }
        body.addView(tonesGroup)

        body.addView(heading("Symbol rate (tone changes per second)"))
        rateSpinner = Spinner(activity)
        rateSpinner.onItemSelectedListener = onPick { i ->
            // Spinners report selections after layout, so the redraw's own
            // setSelection arrives here too: only a different value is an edit.
            rates.getOrNull(i)?.takeIf { it != spec.optInt("symbol_rate_bd") }
                ?.let { r -> edit { put("symbol_rate_bd", r) } }
        }
        body.addView(rateSpinner)

        body.addView(heading("First tone"))
        firstToneValue = TextView(activity)
        body.addView(stepper(firstToneValue,
            { edit { put("first_tone_hz", optInt("first_tone_hz") - TONE_STEP_HZ) } },
            { edit { put("first_tone_hz", optInt("first_tone_hz") + TONE_STEP_HZ) } }))

        body.addView(heading("Tone spacing"))
        spacingValue = TextView(activity)
        body.addView(stepper(spacingValue,
            { edit { put("tone_spacing_hz", optInt("tone_spacing_hz") - TONE_STEP_HZ) } },
            { edit { put("tone_spacing_hz", optInt("tone_spacing_hz") + TONE_STEP_HZ) } },
            Ui.textButton(activity, "= rate").apply {
                // The tightest legal spacing: tones closer than the rate are
                // not separable, and wider pushes the top tone up for nothing.
                setOnClickListener {
                    edit { put("tone_spacing_hz", optInt("symbol_rate_bd")) }
                }
            }))

        body.addView(heading("Error-correction code"))
        codeSpinner = Spinner(activity)
        codeSpinner.adapter = ArrayAdapter(activity,
            android.R.layout.simple_spinner_dropdown_item,
            codes.map {
                "${it.optInt("capacity_bytes")} B per burst, rate ${it.optString("rate")}"
            })
        codeSpinner.onItemSelectedListener = onPick { i ->
            codes.getOrNull(i)?.optString("codename")
                ?.takeIf { it != spec.optString("codename") }
                ?.let { c -> edit { put("codename", c) } }
        }
        body.addView(codeSpinner)

        body.addView(heading("This radio only (need not match the others)"))
        body.addView(note("Copies per burst"))
        copiesGroup = radioRow(listOf("1" to 1, "2" to 2, "3" to 3)) { v ->
            edit { put("repeats", v) }
        }
        body.addView(copiesGroup)

        body.addView(note("How the radio is keyed"))
        keyingGroup = radioRow(listOf("PTT pin (Digirig)" to "cm108",
                                      "VOX (no PTT pin)" to "vox")) { v ->
            edit { put("keying", v) }
        }
        body.addView(keyingGroup)

        body.addView(note("Lead-in (tone sent before the data, so the radio is on air in time)"))
        leadInValue = TextView(activity)
        body.addView(stepper(leadInValue,
            { stepLeadIn(-LEAD_STEP_S) }, { stepLeadIn(LEAD_STEP_S) },
            Ui.textButton(activity, "Default").apply {
                setOnClickListener { edit { put("lead_in_s", JSONObject.NULL) } }
            }))

        body.addView(note("Receive de-emphasis (a K1 PRO cable receiving needs it)"))
        deemphGroup = radioRow(listOf("Default for keying" to null, "Off" to 0.0,
                                      "250 µs" to 250.0)) { v ->
            edit { put("deemphasis_us", v ?: JSONObject.NULL) }
        }
        body.addView(deemphGroup)

        body.addView(heading("What this does"))
        problemView = TextView(activity).apply { textSize = 12f }
        body.addView(problemView)
        previewView = TextView(activity).apply {
            textSize = 12f
            setTextColor(colorIdle)
        }
        body.addView(previewView)
        evidenceView = TextView(activity).apply {
            textSize = 12f
            setTextColor(colorIdle)
            setPadding(0, dp(4), 0, dp(8))
        }
        body.addView(evidenceView)

        fillRateSpinner()
        refresh()

        dialog = MaterialAlertDialogBuilder(activity)
            .setTitle("Custom FM settings")
            .setView(ScrollView(activity).apply { addView(body) })
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Use these", null) // wired below so it can refuse
            .create()
        dialog?.setOnShowListener {
            dialog?.getButton(AlertDialog.BUTTON_POSITIVE)?.setOnClickListener { save() }
        }
        dialog?.show()
    }

    // -- editing -------------------------------------------------------------

    /** Apply one change and redraw. Any edit drops the measured evidence: it
     * describes the settings that were measured, not these. */
    private fun edit(change: JSONObject.() -> Unit) {
        if (syncing) return
        spec.change()
        spec.remove("evidence")
        refresh()
    }

    private fun stepLeadIn(delta: Double) {
        // From "default" the first step starts at the default's value, which
        // describe() reports resolved.
        val now = if (spec.isNull("lead_in_s")) described.optDouble("lead_in_s", 0.1)
                  else spec.optDouble("lead_in_s")
        val next = Math.round((now + delta) / LEAD_STEP_S) * LEAD_STEP_S
        edit { put("lead_in_s", next.coerceIn(0.0, maxLeadInS)) }
    }

    private fun startFrom(preset: String) {
        val text = py.callAttr("preset_spec_json", preset).toString()
        if (text.isEmpty()) return
        spec = JSONObject(text)
        loadChoices()
        fillRateSpinner()
        refresh()
    }

    private fun buildNetCodeEntry(): View {
        val field = EditText(activity).apply {
            hint = "Type another radio's net code"
            textSize = 13f
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        return LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(field)
            addView(Ui.tonal(activity, "Use").apply {
                setOnClickListener {
                    val got = JSONObject(
                        py.callAttr("spec_from_net_code_json", field.text.toString()).toString())
                    val parsed = got.optJSONObject("spec")
                    if (parsed == null) {
                        Toast.makeText(activity, got.optString("error"), Toast.LENGTH_LONG).show()
                        return@setOnClickListener
                    }
                    // Only the NET settings come from a code; this radio's own
                    // keying, lead-in, filter and copies stay as they are.
                    for (k in listOf("tones", "first_tone_hz", "tone_spacing_hz",
                                     "symbol_rate_bd", "codename")) {
                        spec.put(k, parsed.get(k))
                    }
                    spec.remove("evidence")
                    field.setText("")
                    loadChoices()
                    fillRateSpinner()
                    refresh()
                }
            })
        }
    }

    private fun save() {
        if (!described.optBoolean("ok")) {
            Toast.makeText(activity, described.optString("error").ifEmpty {
                "These settings cannot be used" }, Toast.LENGTH_LONG).show()
            return
        }
        onSaved(spec.toString())
        dialog?.dismiss()
    }

    // -- drawing -------------------------------------------------------------

    private fun loadChoices() {
        val c = JSONObject(py.callAttr("choices_json", spec.optInt("tones", 4)).toString())
        val r = c.optJSONArray("rates") ?: JSONArray()
        rates = (0 until r.length()).map { r.getInt(it) }
        val cs = c.optJSONArray("codes") ?: JSONArray()
        codes = (0 until cs.length()).map { cs.getJSONObject(it) }
        maxLeadInS = c.optDouble("max_lead_in_s", 3.0)
    }

    private fun fillRateSpinner() {
        syncing = true
        rateSpinner.adapter = ArrayAdapter(activity,
            android.R.layout.simple_spinner_dropdown_item,
            rates.map { "$it Bd" })
        syncing = false
    }

    /** Ask Python about the current spec and redraw every control from it. */
    private fun refresh() {
        described = try {
            JSONObject(py.callAttr("describe_json", spec.toString(), false).toString())
        } catch (e: Exception) {
            Log.e(TAG, "describe_json failed", e)
            JSONObject().put("ok", false).put("error", e.toString())
        }

        syncing = true
        try {
            check(tonesGroup, spec.optInt("tones"))
            rates.indexOf(spec.optInt("symbol_rate_bd")).takeIf { it >= 0 }
                ?.let { rateSpinner.setSelection(it) }
            codes.indexOfFirst { it.optString("codename") == spec.optString("codename") }
                .takeIf { it >= 0 }?.let { codeSpinner.setSelection(it) }
            check(copiesGroup, spec.optInt("repeats"))
            check(keyingGroup, spec.optString("keying"))
            check(deemphGroup, if (spec.isNull("deemphasis_us")) null
                               else spec.optDouble("deemphasis_us"))
        } finally {
            syncing = false
        }
        firstToneValue.text = "${spec.optInt("first_tone_hz")} Hz"
        spacingValue.text = "${spec.optInt("tone_spacing_hz")} Hz"
        leadInValue.text = if (spec.isNull("lead_in_s"))
            (if (described.has("lead_in_s"))
                "default (%.2f s)".format(described.optDouble("lead_in_s"))
             else "default for keying")
        else "%.2f s".format(spec.optDouble("lead_in_s"))

        netCodeView.text = described.optString("net_code").ifEmpty { "-" }
        val ok = described.optBoolean("ok")
        netCodeView.setTextColor(if (ok) colorGood else colorBad)

        val problems = StringBuilder()
        if (!ok) problems.append("Cannot be used: ").append(described.optString("error"))
        val warnings = described.optJSONArray("warnings") ?: JSONArray()
        for (i in 0 until warnings.length()) {
            if (problems.isNotEmpty()) problems.append("\n\n")
            problems.append("⚠ ").append(warnings.getString(i))
        }
        problemView.text = problems
        problemView.setTextColor(if (ok) colorWarn else colorBad)
        problemView.visibility = if (problems.isEmpty()) View.GONE else View.VISIBLE

        previewView.text = if (!ok) "" else buildString {
            val tones = described.optJSONArray("tones_hz") ?: JSONArray()
            append("Tones ")
            append((0 until tones.length()).joinToString(" / ") { tones.getInt(it).toString() })
            append(" Hz\n")
            append("Each burst %.2f s on air, carries %d B of content\n".format(
                described.optDouble("burst_s"), described.optInt("content_bytes")))
            append("Raw rate %d bit/s before error correction\n".format(
                described.optInt("raw_bps")))
            described.optString("matches_preset").takeIf {
                it.isNotEmpty() && it != "null" }?.let {
                append("Same on the air as the ${it.replaceFirstChar { c -> c.uppercase() }} preset\n")
            }
        }
        evidenceView.text = described.optString("evidence")

        dialog?.getButton(AlertDialog.BUTTON_POSITIVE)?.isEnabled = ok
    }

    // -- small builders ------------------------------------------------------

    private fun heading(text: String) = TextView(activity).apply {
        this.text = text
        textSize = 13f
        setTypeface(typeface, Typeface.BOLD)
        setPadding(0, dp(12), 0, dp(2))
    }

    private fun note(text: String) = TextView(activity).apply {
        this.text = text
        textSize = 12f
        setTextColor(colorIdle)
        setPadding(0, dp(4), 0, 0)
    }

    private fun stepper(value: TextView, down: () -> Unit, up: () -> Unit,
                        extra: View? = null): View =
        LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = android.view.Gravity.CENTER_VERTICAL
            addView(compact(Ui.outlined(activity, "−")).apply { setOnClickListener { down() } })
            addView(value.apply {
                textSize = 15f
                gravity = android.view.Gravity.CENTER
                layoutParams = LinearLayout.LayoutParams(0,
                    LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            })
            addView(compact(Ui.outlined(activity, "+")).apply { setOnClickListener { up() } })
            extra?.let { addView(if (it is Button) compact(it) else it) }
        }

    /** Material buttons default to a wide minimum; a row of four needs less. */
    private fun compact(b: Button): Button = b.apply {
        minWidth = 0
        minimumWidth = 0
        setPadding(dp(14), paddingTop, dp(14), paddingBottom)
    }

    /** A horizontal radio row whose buttons carry their value as a tag. */
    private fun <T> radioRow(options: List<Pair<String, T>>, onPick: (T) -> Unit): RadioGroup {
        val group = RadioGroup(activity).apply { orientation = RadioGroup.HORIZONTAL }
        options.forEach { (label, value) ->
            group.addView(RadioButton(activity).apply {
                // RadioGroup unchecks by findViewById(), which no-ops at NO_ID
                // -- the same trap RadioLinkDialog's groups document.
                id = View.generateViewId()
                text = label
                textSize = 12f
                tag = Tagged(value)
            })
        }
        group.setOnCheckedChangeListener { g, checkedId ->
            if (syncing || checkedId == View.NO_ID) return@setOnCheckedChangeListener
            @Suppress("UNCHECKED_CAST")
            val v = (g.findViewById<RadioButton>(checkedId)?.tag as? Tagged<T>) ?: return@setOnCheckedChangeListener
            onPick(v.value)
        }
        return group
    }

    /** Wraps a radio value so null ("default") is a value, not a missing tag. */
    private class Tagged<T>(val value: T)

    private fun check(group: RadioGroup, value: Any?) {
        for (i in 0 until group.childCount) {
            val b = group.getChildAt(i) as RadioButton
            val v = (b.tag as? Tagged<*>)?.value
            val same = when {
                v == null || value == null -> v == value
                v is Number && value is Number -> v.toDouble() == value.toDouble()
                else -> v == value
            }
            if (same) { group.check(b.id); return }
        }
        group.clearCheck()
    }

    private fun onPick(pick: (Int) -> Unit) = object : AdapterView.OnItemSelectedListener {
        override fun onItemSelected(parent: AdapterView<*>?, view: View?, position: Int, id: Long) {
            if (!syncing) pick(position)
        }
        override fun onNothingSelected(parent: AdapterView<*>?) {}
    }

    private fun dp(value: Int) = (value * activity.resources.displayMetrics.density).toInt()
}
