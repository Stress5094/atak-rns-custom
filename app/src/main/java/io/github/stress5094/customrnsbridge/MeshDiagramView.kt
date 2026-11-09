package io.github.stress5094.customrnsbridge

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Paint
import android.graphics.Path
import android.graphics.Typeface
import android.view.MotionEvent
import android.view.View
import org.json.JSONObject
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.hypot
import kotlin.math.sin

/**
 * The Mesh tab's picture: this phone, every node it has heard, and one line per
 * radio each is heard on.
 *
 * It only DRAWS. Every grade, reason and ring comes from mesh_view.py via
 * get_mesh_view(), for the same reason the FM audio plan comes from Python: a
 * second copy of a rule drifts silently, and a picture that disagrees with the
 * node list beside it is worse than none.
 *
 * Plain Canvas rather than a graph library: the project keeps to three Gradle
 * dependencies on purpose, and a star with two rings is not worth a fourth.
 *
 * Every mark carries its meaning in more than colour -- line pattern and a text
 * tag for the radio kind, thickness and fill for health -- so it survives
 * sunlight and colour blindness, the same rule pill() follows on the Status tab.
 */
class MeshDiagramView(context: Context) : View(context) {

    var showProbable = true
        set(value) { field = value; invalidate() }
    var selected: String? = null
        set(value) { field = value; invalidate() }
    var onSelect: ((String?) -> Unit)? = null

    private var model: JSONObject? = null
    // Where each node was drawn on the last pass, for tap hit-testing.
    private val drawnAt = HashMap<String, Pair<Float, Float>>()
    // Radio chips are drawn after the nodes, so a label never hides which radio a line is.
    private class Chip(val x: Float, val y: Float, val text: String, val colour: Int, val gone: Boolean)
    private val chips = ArrayList<Chip>()

