package io.github.stress5094.customrnsbridge

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.DashPathEffect
import android.graphics.Matrix
import android.graphics.Paint
import android.graphics.Path
import android.graphics.PathMeasure
import android.graphics.Rect
import android.graphics.RectF
import android.graphics.Typeface
import android.view.GestureDetector
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.ScaleGestureDetector
import android.view.View
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.ceil
import kotlin.math.ln
import kotlin.math.tan
import kotlin.math.atan
import kotlin.math.sinh
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.min
import kotlin.math.pow
import kotlin.math.sin

/**
 * The Coverage tab's map: the streets and buildings around a transmitter
 * point, how far the chosen radio reaches from it, and -- since the area
 * picker -- a pannable, zoomable view to choose WHERE to download Overture
 * building data and compute.
 *
 * It only DRAWS and reports gestures. Every number -- the coverage boundary,
 * the streets, the buildings, the basemap --
 * comes from coverage.py, for the same reason the FM audio plan comes from
 * Python: a second copy of a rule drifts silently, and a picture that
 * disagrees with the numbers beside it is worse than none.
 *
 * Plain Canvas rather than a map library: the project keeps to three Gradle
 * dependencies on purpose (see build.gradle.kts). The basemap is Overture's
 * own street tiles, decoded in Python, so there is still no tile server
 * dependency beyond the data source itself.
 *
 * Geometry is held as Paths in local metres (x east, y north) around each
 * layer's own anchor point, built once when the data arrives; drawing
 * transforms them to the screen. Rebuilding ~10k building paths per frame is what would
 * make panning stutter. Equirectangular throughout: over the few km this
 * shows, the error is well under a pixel.
 *
 * Gestures:
 *   browse mode   drag pans, pinch zooms, double-tap zooms in,
 *                 long-press moves the transmitter there (onLongPress)
 *   select mode   drag draws the area box (onSelection); pinch still zooms
 */
class CoverageMapView(context: Context) : View(context) {

    private val density = resources.displayMetrics.density
    private fun dp(v: Float) = v * density
    // Map text in sp, so it follows the phone's font size; colours of the
    // street view from resources, so it has a dark version (values-night).
    // The photo view keeps its own: it is drawn over imagery, not the theme.
    private fun sp(v: Float) = android.util.TypedValue.applyDimension(
        android.util.TypedValue.COMPLEX_UNIT_SP, v, resources.displayMetrics)
    private fun col(id: Int) = androidx.core.content.ContextCompat.getColor(context, id)

    // --- callbacks ----------------------------------------------------------

    /** A pan or zoom has finished; (south, west, north, east) is in view. */
    var onViewportSettled: ((Double, Double, Double, Double) -> Unit)? = null
    /** Long-press in browse mode: the operator wants coverage computed here. */
    var onLongPress: ((Double, Double) -> Unit)? = null
    /** The selection box changed (null = cleared): south, west, north, east. */
    var onSelection: ((DoubleArray?) -> Unit)? = null

    // --- view state ----------------------------------------------------------

    private var viewLat = Double.NaN
    private var viewLon = Double.NaN
    private var mPerPx = 4.0               // zoom: metres per screen pixel
    private val minMPerPx = 0.25
    private val maxMPerPx = 400.0

    /** Hybrid view: satellite imagery under street lines and names, with the
     *  coverage drawn in colours chosen to stand out against it. The
     *  default; false is the plain street map. */
    var satellite = true
        set(v) {
            field = v
            invalidate()
        }

