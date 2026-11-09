package io.github.stress5094.customrnsbridge

import android.app.Activity
import android.graphics.Typeface
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors

/**
 * "Measure my radios": the tone sweep (fm_tune.py), Phase D's first slice.
 *
 * One phone sends a ~10 s ladder of tones; the other listens, measures how
 * each frequency arrived, and recommends settings that fit. Run it with the
 * two phones on their radios, one pressing Listen and then the other Send.
 *
 * Everything is measured and decided in Python (fm_tune, fm_link's
 * tune_* calls); this draws it. A recommendation is a buildable fm_custom
 * spec, so "Use these settings" hands it straight to the editor, which still
 * shows its warnings and still needs Save.
 */
class FmTuneDialog(
    private val activity: Activity,
    private val onUse: (String) -> Unit
) {
    companion object {
        private const val LISTEN_S = 40
        private const val POLL_MS = 1000L
    }

    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()
    private val colorIdle by lazy { Ui.res(activity, R.color.status_idle) }
    private val colorWarn by lazy { Ui.res(activity, R.color.status_warn) }
    private val colorBad by lazy { Ui.res(activity, R.color.status_bad) }
    private val colorGood by lazy { Ui.res(activity, R.color.status_good) }

    private lateinit var status: TextView
    private lateinit var results: LinearLayout
    private var dialog: AlertDialog? = null
    private var polling = false

    fun show() {
        val body = LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), dp(8))
        }
        body.addView(text(
            "Needs two phones, each on its own radio, on the same channel. " +
            "On the phone that should RECEIVE, press Listen; then on the " +
            "other press Send. The sweep takes about 10 seconds.\n\n" +
            "Set the receiving radio's volume to a normal level first. The " +
            "result describes the radios' audio path at that setting; it is " +
            "not a range test.", 13f))
        body.addView(LinearLayout(activity).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, dp(4))
            addView(Ui.filled(activity, "Listen").apply {
                setOnClickListener { listen() }
            })
            addView(Ui.outlined(activity, "Send the sweep").apply {
                setOnClickListener { send() }
                (layoutParams as? LinearLayout.LayoutParams)?.marginStart = dp(8)
            })
        })
        status = text("", 14f).apply { setTypeface(typeface, Typeface.BOLD) }
        body.addView(status)
        results = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
        body.addView(results)

        dialog = MaterialAlertDialogBuilder(activity)
            .setTitle("Measure my radios")
            .setView(ScrollView(activity).apply { addView(body) })
            .setNegativeButton("Close", null)
            .create()
        dialog?.setOnDismissListener {
            polling = false
            executor.execute { PythonRnsService.call("fm_tune_cancel") }
            executor.shutdown()
        }
        dialog?.show()
    }

    // -- actions ---------------------------------------------------------------

    private fun listen() {
        results.removeAllViews()
        setStatus("Starting to listen…", colorIdle)
        executor.execute {
            val got = PythonRnsService.call("fm_tune_listen", LISTEN_S.toString())
                ?.toDoubleOrNull() ?: 0.0
            handler.post {
                if (got <= 0.0) {
                    setStatus("The FM link is not up. If the app just started, wait " +
                        "half a minute; otherwise turn FM on (as the link or as backup).", colorBad)
                } else {
                    polling = true
                    poll()
                }
            }
        }
    }

    private fun send() {
        results.removeAllViews()
        executor.execute {
            val s = PythonRnsService.call("fm_tune_send")?.toDoubleOrNull() ?: 0.0
            handler.post {
                if (s <= 0.0) setStatus("The FM link is not up, so nothing was sent. If the " +
                    "app just started, wait half a minute.", colorBad)
                else setStatus("Sending the sweep: about %.0f seconds on air. The other "
                    .format(s) + "phone shows the result.", colorGood)
            }
        }
    }

    private fun poll() {
        if (!polling) return
        executor.execute {
            val raw = PythonRnsService.call("fm_tune_status")
            val st = try { JSONObject(raw ?: "{}") } catch (e: Exception) { JSONObject() }
            handler.post {
                when (st.optString("state")) {
                    "listening" -> {
                        setStatus("Listening: %.0f of %.0f s. Press Send on the other phone now."
                            .format(st.optDouble("heard_s"), st.optDouble("want_s")), colorIdle)
                        handler.postDelayed({ poll() }, POLL_MS)
                    }
                    "analysing" -> {
                        setStatus("Measuring…", colorIdle)
                        handler.postDelayed({ poll() }, POLL_MS)
                    }
                    "done" -> {
                        polling = false
                        st.optJSONObject("result")?.let { render(it) }
                    }
                    else -> polling = false
                }
            }
        }
    }

    // -- the result ------------------------------------------------------------

    private fun render(r: JSONObject) {
        results.removeAllViews()
        if (!r.optBoolean("found")) {
            setStatus("No sweep measured", colorBad)
            results.addView(text(r.optString("error"), 13f, colorBad))
            return
        }
        val band = r.optJSONArray("band_hz")
        setStatus(if (band != null) "Usable band: ${band.getInt(0)}–${band.getInt(1)} Hz"
                  else "No usable band", if (band != null) colorGood else colorBad)
        if (!r.isNull("tilt_db_per_khz")) {
            results.addView(text("Tilt: %+.1f dB per kHz (how the level changes as the pitch rises)"
                .format(r.optDouble("tilt_db_per_khz")), 13f))
        }
        results.addView(text("How each tone arrived (0 dB = the strongest):", 13f).apply {
            setPadding(0, dp(8), 0, dp(2))
        })
        results.addView(TextView(activity).apply {
            typeface = Typeface.MONOSPACE
            textSize = 11f
            text = chart(r.optJSONArray("points") ?: JSONArray(), band)
        })
        val notes = r.optJSONArray("notes") ?: JSONArray()
        for (i in 0 until notes.length()) results.addView(text(notes.getString(i), 12f, colorIdle))

        val rec = r.optJSONObject("recommendation") ?: return
        if (rec.has("error")) {
            results.addView(text(rec.optString("error"), 13f, colorBad))
            return
        }
        results.addView(heading("Settings that fit these radios"))
        val cands = rec.optJSONArray("candidates") ?: JSONArray()
        for (i in 0 until cands.length()) results.addView(candidate(cands.getJSONObject(i), null))
        rec.optJSONObject("control")?.let {
            results.addView(heading("For comparison"))
            results.addView(candidate(it, it.optString("preset")))
        }
        if (!rec.isNull("deemphasis_hint")) {
            results.addView(text(rec.optString("deemphasis_hint"), 12f, colorWarn))
        }
        results.addView(text("These are fits to one measurement. The way to know is " +
            "to try one against your current setting on the air.", 12f, colorIdle))
    }

    private fun chart(points: JSONArray, band: JSONArray?): String = buildString {
        val lo = band?.getInt(0) ?: Int.MAX_VALUE
        val hi = band?.getInt(1) ?: Int.MIN_VALUE
        for (i in 0 until points.length()) {
            val p = points.getJSONObject(i)
            val hz = p.getInt("hz")
            val db = p.getDouble("level_db")
            // 1 block per 1.5 dB over a 24 dB window.
            val n = ((db + 24.0) / 1.5).toInt().coerceIn(0, 16)
            val mark = if (hz in lo..hi) "●" else " "
            val muddy = if (p.getDouble("purity") < 0.5) " (mostly other frequencies)" else ""
            append("%4d Hz %s %-16s %6.1f dB%s\n".format(
                hz, mark, "█".repeat(n), db, muddy))
        }
    }

    private fun candidate(c: JSONObject, preset: String?): View =
        LinearLayout(activity).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(6), 0, dp(6))
            addView(TextView(activity).apply {
                text = if (preset != null) "${preset.replaceFirstChar { it.uppercase() }} preset"
                       else c.optString("net_code")
                textSize = 14f
                setTypeface(Typeface.MONOSPACE, Typeface.BOLD)
            })
            val tones = c.optJSONArray("tones_hz") ?: JSONArray()
            addView(text("Tones %s Hz · arrive within %.1f dB · %.2f s per burst · %d B per burst"
                .format((0 until tones.length()).joinToString("/") { tones.getInt(it).toString() },
                        c.optDouble("spread_db"), c.optDouble("burst_s"),
                        c.optInt("content_bytes")), 12f))
            val notes = c.optJSONArray("notes") ?: JSONArray()
            for (i in 0 until notes.length()) addView(text(notes.getString(i), 12f, colorWarn))
            if (preset == null) {
                addView(Ui.tonal(activity, "Use these settings").apply {
                    setOnClickListener {
                        onUse(c.getJSONObject("spec").toString())
                        Toast.makeText(activity, "Copied into the editor. Review, then " +
                            "Use these and Save.", Toast.LENGTH_LONG).show()
                        dialog?.dismiss()
                    }
                })
            }
        }

    // -- small builders ----------------------------------------------------------

    private fun setStatus(s: String, color: Int) {
        status.text = s
        status.setTextColor(color)
    }

    private fun heading(s: String) = TextView(activity).apply {
        text = s
        textSize = 14f
        setTypeface(typeface, Typeface.BOLD)
        setPadding(0, dp(12), 0, dp(2))
    }

    private fun text(s: String, size: Float, color: Int? = null) = TextView(activity).apply {
        text = s
        textSize = size
        color?.let { setTextColor(it) }
        setPadding(0, dp(2), 0, dp(2))
    }

    private fun dp(v: Int) = (v * activity.resources.displayMetrics.density).toInt()
}