    private val density = resources.displayMetrics.density
    private fun dp(v: Float) = v * density
    // Text in sp, so it grows with the phone's font size (Google's guidelines).
    private fun sp(v: Float) = android.util.TypedValue.applyDimension(
        android.util.TypedValue.COMPLEX_UNIT_SP, v, resources.displayMetrics)
    // Colours from resources: light and dark themes (values-night).
    private fun col(id: Int) = androidx.core.content.ContextCompat.getColor(context, id)

    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }
    private val fillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val probablePath = Path()
    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = col(R.color.draw_ring)
        strokeWidth = dp(1f)
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(12f)
        color = col(R.color.draw_ink)
        textAlign = Paint.Align.CENTER
    }
    private val tagPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        typeface = Typeface.DEFAULT_BOLD
        textAlign = Paint.Align.CENTER
    }

    fun setModel(view: JSONObject?) {
        model = view
        contentDescription = describe(view)
        invalidate()
    }

    /** What the picture shows, for a screen reader: it cannot see a drawing. */
    private fun describe(m: JSONObject?): String {
        val nodes = m?.optJSONArray("nodes") ?: return "Mesh diagram: nothing heard yet"
        if (nodes.length() == 0) return "Mesh diagram: no other nodes heard"
        val parts = (0 until nodes.length()).map { i ->
            val n = nodes.getJSONObject(i)
            val how = when (n.optString("grade")) {
                "good" -> "good link"; "weak" -> "weak link"; else -> "not heard recently"
            }
            "${n.optString("label")}, ${if (n.optInt("ring", 1) == 2) "through another node" else "direct"}, $how"
        }
        return "Mesh diagram: ${nodes.length()} other node${if (nodes.length() == 1) "" else "s"}. " +
            parts.joinToString("; ")
    }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val w = MeasureSpec.getSize(widthMeasureSpec)
        setMeasuredDimension(w, w)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        drawnAt.clear()
        chips.clear()
        val m = model ?: return
        val nodes = m.optJSONArray("nodes") ?: return
        val meId = m.optJSONObject("me")?.optString("node_id") ?: ""

        val positions = layoutHops(m)
        val me = positions[meId] ?: return

        if (showProbable) drawProbable(canvas, m, positions)

        // Lines first, nodes on top, so a node is never hidden by its own lines.
        val lines = m.optJSONArray("lines")
        val byNode = LinkedHashMap<String, MutableList<JSONObject>>()
        if (lines != null) for (i in 0 until lines.length()) {
            val l = lines.getJSONObject(i)
            byNode.getOrPut(l.optString("node_id")) { mutableListOf() }.add(l)
        }
        for ((nodeId, nodeLines) in byNode) {
            val to = positions[nodeId] ?: continue
            // Parallel lines for a node heard on several radios, side by side
            // rather than on top of each other.
            nodeLines.forEachIndexed { i, l ->
                val offset = (i - (nodeLines.size - 1) / 2f) * dp(7f)
                drawLine(canvas, me, to, offset, l)
            }
        }

        for (i in 0 until nodes.length()) {
            val n = nodes.getJSONObject(i)
            val id = n.optString("node_id")
            val at = positions[id] ?: continue
            drawNode(canvas, at, n.optString("label"), n.optString("grade"),
                ageText(n.opt("age_s")), isSelected = id == selected)
            drawnAt[id] = at
        }
        drawMe(canvas, me, m.optJSONObject("me")?.optString("label") ?: "")
        for (c in chips) drawChip(canvas, c)
        drawLegend(canvas, m)
    }

    /** This phone in the middle, direct neighbours on the inner ring, relayed
     *  nodes on the outer one. Sorted by id so nodes keep their places across
     *  refreshes instead of shuffling every 2 s as ages change. */
    private fun layoutHops(m: JSONObject): Map<String, Pair<Float, Float>> {
        val size = width.toFloat()
        val cx = size / 2f
        val cy = size / 2f
        val out = HashMap<String, Pair<Float, Float>>()
        out[m.optJSONObject("me")?.optString("node_id") ?: ""] = cx to cy
        val nodes = m.optJSONArray("nodes") ?: return out
        val rings = mapOf(1 to mutableListOf<String>(), 2 to mutableListOf())
        for (i in 0 until nodes.length()) {
            val n = nodes.getJSONObject(i)
            rings[if (n.optInt("ring", 1) == 2) 2 else 1]!!.add(n.optString("node_id"))
        }
        for ((ring, ids) in rings) {
            ids.sort()
            val radius = size * (if (ring == 1) 0.27f else 0.42f)
            ids.forEachIndexed { i, id ->
                // Outer ring starts half a step round so its nodes sit between
                // the inner ring's, not behind them.
                val a = -PI / 2 + 2 * PI * (i + if (ring == 2) 0.5 else 0.0) / ids.size
                out[id] = (cx + radius * cos(a)).toFloat() to (cy + radius * sin(a)).toFloat()
            }
        }
        return out
    }

    // -- marks -----------------------------------------------------------------

    private fun drawLine(canvas: Canvas, from: Pair<Float, Float>, to: Pair<Float, Float>,
                         offset: Float, l: JSONObject) {
        val kind = l.optString("radio")
        val grade = l.optString("grade")
        val direct = l.optBoolean("direct", true)
        val dx = to.first - from.first
        val dy = to.second - from.second
        val len = hypot(dx, dy).takeIf { it > 0f } ?: return
        // Perpendicular offset for parallel lines.
        val ox = -dy / len * offset
        val oy = dx / len * offset
        val x1 = from.first + ox
        val y1 = from.second + oy
        val x2 = to.first + ox
        val y2 = to.second + oy

        linePaint.color = colourFor(kind)
        linePaint.pathEffect = patternFor(kind)
        linePaint.strokeWidth = dp(when (grade) { "good" -> 4f; "weak" -> 2.5f; else -> 1.5f })
        // Gone and relayed lines recede: a relayed line is "reached through
        // somebody", not a link this radio has.
        linePaint.alpha = when {
            grade == "gone" -> 70
            !direct -> 120
            else -> 255
        }
        canvas.drawLine(x1, y1, x2, y2, linePaint)
        linePaint.alpha = 255

        // The tag says the kind in words, mid-line, on a white chip.
        val tag = tagFor(kind) + if (!direct) " via" else ""
        chips.add(Chip((x1 + x2) / 2f, (y1 + y2) / 2f, tag, colourFor(kind), grade == "gone"))
    }

    private fun drawChip(canvas: Canvas, c: Chip) {
        val w = tagPaint.measureText(c.text) + dp(6f)
        fillPaint.color = col(R.color.draw_node)
        canvas.drawRoundRect(c.x - w / 2, c.y - dp(7f), c.x + w / 2, c.y + dp(6f), dp(3f), dp(3f), fillPaint)
        tagPaint.color = c.colour
        tagPaint.alpha = if (c.gone) 110 else 255
        canvas.drawText(c.text, c.x, c.y + dp(3f), tagPaint)
        tagPaint.alpha = 255
    }

    // A white backing keeps a line from running through the words.
    private fun drawLabel(canvas: Canvas, text: String, x: Float, baseline: Float) {
        val fm = textPaint.fontMetrics
        val w = textPaint.measureText(text) + dp(6f)
        fillPaint.color = col(R.color.draw_node)
        canvas.drawRoundRect(x - w / 2, baseline + fm.ascent - dp(1f), x + w / 2,
            baseline + fm.descent + dp(1f), dp(3f), dp(3f), fillPaint)
        canvas.drawText(text, x, baseline, textPaint)
    }

    // Bowed outward, not straight: two nodes on opposite sides of this phone lie
    // on one line through it, and a straight probable line would be hidden under
    // the direct lines to them.
    private fun drawProbable(canvas: Canvas, m: JSONObject, positions: Map<String, Pair<Float, Float>>) {
        val pairs = m.optJSONArray("probable") ?: return
        linePaint.color = col(R.color.draw_line)
        linePaint.pathEffect = DashPathEffect(floatArrayOf(dp(2f), dp(5f)), 0f)
        linePaint.strokeWidth = dp(1f)
        linePaint.alpha = 160
        val cx = width / 2f
        val cy = height / 2f
        for (i in 0 until pairs.length()) {
            val p = pairs.getJSONObject(i)
            val a = positions[p.optString("a")] ?: continue
            val b = positions[p.optString("b")] ?: continue
            val dx = b.first - a.first
            val dy = b.second - a.second
            val len = hypot(dx, dy).takeIf { it > 0f } ?: continue
            val mx = (a.first + b.first) / 2f
            val my = (a.second + b.second) / 2f
            var nx = -dy / len
            var ny = dx / len
            if ((mx - cx) * nx + (my - cy) * ny < 0f) { nx = -nx; ny = -ny }
            val bow = dp(50f)
            probablePath.reset()
            probablePath.moveTo(a.first, a.second)
            probablePath.quadTo(mx + nx * bow, my + ny * bow, b.first, b.second)
            canvas.drawPath(probablePath, linePaint)
        }
        linePaint.alpha = 255
    }

    private fun drawNode(canvas: Canvas, at: Pair<Float, Float>, label: String, grade: String,
                         age: String, isSelected: Boolean) {
        val r = dp(11f)
        if (isSelected) {
            fillPaint.color = col(R.color.status_highlight)
            canvas.drawCircle(at.first, at.second, r + dp(6f), fillPaint)
        }
        val ink = gradeColour(grade)
        // A ring rather than a dot reads as "not here": gone nodes are hollow.
        if (grade == "gone") {
            fillPaint.color = col(R.color.draw_node)
            canvas.drawCircle(at.first, at.second, r, fillPaint)
            linePaint.pathEffect = null
            linePaint.color = ink
            linePaint.strokeWidth = dp(2.5f)
            canvas.drawCircle(at.first, at.second, r, linePaint)
        } else {
            fillPaint.color = ink
            canvas.drawCircle(at.first, at.second, r, fillPaint)
        }
        textPaint.typeface = Typeface.DEFAULT_BOLD
        drawLabel(canvas, label.take(14), at.first, at.second + r + dp(13f))
        textPaint.typeface = Typeface.DEFAULT
        drawLabel(canvas, age, at.first, at.second + r + dp(25f))
    }

    private fun drawMe(canvas: Canvas, at: Pair<Float, Float>, label: String) {
        val r = dp(14f)
        fillPaint.color = col(R.color.draw_ink)
        canvas.drawCircle(at.first, at.second, r, fillPaint)
        tagPaint.color = col(R.color.draw_node)
        canvas.drawText("ME", at.first, at.second + dp(3f), tagPaint)
        textPaint.typeface = Typeface.DEFAULT_BOLD
        drawLabel(canvas, label.ifEmpty { "This phone" }.take(14), at.first, at.second + r + dp(13f))
        textPaint.typeface = Typeface.DEFAULT
    }

    /** Top-left: one sample line per kind, from Python's list, plus the
     *  probable-line pattern when those are shown. */
    private fun drawLegend(canvas: Canvas, m: JSONObject) {
        val kinds = m.optJSONArray("kinds") ?: return
        var y = dp(14f)
        val x = dp(6f)
        textPaint.textAlign = Paint.Align.LEFT
        for (i in 0 until kinds.length()) {
            val k = kinds.getJSONObject(i)
            val kind = k.optString("kind")
            linePaint.color = colourFor(kind)
            linePaint.pathEffect = patternFor(kind)
            linePaint.strokeWidth = dp(3f)
            canvas.drawLine(x, y - dp(4f), x + dp(22f), y - dp(4f), linePaint)
            canvas.drawText(k.optString("label"), x + dp(28f), y, textPaint)
            y += dp(15f)
        }
        if (showProbable) {
            linePaint.color = col(R.color.draw_line)
            linePaint.pathEffect = DashPathEffect(floatArrayOf(dp(2f), dp(5f)), 0f)
            linePaint.strokeWidth = dp(1f)
            canvas.drawLine(x, y - dp(4f), x + dp(22f), y - dp(4f), linePaint)
            canvas.drawText("probable", x + dp(28f), y, textPaint)
        }
        textPaint.textAlign = Paint.Align.CENTER
    }

    // -- styles ----------------------------------------------------------------

    // One entry per mesh_view.KINDS; test_mesh_view holds the two together, so
    // a kind added in Python and not here fails a test instead of being drawn
    // in the fallback style.
    private fun colourFor(kind: String) = col(when (kind) {
        "rnode" -> R.color.link_rnode
        "meshtastic" -> R.color.link_meshtastic
        "fm" -> R.color.link_fm
        "wifi" -> R.color.link_wifi
        else -> R.color.draw_ink
    })

    private fun patternFor(kind: String) = when (kind) {
        "rnode" -> null
        "meshtastic" -> null
        "fm" -> DashPathEffect(floatArrayOf(dp(10f), dp(6f)), 0f)
        "wifi" -> DashPathEffect(floatArrayOf(dp(2f), dp(4f)), 0f)
        else -> null
    }

    private fun tagFor(kind: String) = when (kind) {
        "rnode" -> "RN"
        "meshtastic" -> "MT"
        "fm" -> "FM"
        "wifi" -> "WiFi"
        else -> kind
    }

    private fun gradeColour(grade: String) = col(when (grade) {
        "good" -> R.color.status_good
        "weak" -> R.color.status_warn
        else -> R.color.draw_line
    })

    private fun ageText(age: Any?): String {
        val s = (age as? Number)?.toDouble() ?: return ""
        return when {
            s < 90 -> "%d s".format(s.toInt())
            s < 5400 -> "%d min".format((s / 60).toInt())
            else -> "%.1f h".format(s / 3600)
        }
    }

    // -- touch -----------------------------------------------------------------

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.action == MotionEvent.ACTION_DOWN) return true
        if (event.action != MotionEvent.ACTION_UP) return super.onTouchEvent(event)
        var best: String? = null
        var bestD = dp(28f)
        for ((id, at) in drawnAt) {
            val d = hypot(event.x - at.first, event.y - at.second)
            if (d < bestD) { bestD = d; best = id }
        }
        // Tapping the selected node again, or empty space, clears it.
        val pick = if (best == selected) null else best
        selected = pick
        onSelect?.invoke(pick)
        performClick()
        return true
    }

    override fun performClick(): Boolean = super.performClick()
}