    private val imagery = ImageryTiles(
        java.io.File(context.getExternalFilesDir(null), "coverage/imagery")
    ) { postInvalidate() }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        imagery.shutdown()
    }

    var selectMode = false
        set(v) {
            field = v
            invalidate()
        }
    private var selection: DoubleArray? = null    // s, w, n, e
    private var dragStart: DoubleArray? = null    // lat, lon

    /** A geometry layer: paths in metres around an anchor. */
    private class Layer(val lat: Double, val lon: Double) {
        val roads = Path()
        val buildings = Path()
        val reach = Path()
        var hasCoverage = false
        // Named street pieces, longest first within a name: the label goes
        // on the longest piece that is on screen and long enough to hold it.
        val labels = LinkedHashMap<String, MutableList<StreetPiece>>()
    }

    /** One named street segment, x/y interleaved in metres around its layer
     *  anchor, with its bounding box for a cheap on-screen test. */
    private class StreetPiece(val pts: FloatArray, val lengthM: Float,
                              val minX: Float, val minY: Float,
                              val maxX: Float, val maxY: Float)

    private fun addLabel(layer: Layer, name: String, pts: FloatArray) {
        if (name.isEmpty() || pts.size < 4) return
        var len = 0f
        var minX = pts[0]; var maxX = pts[0]; var minY = pts[1]; var maxY = pts[1]
        for (k in 2 until pts.size step 2) {
            len += kotlin.math.hypot(pts[k] - pts[k - 2], pts[k + 1] - pts[k - 1])
            minX = min(minX, pts[k]); maxX = max(maxX, pts[k])
            minY = min(minY, pts[k + 1]); maxY = max(maxY, pts[k + 1])
        }
        layer.labels.getOrPut(name) { ArrayList() }
            .add(StreetPiece(pts, len, minX, minY, maxX, maxY))
    }

    private fun sortLabels(layer: Layer) {
        for (list in layer.labels.values) list.sortByDescending { it.lengthM }
    }

    private var model: JSONObject? = null
    private var modelLayer: Layer? = null
    private var basemapLayer: Layer? = null
    /** A saved area from coverage.list_areas(): its outline, and what it is
     *  -- [kind] is "overture" (downloaded by the operator), "auto" or
     *  "peer" (fetched by the app, replaced on their own). */
    class SavedArea(val box: DoubleArray, val file: String, val kind: String,
                    val source: String, val buildings: Int)

    private var areas: List<SavedArea> = emptyList()
    // Whether the coverage shown is LoRa's: LoRa is drawn GREEN, FM BLUE, so
    // the colour says which radio the shape is for at a glance.
    private var modelIsLora = false

    /** Another node's footprint: where it last SAID it was (rns_bridge's
     *  peer_positions) and, once coverage.compute_peer() has answered, its
     *  reach -- [coverage] is that result's "coverage" array. [note] is shown
     *  beside the name instead when there is no outline yet. */
    class PeerFootprint(val nodeId: String, val name: String,
                        val lat: Double, val lon: Double, val ageS: Double,
                        val coverage: JSONArray?, val note: String?,
                        // Where [coverage] was computed from: the outline is
                        // only recomputed after a 100 m move, so this can lag
                        // the dot at (lat, lon) by up to that.
                        val covLat: Double = lat, val covLon: Double = lon)

    private class PeerShape(val p: PeerFootprint, val layer: Layer, val colour: Int)

    private var peers: List<PeerShape> = emptyList()
    // A peer keeps its colour while it stays on the map; a new one takes the
    // lowest free slot, so two peers never share one until there are more
    // peers than colours.
    private val peerSlots = HashMap<String, Int>()

    var showPeers = true
        set(v) {
            field = v
            invalidate()
        }

    // --- paints --------------------------------------------------------------

    private val bgPaint = Paint().apply { color = col(R.color.draw_background) }
    private val basemapPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.draw_grid)
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeWidth = dp(2f)
    }
    private val roadPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.draw_street)
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeWidth = dp(3f)
    }
    private val buildingFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.draw_building)
        style = Paint.Style.FILL
    }
    // Street view: LoRa green, FM blue (set per draw from modelIsLora).
    private val streetLoraFill = Color.parseColor("#5566BB33")   // ~33% alpha
    private val streetLoraLine = col(R.color.cov_lora_line)
    private val streetFmFill = Color.parseColor("#551E88E5")
    private val streetFmLine = col(R.color.cov_fm_line)
    private val coverageFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }
    private val coverageLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(2f)
    }
    private val areaLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.cov_area_line)
        style = Paint.Style.STROKE
        strokeWidth = dp(1.5f)
        pathEffect = DashPathEffect(floatArrayOf(dp(6f), dp(4f)), 0f)
    }
    private val selectFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#33FF8F00")
        style = Paint.Style.FILL
    }
    private val selectLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.parseColor("#E65100")
        style = Paint.Style.STROKE
        strokeWidth = dp(2f)
    }
    private val mePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.draw_ink)
        style = Paint.Style.FILL
    }
    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        color = col(R.color.draw_ink)
        textAlign = Paint.Align.CENTER
        typeface = Typeface.DEFAULT_BOLD
    }
    private val smallText = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        color = col(R.color.draw_ink_soft)
        textAlign = Paint.Align.LEFT
    }
    private val ringPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.draw_line)
        style = Paint.Style.STROKE
        strokeWidth = dp(1f)
        pathEffect = DashPathEffect(floatArrayOf(dp(2f), dp(3f)), 0f)
    }
    private val ringText = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        color = col(R.color.draw_ink_soft)
        typeface = Typeface.DEFAULT_BOLD
    }
    private val labelText = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        color = col(R.color.draw_ink)
    }
    // A white outline under each label so it reads over buildings and the
    // green coverage fill alike.
    private val labelHalo = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        color = col(R.color.draw_background)
        style = Paint.Style.STROKE
        strokeWidth = dp(3f)
        strokeJoin = Paint.Join.ROUND
    }
    // --- hybrid (satellite) styling --------------------------------------
    // The imagery here is tan desert, grey roofs and green trees, so the
    // coverage is CYAN -- a colour the ground does not have -- and every
    // line gets a dark under-stroke so it holds against light roofs and
    // dark trees alike. The imagery itself is dimmed a little (imageryDim)
    // for the same reason.
    private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)
    private val imageryDim = Paint().apply { color = Color.argb(70, 0, 0, 0) }
    private val satRoad = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(140, 255, 255, 255)
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeWidth = dp(1.2f)
    }
    // Photo view: LoRa a bright green, FM cyan -- neither is a colour the
    // ground has (tan desert, grey roofs, darker green trees).
    private val satLora = Color.rgb(0, 230, 118)
    private val satFm = Color.rgb(0, 229, 255)
    private val satCoverageFill = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }
    private val satCoverageUnder = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(200, 0, 0, 0)
        style = Paint.Style.STROKE
        strokeWidth = dp(5f)
        strokeJoin = Paint.Join.ROUND
    }
    private val satCoverageLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(2.5f)
        strokeJoin = Paint.Join.ROUND
    }
    private val satRingUnder = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(150, 0, 0, 0)
        style = Paint.Style.STROKE
        strokeWidth = dp(2.5f)
    }
    private val satRing = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        style = Paint.Style.STROKE
        strokeWidth = dp(1f)
        pathEffect = DashPathEffect(floatArrayOf(dp(3f), dp(3f)), 0f)
    }
    private val satAreaUnder = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(180, 0, 0, 0)
        style = Paint.Style.STROKE
        strokeWidth = dp(3.5f)
    }
    private val satAreaLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.rgb(255, 64, 255)
        style = Paint.Style.STROKE
        strokeWidth = dp(1.5f)
        pathEffect = DashPathEffect(floatArrayOf(dp(6f), dp(4f)), 0f)
    }
    private val meRing = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        style = Paint.Style.FILL
    }
    private val textHalo = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(200, 0, 0, 0)
        style = Paint.Style.STROKE
        strokeWidth = dp(3f)
        strokeJoin = Paint.Join.ROUND
        typeface = Typeface.DEFAULT_BOLD
    }
    private val panelPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.draw_background)
        alpha = 220
    }

    // --- other nodes' footprints ---------------------------------------
    // Outlines only, no fill: the operator's own reach is the filled shape,
    // and three translucent fills over each other turn to mud. Colours the
    // map does not already use -- not green or cyan (own LoRa/FM reach),
    // magenta (saved areas), orange (the selection box) on the photo; not
    // green or blue (own reach) on the street view.
    private val peerSatColours = intArrayOf(
        Color.rgb(255, 235, 59), Color.rgb(255, 82, 82),
        Color.rgb(179, 136, 255), Color.rgb(255, 255, 255))
    private val peerStreetColours = intArrayOf(
        col(R.color.peer_1), col(R.color.peer_2), col(R.color.peer_3), col(R.color.peer_4))
    private val peerUnder = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(200, 0, 0, 0)
        style = Paint.Style.STROKE
        strokeWidth = dp(4.5f)
        strokeJoin = Paint.Join.ROUND
    }
    private val peerLine = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = dp(2f)
        strokeJoin = Paint.Join.ROUND
    }
    private val peerDot = Paint(Paint.ANTI_ALIAS_FLAG).apply { style = Paint.Style.FILL }
    private val peerText = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        typeface = Typeface.DEFAULT_BOLD
    }
    private val peerHalo = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        textSize = sp(11f)
        typeface = Typeface.DEFAULT_BOLD
        style = Paint.Style.STROKE
        strokeWidth = dp(3f)
        strokeJoin = Paint.Join.ROUND
    }

    private val scalePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = col(R.color.draw_ink)
        strokeWidth = dp(1.5f)
    }

    // --- data in -------------------------------------------------------------

    /** A coverage result from coverage.compute(). Re-centres on its
     *  transmitter when [recenter] or when nothing has been shown yet. */
    fun setModel(m: JSONObject?, recenter: Boolean = true) {
        model = m
        // For a screen reader, which cannot see the drawing.
        contentDescription = when {
            m == null -> "Coverage map: nothing computed yet"
            m.has("error") -> "Coverage map: " + m.optString("error")
            else -> "Coverage map: estimated reach of " + m.optString("label", "this radio") +
                " around the transmitter"
        }
        modelLayer = null
        if (m != null && !m.has("error")) {
            modelIsLora = m.optString("radio") == "lora"
            val layer = Layer(m.optDouble("lat"), m.optDouble("lon"))
            addLines(layer.roads, m.optJSONArray("roads"), close = false)
            // Whole named streets, chained from Overture's per-intersection
            // pieces by coverage.merge_street_lines -- a single piece is
            // usually shorter on screen than its name.
            m.optJSONArray("street_labels")?.let { labels ->
                for (i in 0 until labels.length()) {
                    val entry = labels.getJSONArray(i)
                    val line = entry.getJSONArray(1)
                    val pts = FloatArray(line.length() * 2)
                    for (j in 0 until line.length()) {
                        val pt = line.getJSONArray(j)
                        pts[2 * j] = pt.getDouble(0).toFloat()
                        pts[2 * j + 1] = pt.getDouble(1).toFloat()
                    }
                    addLabel(layer, entry.getString(0), pts)
                }
                sortLabels(layer)
            }
            addLines(layer.buildings, m.optJSONArray("buildings"), close = true)
            m.optJSONArray("coverage")?.let { cov ->
                for (i in 0 until cov.length()) {
                    val c = cov.getJSONObject(i)
                    val az = Math.toRadians(c.optDouble("az"))
                    val r = c.optDouble("range").toFloat()
                    val sx = sin(az).toFloat()
                    val cy = cos(az).toFloat()
                    if (i == 0) layer.reach.moveTo(r * sx, r * cy) else layer.reach.lineTo(r * sx, r * cy)
                }
                layer.reach.close()
                layer.hasCoverage = cov.length() > 0
            }
            modelLayer = layer
            if (recenter || viewLat.isNaN()) {
                viewLat = layer.lat
                viewLon = layer.lon
                // Fit the computation disc (diameter) into the view.
                val radius = m.optDouble("radius_m", 2000.0)
                if (width > 0) mPerPx = clampZoom(2.2 * radius / min(width, height))
            }
        }
        invalidate()
        settle()
    }

    /** Major streets from coverage.basemap(), in [lat, lon] points, and
     *  [labels] -- [name, line] pairs of the same streets chained whole. */
    fun setBasemap(roads: JSONArray?, labels: JSONArray? = null) {
        if (roads == null || viewLat.isNaN()) {
            basemapLayer = null
        } else {
            val layer = Layer(viewLat, viewLon)
            val k = metresPerDegLon(layer.lat)
            for (i in 0 until roads.length()) {
                val line = roads.getJSONArray(i)
                for (j in 0 until line.length()) {
                    val p = line.getJSONArray(j)
                    val x = ((p.getDouble(1) - layer.lon) * k).toFloat()
                    val y = ((p.getDouble(0) - layer.lat) * M_PER_DEG_LAT).toFloat()
                    if (j == 0) layer.roads.moveTo(x, y) else layer.roads.lineTo(x, y)
                }
            }
            if (labels != null) {
                for (i in 0 until labels.length()) {
                    val entry = labels.getJSONArray(i)
                    val line = entry.getJSONArray(1)
                    val pts = FloatArray(line.length() * 2)
                    for (j in 0 until line.length()) {
                        val p = line.getJSONArray(j)
                        pts[2 * j] = ((p.getDouble(1) - layer.lon) * k).toFloat()
                        pts[2 * j + 1] = ((p.getDouble(0) - layer.lat) * M_PER_DEG_LAT).toFloat()
                    }
                    addLabel(layer, entry.getString(0), pts)
                }
            }
            sortLabels(layer)
            basemapLayer = layer
        }
        invalidate()
    }

    /** Other nodes' footprints, replacing the last set. */
    fun setPeers(list: List<PeerFootprint>) {
        val keep = list.map { it.nodeId }.toSet()
        peerSlots.keys.retainAll(keep)
        val out = ArrayList<PeerShape>()
        for (p in list) {
            val slot = peerSlots.getOrPut(p.nodeId) {
                val used = peerSlots.values.toSet()
                (0 until peerSatColours.size).firstOrNull { it !in used }
                    ?: (peerSlots.size % peerSatColours.size)
            }
            val layer = Layer(p.covLat, p.covLon)
            p.coverage?.let { cov ->
                for (i in 0 until cov.length()) {
                    val c = cov.getJSONObject(i)
                    val az = Math.toRadians(c.optDouble("az"))
                    val r = c.optDouble("range").toFloat()
                    val x = r * sin(az).toFloat()
                    val y = r * cos(az).toFloat()
                    if (i == 0) layer.reach.moveTo(x, y) else layer.reach.lineTo(x, y)
                }
                layer.reach.close()
                layer.hasCoverage = cov.length() > 0
            }
            out.add(PeerShape(p, layer, slot))
        }
        peers = out
        invalidate()
    }

    /** Downloaded areas from coverage.list_areas(), outlined on the map. */
    fun setAreas(list: JSONArray?) {
        val out = ArrayList<SavedArea>()
        if (list != null) {
            for (i in 0 until list.length()) {
                val a = list.getJSONObject(i)
                out.add(SavedArea(
                    doubleArrayOf(a.optDouble("south"), a.optDouble("west"),
                                  a.optDouble("north"), a.optDouble("east")),
                    a.optString("file"), a.optString("kind"),
                    a.optString("source"), a.optInt("buildings")))
            }
        }
        areas = out
        invalidate()
    }

    /** The saved areas whose outline holds (lat, lon), smallest first. */
    fun areasAt(lat: Double, lon: Double): List<SavedArea> =
        areas.filter { lat >= it.box[0] && lon >= it.box[1] && lat <= it.box[2] && lon <= it.box[3] }
            .sortedBy { (it.box[2] - it.box[0]) * (it.box[3] - it.box[1]) }

    /** Centre the view on a point, keeping the zoom (used when there is no
     *  coverage result yet, e.g. no GPS fix but a downloaded area exists). */
    fun centerOn(lat: Double, lon: Double) {
        viewLat = lat
        viewLon = lon
        invalidate()
        settle()
    }

    fun clearSelection() {
        selection = null
        onSelection?.invoke(null)
        invalidate()
    }

    fun currentSelection(): DoubleArray? = selection?.copyOf()

    /** (south, west, north, east) currently in view, or null before layout. */
    fun viewport(): DoubleArray? {
        if (viewLat.isNaN() || width == 0) return null
        val (n, w) = toLatLon(0f, 0f)
        val (s, e) = toLatLon(width.toFloat(), height.toFloat())
        return doubleArrayOf(s, w, n, e)
    }

    private fun addLines(path: Path, arr: JSONArray?, close: Boolean) {
        if (arr == null) return
        for (i in 0 until arr.length()) {
            val line = arr.getJSONArray(i)
            if (line.length() < 2) continue
            for (j in 0 until line.length()) {
                val pt = line.getJSONArray(j)
                val x = pt.getDouble(0).toFloat()
                val y = pt.getDouble(1).toFloat()
                if (j == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            if (close) path.close()
        }
    }

    // --- projection ----------------------------------------------------------

    private fun metresPerDegLon(lat: Double) = M_PER_DEG_LON_EQ * cos(Math.toRadians(lat))

    private fun toScreen(lat: Double, lon: Double): Pair<Float, Float> {
        val x = (lon - viewLon) * metresPerDegLon(viewLat) / mPerPx
        val y = (lat - viewLat) * M_PER_DEG_LAT / mPerPx
        return Pair((width / 2f + x).toFloat(), (height / 2f - y).toFloat())
    }

    private fun toLatLon(px: Float, py: Float): Pair<Double, Double> {
        val x = (px - width / 2f) * mPerPx
        val y = (height / 2f - py) * mPerPx
        return Pair(viewLat + y / M_PER_DEG_LAT, viewLon + x / metresPerDegLon(viewLat))
    }

    private fun clampZoom(v: Double) = v.coerceIn(minMPerPx, maxMPerPx)

    // --- drawing -------------------------------------------------------------

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val w = MeasureSpec.getSize(widthMeasureSpec)
        setMeasuredDimension(w, w)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        // A model set before the first layout could not fit itself to a
        // width it did not have yet.
        val m = model
        if (oldw == 0 && m != null && !m.has("error")) {
            mPerPx = clampZoom(2.2 * m.optDouble("radius_m", 2000.0) / min(w, h))
        }
        settle()
    }

    private val layerMatrix = Matrix()
    private val scratch = Path()

    /** Draw a layer's metre-space path at screen scale. The transform is
     *  applied to the PATH on the CPU, not to the canvas: before Android 9
     *  the renderer rasterised a path into a texture sized in the path's own
     *  units, so a canvas-scaled path thousands of metres across exceeded the
     *  texture limit and silently did not draw. minSdk is 26. */
    private fun drawInLayer(canvas: Canvas, layer: Layer, src: Path, paint: Paint) {
        val (ax, ay) = toScreen(layer.lat, layer.lon)
        val s = (1.0 / mPerPx).toFloat()
        layerMatrix.setScale(s, -s)
        layerMatrix.postTranslate(ax, ay)
        src.transform(layerMatrix, scratch)
        canvas.drawPath(scratch, paint)
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), bgPaint)
        if (viewLat.isNaN()) {
            canvas.drawText("No map yet — use \"My location\" or long-press after panning",
                width / 2f, height / 2f, textPaint)
            return
        }

        if (satellite) {
            drawImagery(canvas)
            // Streets as fine lines: the photo already shows the buildings,
            // so their footprints would only hide it.
            basemapLayer?.let { b -> drawInLayer(canvas, b, b.roads, satRoad) }
        } else {
            basemapLayer?.let { b -> drawInLayer(canvas, b, b.roads, basemapPaint) }
        }
        val m0 = modelLayer
        m0?.let { m ->
            if (satellite) {
                drawInLayer(canvas, m, m.roads, satRoad)
            } else {
                drawInLayer(canvas, m, m.roads, roadPaint)
                // Buildings below ~0.5 px are noise; skip them when zoomed
                // far out rather than painting a grey smear.
                if (mPerPx < 12) drawInLayer(canvas, m, m.buildings, buildingFill)
            }
            if (m.hasCoverage) {
                val sat = if (modelIsLora) satLora else satFm
                satCoverageFill.color = Color.argb(90, Color.red(sat), Color.green(sat), Color.blue(sat))
                satCoverageLine.color = sat
                coverageFill.color = if (modelIsLora) streetLoraFill else streetFmFill
                coverageLine.color = if (modelIsLora) streetLoraLine else streetFmLine
                if (satellite) {
                    drawInLayer(canvas, m, m.reach, satCoverageFill)
                    drawInLayer(canvas, m, m.reach, satCoverageUnder)
                    drawInLayer(canvas, m, m.reach, satCoverageLine)
                } else {
                    drawInLayer(canvas, m, m.reach, coverageFill)
                    drawInLayer(canvas, m, m.reach, coverageLine)
                }
                // The line-of-sight outline (each result's "los") is no longer
                // drawn: it was read as a second coverage area. The numbers
                // are still in the result.
            }
        }
        // Over the operator's own fill, so an outline is not tinted into it,
        // and under the operator's own point and rings.
        if (showPeers) drawPeerOutlines(canvas)
        m0?.let { m ->
            val (tx, ty) = toScreen(m.lat, m.lon)
            drawRings(canvas, tx, ty)
            if (satellite) canvas.drawCircle(tx, ty, dp(8f), meRing)
            canvas.drawCircle(tx, ty, dp(6f), mePaint)
        }
        drawStreetLabels(canvas)
        // Names after the street labels: who a shape belongs to matters more
        // than which street it crosses.
        if (showPeers) drawPeerMarkers(canvas)
        for (a in areas) {
            if (satellite) drawBox(canvas, a.box, null, satAreaUnder)
            drawBox(canvas, a.box, null, if (satellite) satAreaLine else areaLine)
        }
        selection?.let { drawBox(canvas, it, selectFill, selectLine) }

        val attribution = if (satellite)
            "${ImageryTiles.ATTRIBUTION} · © OpenStreetMap · © Overture Maps"
        else "© OpenStreetMap · © Overture Maps Foundation"
        if (satellite) {
            // Light panels under the fixed text so it reads over any photo.
            canvas.drawRect(0f, height - dp(28f), width.toFloat(), height.toFloat(), panelPaint)
            canvas.drawRect(dp(8f), dp(4f), dp(30f), dp(21f), panelPaint)
        }
        canvas.drawText("N ↑", dp(18f), dp(16f), textPaint)
        drawScale(canvas)
        canvas.drawText(attribution, dp(4f), height - dp(4f), smallText)
        if (selectMode) {
            if (satellite) canvas.drawRect(width / 2f - dp(80f), dp(4f),
                                           width / 2f + dp(80f), dp(21f), panelPaint)
            canvas.drawText("Drag to select an area", width / 2f, dp(16f), textPaint)
        }
    }

    // --- imagery -------------------------------------------------------------

    private fun mercTileX(lon: Double, n: Double) = (lon + 180.0) / 360.0 * n
    private fun mercTileY(lat: Double, n: Double): Double {
        val r = Math.toRadians(lat.coerceIn(-85.0511, 85.0511))
        return (1.0 - ln(tan(r) + 1.0 / cos(r)) / PI_D) / 2.0 * n
    }
    private fun tileLat(y: Int, n: Double) = Math.toDegrees(atan(sinh(PI_D * (1.0 - 2.0 * y / n))))
    private fun tileLon(x: Int, n: Double) = x / n * 360.0 - 180.0

    private val srcRect = Rect()
    private val dstRect = RectF()

    /** Imagery tiles under the viewport, at the zoom whose pixels best match
     *  the screen's (capped at what the service serves). A tile still on its
     *  way is covered by a stretched ancestor already in memory, so panning
     *  shows a blurry patch rather than a hole. */
    private fun drawImagery(canvas: Canvas) {
        val ideal = ln(156543.03 * cos(Math.toRadians(viewLat)) / mPerPx) / ln(2.0)
        val z = ceil(ideal - 0.3).toInt().coerceIn(1, ImageryTiles.MAX_ZOOM)
        val n = (1 shl z).toDouble()
        val v = viewport() ?: return
        val x0 = floor(mercTileX(v[1], n)).toInt()
        val x1 = floor(mercTileX(v[3], n)).toInt()
        val y0 = floor(mercTileY(v[2], n)).toInt()
        val y1 = floor(mercTileY(v[0], n)).toInt()
        if ((x1 - x0 + 1) * (y1 - y0 + 1) > 64) return        // far out: streets only
        for (tx in x0..x1) for (ty in y0..y1) {
            val (l, t) = toScreen(tileLat(ty, n), tileLon(tx, n))
            val (r, b) = toScreen(tileLat(ty + 1, n), tileLon(tx + 1, n))
            dstRect.set(l, t, r, b)
            val bmp = imagery.get(z, tx, ty)
            if (bmp != null) {
                canvas.drawBitmap(bmp, null, dstRect, bitmapPaint)
                continue
            }
            // Fall back to the nearest ancestor in memory, cropped.
            for (up in 1..6) {
                if (z - up < 1) break
                val parent = imagery.peek(z - up, tx shr up, ty shr up) ?: continue
                val cells = 1 shl up
                val size = parent.width / cells
                val cx = (tx - ((tx shr up) shl up)) * size
                val cy = (ty - ((ty shr up) shl up)) * size
                srcRect.set(cx, cy, cx + size, cy + size)
                canvas.drawBitmap(parent, srcRect, dstRect, bitmapPaint)
                break
            }
        }
        canvas.drawRect(0f, 0f, width.toFloat(), height.toFloat(), imageryDim)
    }

    /** Distance rings around the transmitter, so the green reach can be
     *  read as a number of metres as well as against the street names. */
    private fun drawRings(canvas: Canvas, tx: Float, ty: Float) {
        for (r in RING_METRES) {
            val rp = (r / mPerPx).toFloat()
            if (rp < dp(18f) || rp > 2 * max(width, height)) continue
            val label = if (r >= 1000) "${trimKm(r / 1000.0)} km" else "${r.toInt()} m"
            if (satellite) {
                canvas.drawCircle(tx, ty, rp, satRingUnder)
                canvas.drawCircle(tx, ty, rp, satRing)
                textHalo.textSize = ringText.textSize
                canvas.drawText(label, tx + rp + dp(2f), ty - dp(2f), textHalo)
                ringText.color = Color.WHITE
            } else {
                canvas.drawCircle(tx, ty, rp, ringPaint)
                ringText.color = col(R.color.draw_ink_soft)
            }
            canvas.drawText(label, tx + rp + dp(2f), ty - dp(2f), ringText)
        }
    }

    private fun peerColour(shape: PeerShape) =
        if (satellite) peerSatColours[shape.colour % peerSatColours.size]
        else peerStreetColours[shape.colour % peerStreetColours.size]

    private fun drawPeerOutlines(canvas: Canvas) {
        for (shape in peers) {
            if (!shape.layer.hasCoverage) continue
            peerLine.color = peerColour(shape)
            if (satellite) drawInLayer(canvas, shape.layer, shape.layer.reach, peerUnder)
            drawInLayer(canvas, shape.layer, shape.layer.reach, peerLine)
        }
    }

    /** Each peer's point and its name with how old the position is -- the
     *  outline is drawn from where the node WAS, and the age says how much
     *  to trust that. */
    private fun drawPeerMarkers(canvas: Canvas) {
        for (shape in peers) {
            val p = shape.p
            val (x, y) = toScreen(p.lat, p.lon)
            if (x < -dp(40f) || y < -dp(40f) || x > width + dp(40f) || y > height + dp(40f)) continue
            val c = peerColour(shape)
            peerDot.color = if (satellite) Color.WHITE else col(R.color.draw_ink)
            canvas.drawCircle(x, y, dp(6.5f), peerDot)
            peerDot.color = c
            canvas.drawCircle(x, y, dp(4.5f), peerDot)
            val mins = (p.ageS / 60.0).toInt()
            val age = if (mins < 1) "now" else "$mins min ago"
            val label = if (p.note != null) "${p.name} · $age · ${p.note}" else "${p.name} · $age"
            peerHalo.color = if (satellite) Color.argb(220, 0, 0, 0) else col(R.color.draw_background)
            peerText.color = c
            canvas.drawText(label, x + dp(9f), y + dp(4f), peerHalo)
            canvas.drawText(label, x + dp(9f), y + dp(4f), peerText)
        }
    }

    private fun trimKm(v: Double) = if (v % 1.0 == 0.0) v.toInt().toString() else v.toString()

    private val labelMeasure = PathMeasure()
    private val labelPath = Path()
    private val labelSeg = Path()
    private val labelRev = Path()
    private val labelPos = FloatArray(2)
    private val placed = ArrayList<RectF>()

    /** Street names along the streets, model layer first (its streets are
     *  the detailed ones), each name once, skipping any label that would
     *  overlap one already placed. Hidden when zoomed out far enough that
     *  they would only be clutter. */
    private fun drawStreetLabels(canvas: Canvas) {
        if (mPerPx > LABEL_MAX_M_PER_PX) return
        placed.clear()
        val done = HashSet<String>()
        val textH = labelText.textSize
        for (layer in listOfNotNull(modelLayer, basemapLayer)) {
            val (ax, ay) = toScreen(layer.lat, layer.lon)
            val s = (1.0 / mPerPx).toFloat()
            // Viewport in this layer's metres, for the cheap rejection.
            val vMinX = (-ax) / s; val vMaxX = (width - ax) / s
            val vMinY = (ay - height) / s; val vMaxY = ay / s
            for ((name, pieces) in layer.labels) {
                if (placed.size >= MAX_LABELS) return
                if (name in done) continue
                val textW = labelText.measureText(name)
                for (pc in pieces) {
                    if (pc.lengthM * s < textW + dp(4f)) break    // sorted: none longer
                    if (pc.maxX < vMinX || pc.minX > vMaxX ||
                        pc.maxY < vMinY || pc.minY > vMaxY) continue
                    // Screen path of the whole piece.
                    val n = pc.pts.size / 2
                    labelPath.reset()
                    for (j in 0 until n) {
                        val x = ax + pc.pts[2 * j] * s
                        val y = ay - pc.pts[2 * j + 1] * s
                        if (j == 0) labelPath.moveTo(x, y) else labelPath.lineTo(x, y)
                    }
                    labelMeasure.setPath(labelPath, false)
                    val len = labelMeasure.length
                    val span = textW + dp(4f)
                    if (len < span) continue
                    // Just the stretch the label will sit on, centred.
                    val from = (len - span) / 2
                    labelMeasure.getPosTan(from, labelPos, null)
                    val sx = labelPos[0]; val sy = labelPos[1]
                    labelMeasure.getPosTan(from + span, labelPos, null)
                    val ex = labelPos[0]; val ey = labelPos[1]
                    // Too curved to read: text on a bend comes out scrambled.
                    // The chord across the stretch must be most of its length.
                    if (kotlin.math.hypot(ex - sx, ey - sy) < 0.92f * span) continue
                    val mx = (sx + ex) / 2; val my = (sy + ey) / 2
                    if (mx < 0 || mx > width || my < 0 || my > height) continue
                    val half = textW / 2 + dp(4f)
                    val box = RectF(mx - half, my - half, mx + half, my + half)
                    // Not under the attribution/scale strip or the N marker.
                    if (box.bottom > height - dp(28f) || (box.top < dp(22f) && box.left < dp(34f))) continue
                    if (placed.any { RectF.intersects(it, box) }) continue
                    // Readable direction, decided by THIS stretch, not the
                    // street's end points: left-to-right, or bottom-to-top
                    // when near vertical (the map convention).
                    val dx = ex - sx; val dy = ey - sy
                    val reverse = if (abs(dx) < 0.3f * span) dy > 0 else dx < 0
                    labelSeg.reset()
                    labelMeasure.getSegment(from, from + span, labelSeg, true)
                    if (reverse) {
                        // Rebuild the stretch backwards from sampled points.
                        labelRev.reset()
                        val steps = 12
                        for (k in 0..steps) {
                            labelMeasure.getPosTan(from + span * (steps - k) / steps, labelPos, null)
                            if (k == 0) labelRev.moveTo(labelPos[0], labelPos[1])
                            else labelRev.lineTo(labelPos[0], labelPos[1])
                        }
                    }
                    val onPath = if (reverse) labelRev else labelSeg
                    // Streets: dark text, white halo. Hybrid: the reverse, which
                    // is what reads over a photo.
                    labelHalo.color = if (satellite) Color.argb(220, 0, 0, 0) else col(R.color.draw_background)
                    labelText.color = if (satellite) Color.WHITE else col(R.color.draw_ink)
                    val h = dp(2f)
                    canvas.drawTextOnPath(name, onPath, h, textH / 3, labelHalo)
                    canvas.drawTextOnPath(name, onPath, h, textH / 3, labelText)
                    placed.add(box)
                    done.add(name)
                    break
                }
            }
        }
    }

    private fun drawBox(canvas: Canvas, b: DoubleArray, fill: Paint?, line: Paint) {
        val (x0, y0) = toScreen(b[2], b[1])   // north-west
        val (x1, y1) = toScreen(b[0], b[3])   // south-east
        if (fill != null) canvas.drawRect(x0, y0, x1, y1, fill)
        canvas.drawRect(x0, y0, x1, y1, line)
    }

    private fun drawScale(canvas: Canvas) {
        // The largest 1/2/5 x 10^n metres that fits in a quarter of the width.
        val target = width / 4.0 * mPerPx
        val mag = 10.0.pow(floor(log10(target)))
        val nice = listOf(5.0, 2.0, 1.0).map { it * mag }.first { it <= target }
        val len = (nice / mPerPx).toFloat()
        val x1 = width - dp(12f)
        val y = height - dp(14f)
        canvas.drawLine(x1 - len, y, x1, y, scalePaint)
        canvas.drawLine(x1 - len, y - dp(4f), x1 - len, y, scalePaint)
        canvas.drawLine(x1, y - dp(4f), x1, y, scalePaint)
        val label = if (nice >= 1000) "${(nice / 1000).let { if (it % 1.0 == 0.0) it.toInt().toString() else it.toString() }} km"
                    else "${nice.toInt()} m"
        canvas.drawText(label, x1 - len / 2, y - dp(4f), textPaint)
    }

    // --- gestures ------------------------------------------------------------

    private val scaleDetector = ScaleGestureDetector(context,
        object : ScaleGestureDetector.SimpleOnScaleGestureListener() {
            override fun onScale(d: ScaleGestureDetector): Boolean {
                if (viewLat.isNaN()) return false
                // Zoom about the pinch focus, so the point under the fingers
                // stays under the fingers.
                val (flat, flon) = toLatLon(d.focusX, d.focusY)
                mPerPx = clampZoom(mPerPx / d.scaleFactor)
                val (nlat, nlon) = toLatLon(d.focusX, d.focusY)
                viewLat += flat - nlat
                viewLon += flon - nlon
                invalidate()
                return true
            }
        })

    private val gestureDetector = GestureDetector(context,
        object : GestureDetector.SimpleOnGestureListener() {
            override fun onDown(e: MotionEvent) = true

            override fun onScroll(e1: MotionEvent?, e2: MotionEvent,
                                  dx: Float, dy: Float): Boolean {
                if (selectMode || viewLat.isNaN() || scaleDetector.isInProgress) return false
                viewLat -= dy * mPerPx / M_PER_DEG_LAT
                viewLon += dx * mPerPx / metresPerDegLon(viewLat)
                invalidate()
                return true
            }

            override fun onDoubleTap(e: MotionEvent): Boolean {
                if (viewLat.isNaN()) return false
                mPerPx = clampZoom(mPerPx / 2)
                invalidate()
                settle()
                return true
            }

            override fun onLongPress(e: MotionEvent) {
                if (selectMode || viewLat.isNaN()) return
                val (lat, lon) = toLatLon(e.x, e.y)
                performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
                onLongPress?.invoke(lat, lon)
            }
        })

    @SuppressLint("ClickableViewAccessibility")
    override fun onTouchEvent(event: MotionEvent): Boolean {
        // The tab lives in a vertical ScrollView; without this the first
        // vertical drag on the map scrolls the page instead of panning.
        if (event.actionMasked == MotionEvent.ACTION_DOWN) {
            parent?.requestDisallowInterceptTouchEvent(true)
        }
        scaleDetector.onTouchEvent(event)
        // A second finger means a pinch, not the end of a box: drop the half-
        // drawn box rather than finishing it wherever the last finger lifts.
        if (event.pointerCount > 1) dragStart = null
        if (selectMode && !viewLat.isNaN() && !scaleDetector.isInProgress &&
            event.pointerCount == 1) {
            handleSelectDrag(event)
        } else {
            gestureDetector.onTouchEvent(event)
        }
        if (event.actionMasked == MotionEvent.ACTION_UP ||
            event.actionMasked == MotionEvent.ACTION_CANCEL) {
            parent?.requestDisallowInterceptTouchEvent(false)
            settle()
        }
        return true
    }

    private fun handleSelectDrag(event: MotionEvent) {
        val (lat, lon) = toLatLon(event.x, event.y)
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> dragStart = doubleArrayOf(lat, lon)
            MotionEvent.ACTION_MOVE, MotionEvent.ACTION_UP -> {
                val st = dragStart ?: return
                selection = doubleArrayOf(min(st[0], lat), min(st[1], lon),
                                          max(st[0], lat), max(st[1], lon))
                invalidate()
                if (event.actionMasked == MotionEvent.ACTION_UP) {
                    dragStart = null
                    // A tap, not a drag: too small to be meant as an area.
                    val box = selection!!
                    val hM = (box[2] - box[0]) * M_PER_DEG_LAT
                    val wM = (box[3] - box[1]) * metresPerDegLon(viewLat)
                    if (abs(hM) < 20 * mPerPx && abs(wM) < 20 * mPerPx) {
                        clearSelection()
                    } else {
                        onSelection?.invoke(box.copyOf())
                    }
                }
            }
        }
    }

    private fun settle() {
        val v = viewport() ?: return
        onViewportSettled?.invoke(v[0], v[1], v[2], v[3])
    }

    companion object {
        // The same equirectangular constants coverage.py projects with, so a
        // model's metres and this view's degrees agree.
        private const val M_PER_DEG_LAT = 110540.0
        private const val M_PER_DEG_LON_EQ = 111320.0
        private const val PI_D = Math.PI
        private val RING_METRES = listOf(250.0, 500.0, 1000.0, 1500.0, 2000.0)
        // Above this zoom (metres per pixel) a 10 dp label covers ~200 m of
        // street and the map is all text.
        private const val LABEL_MAX_M_PER_PX = 8.0
        private const val MAX_LABELS = 60
    }
}
