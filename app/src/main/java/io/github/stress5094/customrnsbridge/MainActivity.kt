package io.github.stress5094.customrnsbridge

import android.Manifest
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.location.Location
import android.location.LocationManager
import android.media.AudioAttributes
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.AutomaticGainControl
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.text.InputType
import android.util.Log
import android.view.Gravity
import android.view.View
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.chaquo.python.Python
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

/**
 * Live status screen: what this node is, whether the radio and ATAK are up,
 * which peers are currently reachable, and whether any one-shot message failed
 * to get through.
 *
 * The peer list is driven by the beacon `rns_bridge.py` broadcasts every 15s
 * on the same RNS destination real CoT uses. That matters for what this screen
 * actually means: a peer showing CONNECTED is not a guess from configuration,
 * it is proof that a packet crossed the air from that device within the last
 * 45 seconds, over exactly the path CoT depends on.
 *
 * Status is pulled from Python via Chaquopy rather than pushed, because the
 * bridge has no reason to know a UI exists -- it keeps running identically
 * whether or not this Activity is alive, which is the whole point of it being
 * a foreground service.
 */
class MainActivity : AppCompatActivity() {
    // Status colours from resources, so the dark theme (values-night) and the
    // 4.5:1 contrast rule hold: green / amber / red / grey, each with its tile.
    private val COLOR_UP by lazy { getColor(R.color.status_good) }
    private val COLOR_DOWN by lazy { getColor(R.color.status_bad) }
    private val COLOR_IDLE by lazy { getColor(R.color.status_idle) }
    private val COLOR_HEADING by lazy { getColor(R.color.status_heading) }
    private val COLOR_WARN by lazy { getColor(R.color.status_warn) }
    private val TINT_UP by lazy { getColor(R.color.status_good_container) }
    private val TINT_WARN by lazy { getColor(R.color.status_warn_container) }
    private val TINT_DOWN by lazy { getColor(R.color.status_bad_container) }
    private val TINT_TILE by lazy { getColor(R.color.status_tile) }


    companion object {
        /** The main screen while it is showing, for settings offers that
         *  arrive over WiFi (SettingsShareUi.onEvent). Null otherwise. */
        @Volatile var visible: MainActivity? = null

        /** How long Quit waits for the services' onDestroy (which unkeys PTT) before
         *  ending the process. See quitApp(). */
        private const val QUIT_GRACE_MS = 1500L

        private const val TAG = "MainActivity"
        private const val REFRESH_MS = 2000L

        private const val REQ_BLUETOOTH = 41
        // 42 and 43 were the removed FM receive tests' microphone-permission
        // requests. NOT REUSED: an in-flight request from a pre-2026-10-01
        // build could still deliver against either code, and a result
        // delivered to the wrong handler is a permission silently treated as
        // granted or denied. 44 is the FM link's own.
        private const val REQ_RECORD_AUDIO_FM = 44
        // The Coverage tab's location permission. A fresh number, same "never
        // reuse" rule as above.
        private const val REQ_LOCATION = 45
        // Notifications (Android 13+). Same "never reuse" rule.
        private const val REQ_NOTIFICATIONS = 46

        // Coverage following: recompute after this much movement. Smaller is
        // just churn -- the result is a 2 km picture and a fix wanders ~10 m.
        private const val FOLLOW_RECOMPUTE_M = 100f
        private const val FOLLOW_MIN_TIME_MS = 5_000L
        private const val FOLLOW_MIN_DISTANCE_M = 20f
        private const val PEER_REFRESH_MS = 15_000L

        // Thresholds are the ones the 2026-08-16 stress bench measured, not
        // taste. Airtime: the channel saturates near 32% and delivery collapses
        // there. WAITING is DEFAULT_CONGESTION_INFLIGHT -- every healthy load
        // step peaked at one message in flight and every collapsed one sat at
        // 21+, so a red tile means the node is now suppressing its own retries.
        // Keeping these in step with reliable.py is the point; a screen that
        // calls a node healthy while the code is backing off is worse than no
        // screen.
        //
        // SNR IS NOT FROM THAT BENCH, which ran at 12.5-14 dB throughout and
        // could not have measured a signal threshold. It was 10/3 until
        // 2026-10-14, which graded the desk Meshtastic link (~6 dB) as weak.
        // Now 0/-7, chosen against the one real-distance reading there is: the
        // 2026-08-21 drive decoded a beacon at -11.25 dB on SF8 (docs/benches.md).
        // Still not a measured threshold, and one figure for every preset --
        // SF8's floor is ~-10 dB, Long Fast's (SF11) ~-17.5. mesh_view.py holds
        // the same numbers and a test keeps the two equal.
        private const val AIRTIME_WARN = 10.0
        private const val AIRTIME_BAD = 25.0
        private const val WAITING_WARN = 2
        private const val WAITING_BAD = 4
        private const val SNR_WARN = 0.0
        private const val SNR_BAD = -7.0

        private const val STATE_UP = 0
        private const val STATE_WARN = 1
        private const val STATE_DOWN = 2

        private const val WRAP = LinearLayout.LayoutParams.WRAP_CONTENT

        // Must match mesh.MISMATCH_THRESHOLD. One rejection is noise; two in a
        // row with nothing decrypting between them is a key that disagrees.
        private const val MISMATCH_FRAMES = 2
    }

    // Survives the 2 s status refresh, which rebuilds localBox from scratch.
    private var diagnosticsOpen = false

    private val handler = Handler(Looper.getMainLooper())

    // Settings sharing (docs/settings-sharing.md). The scanner is registered
    // here, at construction: an activity-result launcher registered later
    // than onCreate throws.
    private val qrScan = registerForActivityResult(com.journeyapps.barcodescanner.ScanContract()) { r ->
        r.contents?.let { shareUi.onScanned(it) }
    }
    private val shareUi: SettingsShareUi by lazy {
        SettingsShareUi(this, handler) {
            qrScan.launch(com.journeyapps.barcodescanner.ScanOptions()
                .setDesiredBarcodeFormats(com.journeyapps.barcodescanner.ScanOptions.QR_CODE)
                .setPrompt("Scan the settings QR code on the other phone")
                .setBeepEnabled(false)
                .setOrientationLocked(false))
        }
    }

    /** A settings offer arrived over WiFi. */
    fun showSharedSettingsOffers() = shareUi.showWaitingOffers()

    /** Everything else about sharing settings lives on the Share tab. */
    fun showShareTab() {
        // With the tab hidden (the default), the offers open as a dialog.
        if (RadioLink.shareTabEnabled(this)) showTab(4) else shareUi.showWaitingOffers()
    }

    /** A settings host was heard on WiFi and waits for this phone's say-so. */
    fun showFoundSettingsHosts() = shareUi.showFoundHosts()

    /** After a shared change lands: labels that show settings are redrawn
     *  (the radio's and the Coverage picker's). */
    fun onSharedSettingsApplied() {
        if (::linkButton.isInitialized) renderLink()
        if (::coveragePicker.isInitialized) updateCoveragePickerLabels()
    }
    // Chaquopy calls cross into the interpreter and can block on its GIL;
    // doing that on the UI thread would jank the refresh.
    private val executor = Executors.newSingleThreadExecutor()

    private lateinit var localBox: LinearLayout
    private lateinit var peerBox: LinearLayout
    private lateinit var peerHeading: TextView
    private lateinit var messageBox: LinearLayout
    private lateinit var messageHeading: TextView
    private lateinit var keyLine: TextView
    private lateinit var logLine: TextView
    private lateinit var logToggle: Button
    private lateinit var surveyLine: TextView
    private lateinit var surveyButton: Button
    private lateinit var rangeLine: TextView
    private lateinit var rangeButton: Button
    private lateinit var linkButton: Button
    private lateinit var advancedBox: LinearLayout
    private lateinit var overrideNote: TextView
    private val overrideButtons = mutableListOf<Triple<String, String, Button>>()
    private lateinit var statusPage: View
    private lateinit var radioTestPage: View
    private lateinit var nav: com.google.android.material.bottomnavigation.BottomNavigationView
    private lateinit var meshPage: View
    private lateinit var meshDiagram: MeshDiagramView
    private lateinit var meshListBox: LinearLayout
    private lateinit var meshNote: TextView
    private lateinit var coveragePage: View
    private lateinit var sharePage: View
    private lateinit var coverageMap: CoverageMapView
    private lateinit var coverageStatus: TextView
    private lateinit var coveragePicker: Spinner
    private lateinit var coveragePickerAdapter: ArrayAdapter<String>
    // What the radio section was last read as, for the LoRa label; refreshed
    // with every computation since the radio dialog can change it any time.
    private var coverageLoraSummary: String? = null
    // One button for the whole download: tap, drag a box, confirm. (Was
    // Select area + Download area + Saved areas; a saved area is deleted by
    // long-pressing inside it now.)
    private lateinit var coverageDownloadButton: Button
    private lateinit var coveragePeerStatus: TextView
    private lateinit var coveragePeerToggle: android.widget.CompoundButton
    // Guards against stacking coverage runs: the fetch + compute is seconds
    // long, and a radio change mid-run must not queue a second one. Also set
    // for the whole of an area download, which is the longer of the two.
    private var coverageComputing = false
    // The transmitter point the operator chose by long-pressing the map, or
    // null for "this phone's GPS position".
    private var coverageTx: DoubleArray? = null
    // The bbox the street basemap was last fetched for, so a small pan does
    // not refetch.
    private var basemapBox: DoubleArray? = null
    private var basemapFetching = false
    // Coverage work gets its own threads: an area download is up to a minute
    // of network, and on the shared executor it would stall the status
    // page's refresh for all of it. The basemap is separate again so the map
    // can still be panned and oriented while a download runs.
    private val coverageExecutor = Executors.newSingleThreadExecutor()
    // FOLLOWING: while the Coverage tab is open and no point has been chosen
    // by hand (coverageTx == null), live fixes come in here, and a move of
    // more than FOLLOW_RECOMPUTE_M from the last computed point recomputes --
    // which brings the new place's streets and names with it.
    private var coverageFix: Location? = null
    private var coverageComputedAt: DoubleArray? = null
    private var followingLocation = false
    private val coverageLocationListener = object : android.location.LocationListener {
        override fun onLocationChanged(location: Location) {
            coverageFix = location
            maybeFollow()
        }
        // Declared explicitly: before API 30 these have no default bodies,
        // and a lambda listener would throw AbstractMethodError on minSdk 26.
        @Deprecated("platform callback")
        override fun onStatusChanged(provider: String?, status: Int, extras: Bundle?) {}
        override fun onProviderEnabled(provider: String) {}
        override fun onProviderDisabled(provider: String) {}
    }
    private val basemapExecutor = Executors.newSingleThreadExecutor()
    // OTHER NODES' FOOTPRINTS: every PEER_REFRESH_MS while the tab is open,
    // ask the bridge where peers last said they were and compute each one's
    // reach that is new, has moved FOLLOW_RECOMPUTE_M, or was drawn for
    // another radio. Its own thread, so a peer's area download (~35 s)
    // never holds up the operator's own map. peerStates is touched only on
    // peerExecutor.
    private val peerExecutor = Executors.newSingleThreadExecutor()
    private class PeerState(val lat: Double, val lon: Double, val radio: String,
                            val coverage: JSONArray?, val note: String?,
                            val minKm: Double, val maxKm: Double)
    private val peerStates = HashMap<String, PeerState>()
    private val peerWaitingSince = HashMap<String, Long>()   // see PeerQueue
    private var peerRefreshing = false
    private val peerRefresh = object : Runnable {
        override fun run() {
            if (currentTab != 3) return
            refreshPeers()
            handler.postDelayed(this, PEER_REFRESH_MS)
        }
    }
    // Which tab is showing, so the refresh only asks for the mesh picture
    // while somebody can see it.
    private var currentTab = 0
    // The last picture fetched, kept so a tap or a view toggle can redraw the
    // list without waiting up to 2 s for the next poll.
    private var lastMeshView: JSONObject? = null

    private val refresh = object : Runnable {
        override fun run() {
            executor.execute {
                val json = fetchStatus()
                // Fetched separately from the main status because a survey
                // runs when the bridge is otherwise idle, and its progress has
                // to keep updating even while fetchStatus() is returning
                // nothing useful.
                val sweep = try {
                    callPython("sweep_status")?.let { JSONObject(it) }
                } catch (e: Exception) {
                    null
                }
                val range = try {
                    callPython("range_test_status")?.let { JSONObject(it) }
                } catch (e: Exception) {
                    null
                }
                // Only while the Mesh tab is open: it is a separate call so the
                // picture costs nothing the rest of the time.
                val meshView = if (currentTab == 2) fetchMeshView() else null
                handler.post {
                    render(json)
                    renderSurvey(sweep)
                    renderRangeTest(range)
                    if (meshView != null) renderMesh(meshView)
                }
            }
            handler.postDelayed(this, REFRESH_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        PythonRnsService.startBridge(applicationContext)
        // Android 13+ drops every notification SILENTLY until the app asks,
        // and nothing did: settings offered over WiFi while ATAK is in front
        // went unseen on both phones. Asked on a fresh start only; Android
        // itself stops showing the dialog after two refusals.
        if (savedInstanceState == null && Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) !=
                PackageManager.PERMISSION_GRANTED
        ) {
            ActivityCompat.requestPermissions(
                this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQ_NOTIFICATIONS)
        }
        // Auto-started rather than left to the manual toggle: a field test
        // needs the log running from the moment the app comes up, including
        // the fresh process a bridge restart launches -- see restartApp()'s
        // own comment on why that has to kill and relaunch. start() already
        // no-ops if a recording is already in progress, so this is safe on
        // every onCreate() re-entry.
        LogRecorder.start(applicationContext)
        Log.i(TAG, "log recording resumed: ${LogRecorder.currentLogFile()?.name}")
        renderLog()
    }

    override fun onResume() {
        super.onResume()
        // Only poll while visible. The bridge itself is unaffected either way.
        handler.post(refresh)
        if (currentTab == 3) {
            startFollowingLocation()
            handler.removeCallbacks(peerRefresh)
            handler.post(peerRefresh)
        }
        registerCoverageFixHook()
        if (currentTab == 4) shareUi.pageVisible(true)
        visible = this
        // Opened from the "settings are waiting" notification, or came back
        // after one arrived: show it.
        if (SettingsShareUi.unseenOffer) showSharedSettingsOffers()
        else if (SettingsShareUi.unseenHost) showFoundSettingsHosts()
    }

    override fun onPause() {
        super.onPause()
        if (visible === this) visible = null
        shareUi.pageVisible(false)
        handler.removeCallbacks(refresh)
        stopFollowingLocation()
        handler.removeCallbacks(peerRefresh)
        coverageFixHook?.let { unregisterReceiver(it) }
        coverageFixHook = null
    }

    override fun onDestroy() {
        super.onDestroy()
        executor.shutdownNow()
        coverageExecutor.shutdownNow()
        basemapExecutor.shutdownNow()
        peerExecutor.shutdownNow()
    }

    private fun buildUi(): View {
        val statusRoot = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(20))
        }

        // The old "ATAK ↔ Reticulum bridge" heading lived here and said exactly
        // what the title bar directly above it already says, at the top of the
        // screen -- the most valuable space there is. The node's own identity
        // goes here instead, which is the thing you actually check first.
        localBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        statusRoot.addView(localBox)

        statusRoot.addView(buildKeySection())

        statusRoot.addView(buildSettingsControls())

        // Above the node list, and hidden entirely when there is nothing to
        // say. An undelivered message is the only thing on this screen the
        // user may actually need to act on, so it must not be something you
        // have to scroll a long peer list to find.
        messageHeading = TextView(this).apply {
            text = "Messages"
            Ui.heading(this)
            setPadding(0, dp(20), 0, dp(6))
            visibility = View.GONE
        }
        statusRoot.addView(messageHeading)

        messageBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
        }
        statusRoot.addView(messageBox)

        peerHeading = TextView(this).apply {
            text = "Nodes"
            Ui.heading(this)
            setPadding(0, dp(20), 0, dp(6))
        }
        statusRoot.addView(peerHeading)

        peerBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        statusRoot.addView(peerBox)
        statusPage = statusRoot

        radioTestPage = buildRadioTestControls().apply {
            visibility = View.GONE
        }
        meshPage = buildMeshPage().apply { visibility = View.GONE }
        coveragePage = buildCoveragePage().apply { visibility = View.GONE }
        sharePage = shareUi.buildPage().apply { visibility = View.GONE }

        val pages = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(statusPage)
            addView(radioTestPage)
            addView(meshPage)
            addView(coveragePage)
            addView(sharePage)
        }

        // The WHOLE page scrolls, not just the node list. Scrolling only the
        // node list was survivable while the controls sat above it, but in
        // landscape -- or on any short screen -- everything below the status
        // block is simply unreachable, buttons included. Found by trying to
        // press Survey on a phone lying on its side.
        //
        // Replaces the inner ScrollView rather than nesting inside it: two
        // scrolling containers on one axis fight each other for the gesture.
        val scroll = ScrollView(this).apply { addView(pages) }

        // Material 3's navigation bar, at the bottom, for the five top-level
        // destinations (Google's guidelines, 2026-10-22: a bar for 3-5
        // destinations, icon AND label, within reach of a thumb). Outside the
        // ScrollView so it never scrolls away.
        scroll.layoutParams = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f)
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            addView(scroll)
            addView(buildNavBar())
        }
    }

    private fun buildNavBar(): View {
        nav = com.google.android.material.bottomnavigation.BottomNavigationView(this).apply {
            // Labels on every item, not only the selected one: five short
            // words, and an operator in the field should not have to learn
            // the icons.
            labelVisibilityMode =
                com.google.android.material.navigation.NavigationBarView.LABEL_VISIBILITY_LABELED
            listOf("Status" to R.drawable.ic_nav_status,
                   "Debug" to R.drawable.ic_nav_debug,
                   "Mesh" to R.drawable.ic_nav_mesh,
                   "Coverage" to R.drawable.ic_nav_coverage,
                   "Share" to R.drawable.ic_nav_share).forEachIndexed { i, (label, icon) ->
                menu.add(android.view.Menu.NONE, i, i, label).setIcon(icon)
            }
            menu.findItem(4).isVisible = RadioLink.shareTabEnabled(this@MainActivity)
            setOnItemSelectedListener { item ->
                if (item.itemId != currentTab) showTab(item.itemId)
                true
            }
        }
        showTab(0)
        return nav
    }

    private fun showTab(index: Int) {
        currentTab = index
        statusPage.visibility = if (index == 0) View.VISIBLE else View.GONE
        radioTestPage.visibility = if (index == 1) View.VISIBLE else View.GONE
        meshPage.visibility = if (index == 2) View.VISIBLE else View.GONE
        coveragePage.visibility = if (index == 3) View.VISIBLE else View.GONE
        sharePage.visibility = if (index == 4) View.VISIBLE else View.GONE
        // Opened from code (showShareTab): the bar follows.
        if (::nav.isInitialized && nav.selectedItemId != index) nav.selectedItemId = index
        shareUi.pageVisible(index == 4)
        // Straight away rather than on the next 2 s tick, so the tab does not
        // open blank. A no-op before onCreate finishes -- the first resume
        // posts the refresh anyway.
        if (index == 2) executor.execute {
            val view = fetchMeshView()
            handler.post { if (view != null) renderMesh(view) }
        }
        handler.removeCallbacks(peerRefresh)
        if (index == 3) {
            refreshCoverage()
            startFollowingLocation()
            handler.post(peerRefresh)
        } else {
            stopFollowingLocation()
        }
    }

    /**
     * Mesh tab: a picture of who this phone hears, on which radio, and how
     * well, with the exact numbers listed underneath.
     *
     * Everything shown is decided in mesh_view.py; this only lays it out. The
     * probable-line switch is a per-phone convenience, so plain SharedPreferences,
     * separate from RadioLink's.
     */
    private fun buildMeshPage(): View {
        val prefs = getSharedPreferences("mesh_tab", Context.MODE_PRIVATE)
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(10), dp(20), dp(20))
        }

        meshDiagram = MeshDiagramView(this).apply {
            showProbable = prefs.getBoolean("probable", true)
            onSelect = { lastMeshView?.let { renderMeshList(it) } }
        }

        box.addView(com.google.android.material.materialswitch.MaterialSwitch(this).apply {
            text = "Show probable links between other nodes"
            textSize = 13f
            isChecked = meshDiagram.showProbable
            setOnCheckedChangeListener { _, checked ->
                meshDiagram.showProbable = checked
                prefs.edit().putBoolean("probable", checked).apply()
            }
        })

        meshNote = TextView(this).apply {
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(4), 0, dp(8))
            text = "Loading…"
        }
        box.addView(meshNote)
        box.addView(meshDiagram)

        box.addView(TextView(this).apply {
            text = "Links"
            Ui.heading(this)
            setPadding(0, dp(16), 0, dp(6))
        })
        meshListBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        box.addView(meshListBox)
        return box
    }

    /**
     * Coverage tab: how far the chosen radio reaches from a transmitter
     * point, drawn over the real streets and buildings.
     *
     * The point is this phone's GPS fix, or anywhere the operator long-
     * presses on the map. "Select area" lets them drag a box; "Download
     * area" then pulls Overture's buildings (real heights) and streets for it
     * ON THE PHONE (coverage.download_area -> overture_tiles) and keeps them
     * offline, so every later computation inside the box uses them.
     *
     * The heavy lifting is all coverage.py's; this lays out the controls and
     * hands results to CoverageMapView. Work runs on coverageExecutor.
     */
    private fun buildCoveragePage(): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(10), dp(20), dp(20))
        }

        // Keys must match RADIOS in coverage.py; the label is shown, the key
        // is what compute() receives (selectedCoverageRadio). The labels say
        // which settings each will be drawn with -- LoRa's from the radio
        // section, FM's from this tab's own settings -- and change with them.
        coveragePickerAdapter = ArrayAdapter(
            this@MainActivity, android.R.layout.simple_spinner_dropdown_item,
            ArrayList(coveragePickerLabels())
        )
        coveragePicker = Spinner(this).apply {
            adapter = coveragePickerAdapter
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
            // The last radio picked, kept across reboots. Set BEFORE the
            // listener, which also fires once during layout.
            setSelection(CoverageSettings.radioIndex(this@MainActivity)
                .coerceIn(0, coveragePickerAdapter.count - 1))
            onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
                override fun onItemSelected(parent: android.widget.AdapterView<*>?, view: View?,
                                            position: Int, id: Long) {
                    // Spinner also fires this once during layout, with the
                    // position it was given: only a real change recomputes.
                    if (position == CoverageSettings.radioIndex(this@MainActivity)) return
                    CoverageSettings.setRadioIndex(this@MainActivity, position)
                    refreshCoverage()
                    refreshPeers()
                }
                override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
            }
        }
        val pickerRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        pickerRow.addView(coveragePicker)
        pickerRow.addView(Ui.textButton(this, "Model…").apply {
            setOnClickListener { showCoverageSettings() }
        })
        box.addView(pickerRow)

        // Three to a row: less side padding than a lone button, so a label
        // like "Recompute" stays whole instead of breaking mid-word.
        fun smallButton(label: String, onClick: () -> Unit) = Ui.outlined(this, label).apply {
            setPadding(dp(8), paddingTop, dp(8), paddingBottom)
            maxLines = 2
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
                .apply { marginStart = dp(2); marginEnd = dp(2) }
            setOnClickListener { onClick() }
        }

        // Picking another radio recomputes by itself (the listener above);
        // a pick made while a run is going is queued, not raced -- see
        // coverageRecomputeWanted. Recompute stays for a run on demand.
        val row1 = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        row1.addView(smallButton("My location") {
            coverageTx = null
            // Start again from the system's own fix: a held live fix may be a
            // debug test fix, or simply stale.
            coverageFix = null
            refreshCoverage()
        })
        row1.addView(smallButton("Recompute") { refreshCoverage() })
        // Hybrid (satellite + street names) is the default view; the choice
        // is kept across reboots.
        row1.addView(smallButton(
            if (CoverageSettings.satellite(this)) "Streets view" else "Satellite view") {}.apply {
            setOnClickListener {
                coverageMap.satellite = !coverageMap.satellite
                CoverageSettings.setSatellite(this@MainActivity, coverageMap.satellite)
                text = if (coverageMap.satellite) "Streets view" else "Satellite view"
            }
        })
        box.addView(row1)

        val row2 = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        coverageDownloadButton = smallButton("Download an area for offline use") { toggleSelectMode() }
        row2.addView(coverageDownloadButton)
        box.addView(row2)

        coverageStatus = TextView(this).apply {
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(4), 0, dp(8))
            text = "Open to compute coverage at this phone's location. " +
                "Long-press the map to compute somewhere else."
        }
        box.addView(coverageStatus)

        coveragePeerToggle = com.google.android.material.materialswitch.MaterialSwitch(this).apply {
            text = "Show other nodes' coverage"
            textSize = 12f
            isChecked = CoverageSettings.showPeers(this@MainActivity)
            setOnCheckedChangeListener { _, on ->
                CoverageSettings.setShowPeers(this@MainActivity, on)
                coverageMap.showPeers = on
                coveragePeerStatus.visibility = if (on) View.VISIBLE else View.GONE
                if (on) refreshPeers()
            }
        }
        box.addView(coveragePeerToggle)
        coveragePeerStatus = TextView(this).apply {
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, 0, 0, dp(8))
            text = "No other nodes have reported a position yet."
        }
        box.addView(coveragePeerStatus)

        coverageMap = CoverageMapView(this).apply {
            onLongPress = { lat, lon -> onCoverageLongPress(lat, lon) }
            onSelection = { sel -> onCoverageSelection(sel) }
            onViewportSettled = { s, w, n, e -> maybeFetchBasemap(s, w, n, e) }
            satellite = CoverageSettings.satellite(this@MainActivity)
            showPeers = coveragePeerToggle.isChecked
        }
        if (!coveragePeerToggle.isChecked) coveragePeerStatus.visibility = View.GONE
        box.addView(coverageMap)
        refreshSavedAreas()
        return box
    }

    private fun coveragePickerLabels(): List<String> = listOf(
        "LoRa — " + (coverageLoraSummary ?: "the Radio settings"),
        "FM ${CoverageSettings.fmMhz(this)} MHz, ${CoverageSettings.fmW(this)} W",
    )

    private fun updateCoveragePickerLabels() {
        val pos = coveragePicker.selectedItemPosition
        coveragePickerAdapter.clear()
        coveragePickerAdapter.addAll(coveragePickerLabels())
        coveragePickerAdapter.notifyDataSetChanged()
        coveragePicker.setSelection(pos)
    }

    /**
     * Everything coverage.compute() is to model with, as its params JSON:
     * LoRa from the radio in use (rns_bridge.coverage_radio: what an RNode
     * reports, what a Meshtastic board is set to, else the app's Radio
     * settings), FM and the transmitter height from CoverageSettings. CALLS PYTHON -- background threads only. Without the
     * transmitter height when [forPeer], since that height is this phone's
     * antenna, not theirs (compute_peer ignores it anyway; leaving it out
     * keeps a height change from looking like a radio change to the peer
     * loop).
     */
    private fun coverageParams(forPeer: Boolean = false): JSONObject {
        val p = CoverageSettings.params(this)
        if (forPeer) p.remove(CoverageSettings.KEY_TX_HEIGHT_M)
        try {
            val r = JSONObject(callPython("coverage_radio") ?: "{}")
            if (r.has("preset")) {
                p.put("lora_preset", r.optString("preset"))
                p.put("lora_freq_mhz", r.optString("frequency_mhz"))
                p.put("lora_tx_dbm", r.optInt("txpower_dbm"))
                p.put("lora_source", r.optString("source"))
                val summary = "${r.optString("preset")}, ${r.optString("frequency_mhz")} MHz, " +
                    "${r.optInt("txpower_dbm")} dBm (${r.optString("source")})"
                handler.post {
                    if (summary != coverageLoraSummary) {
                        coverageLoraSummary = summary
                        updateCoveragePickerLabels()
                    }
                }
            }
        } catch (e: Exception) {
            // No bridge yet: compute() falls back to RADIOS' LoRa, and the
            // result's label says which numbers were used.
        }
        return p
    }

    /** The FM frequency and power and the transmitter height, saved across
     *  reboots. */
    private fun showCoverageSettings() {
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(12), dp(24), 0)
        }
        fun field(label: String, value: String): EditText {
            layout.addView(TextView(this).apply {
                text = label
                textSize = 12f
                setTextColor(COLOR_HEADING)
                setPadding(0, dp(10), 0, 0)
            })
            return EditText(this).apply {
                inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
                setSingleLine()
                setText(value)
                layout.addView(this)
            }
        }
        val fm = field("FM frequency (MHz) — VHF or UHF", CoverageSettings.fmMhz(this))
        val fmW = field("FM transmit power (W)", CoverageSettings.fmW(this))
        // One height for both radios, and said so on the field: it sits
        // below the FM fields, and an operator changing it for FM would
        // otherwise not expect the LoRa map to move too.
        val height = field("Transmitter antenna height above ground (m) — " +
                           "applies to LoRa AND FM coverage",
                           CoverageSettings.txHeightM(this))
        layout.addView(TextView(this).apply {
            text = "The antenna height above is used for this phone's coverage on both " +
                "LoRa and FM. The receiver is always modelled as a handheld at 1.5 m, and other " +
                "nodes' coverage keeps 1.5 m for their transmitter too. LoRa uses the " +
                "radio in use: " + (coverageLoraSummary ?: "its preset, frequency and power") +
                " — change those under LoRa settings (or on the Meshtastic board)."
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(12), 0, 0)
        })
        // Scrollable: the fields and the note do not fit above the keyboard,
        // and without this Save sat hidden behind it (seen on the OnePlus).
        val scroller = android.widget.ScrollView(this).apply { addView(layout) }
        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle("Coverage model")
            .setView(scroller)
            .setPositiveButton("Save", null)
            .setNeutralButton("Defaults", null)
            .setNegativeButton("Cancel", null)
            .create()
        // ...and the window must shrink to the space above the keyboard for
        // the scrolling to reach Save; a dialog does not by default.
        @Suppress("DEPRECATION")
        dialog.window?.setSoftInputMode(
            android.view.WindowManager.LayoutParams.SOFT_INPUT_ADJUST_RESIZE)
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                fm.setText(CoverageSettings.DEFAULT_FM_MHZ)
                fmW.setText(CoverageSettings.DEFAULT_FM_W)
                height.setText(CoverageSettings.DEFAULT_TX_HEIGHT_M)
            }
            // Kept open on a bad value: Python says what is wrong, and the
            // operator fixes it without retyping the other two.
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val f = fm.text.toString().trim()
                val fw = fmW.text.toString().trim()
                val h = height.text.toString().trim()
                coverageExecutor.execute {
                    val problem = PythonRnsService.callModule("coverage", "check_settings",
                        CoverageSettings.params(this, f, fw, h).toString())
                    handler.post {
                        when {
                            problem == null -> Toast.makeText(this,
                                "Cannot check the settings — the Python bridge is not up yet.",
                                Toast.LENGTH_LONG).show()
                            problem.isNotEmpty() -> Toast.makeText(this, problem, Toast.LENGTH_LONG).show()
                            !CoverageSettings.save(this, f, fw, h) -> Toast.makeText(this,
                                "Could not save the settings.", Toast.LENGTH_LONG).show()
                            else -> {
                                dialog.dismiss()
                                updateCoveragePickerLabels()
                                Toast.makeText(this, "Coverage model saved", Toast.LENGTH_SHORT).show()
                                refreshCoverage()
                                refreshPeers()
                            }
                        }
                    }
                }
            }
        }
        dialog.show()
    }

    /** Radio key for the picker's current position, safe across reorderings
     *  by staying keyed to the label list rather than a hardcoded index. */
    private fun selectedCoverageRadio(): String = when (coveragePicker.selectedItemPosition) {
        0 -> "lora"
        else -> "fm"
    }

    /** A recompute was asked for while a run held the executor (a radio
     *  picked mid-run, say): done when that run ends, with the settings as
     *  they are THEN, instead of being dropped. */
    private var coverageRecomputeWanted = false

    /** Called where a run ends: the queued recompute if one was asked for,
     *  else whether the phone has moved on. */
    private fun afterCoverageRun() {
        if (coverageRecomputeWanted) {
            coverageRecomputeWanted = false
            refreshCoverage()
        } else {
            maybeFollow()
        }
    }

    private fun refreshCoverage() {
        if (coverageComputing) {
            coverageRecomputeWanted = true
            return
        }
        val tx = coverageTx
        val lat: Double
        val lon: Double
        if (tx != null) {
            lat = tx[0]
            lon = tx[1]
        } else {
            if (!hasLocationPermission()) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(
                        Manifest.permission.ACCESS_FINE_LOCATION,
                        Manifest.permission.ACCESS_COARSE_LOCATION
                    ),
                    REQ_LOCATION
                )
                coverageStatus.text = "Location permission is needed to compute coverage here."
                return
            }
            // The listener's fix when it is the newer one: lastKnownLocation can
            // lag a moving phone by however long since another app asked.
            val known = lastLocation()
            val live = coverageFix
            val location = if (live != null && (known == null || live.time >= known.time)) live else known
            if (location == null) {
                coverageStatus.text = "No location yet — enable location, or long-press " +
                    "the map to choose a point."
                return
            }
            lat = location.latitude
            lon = location.longitude
        }
        val radio = selectedCoverageRadio()
        if (tx == null) coverageComputedAt = doubleArrayOf(lat, lon)
        coverageComputing = true
        // This run reads the picker and settings as they are now, so it is
        // the recompute anybody queued.
        coverageRecomputeWanted = false
        coverageStatus.text = "Computing… (a new place downloads Overture buildings and " +
            "terrain first, then is kept offline)"
        val poll = startCoverageProgressPoll()
        coverageExecutor.execute {
            val json = PythonRnsService.callModule(
                "coverage", "compute", lat.toString(), lon.toString(), radio,
                coverageParams().toString()
            )
            handler.post {
                coverageComputing = false
                handler.removeCallbacks(poll)
                showCoverageResult(json, recenter = tx == null)
                // A compute in a new place saves an automatic area; outline it.
                refreshSavedAreas()
                // A radio picked while this ran, or the phone moved on.
                afterCoverageRun()
            }
        }
    }

    private fun showCoverageResult(json: String?, recenter: Boolean) {
        val obj = try {
            if (json != null) JSONObject(json) else null
        } catch (e: Exception) {
            null
        }
        when {
            obj == null -> coverageStatus.text =
                "Coverage unavailable — the Python bridge is not up yet."
            obj.has("error") -> coverageStatus.text =
                "Coverage failed: ${obj.optString("error")}"
            else -> {
                coverageMap.setModel(obj, recenter)
                val s = obj.optJSONObject("stats")
                val where = if (coverageTx == null) "from this phone (follows it as it moves)"
                            else "from the chosen point"
                val h = obj.optDouble("tx_height_m", 1.5)
                val heightNote = if (h != 1.5) String.format(Locale.US, ", antenna at %.1f m", h) else ""
                var text = String.format(
                    Locale.US, "%s%s %s — reaches %.2f–%.2f km. Buildings: %s.",
                    obj.optString("label"), heightNote, where,
                    s?.optDouble("min_km") ?: 0.0,
                    s?.optDouble("max_km") ?: 0.0,
                    obj.optString("data_source", "?")
                )
                // A downloaded area that holds the point but not the whole
                // 2 km around it leaves the far side looking like open
                // ground; say so rather than let the green shape lie quietly.
                if (!obj.optBoolean("data_complete", true)) {
                    text += " Point is near the edge of a downloaded area — range " +
                        "toward the edge is overstated."
                }
                coverageStatus.text = text
            }
        }
    }

    /** One pass of the peer loop: positions from the bridge, then each
     *  peer's footprint, drawn as each one lands so the first does not wait
     *  for the last. */
    private fun refreshPeers() {
        if (peerRefreshing || !coverageMap.showPeers) return
        peerRefreshing = true
        val radioKey = selectedCoverageRadio()
        val radioLabel = coveragePicker.selectedItem?.toString() ?: radioKey
        peerExecutor.execute {
            val params = coverageParams(forPeer = true).toString()
            // What a footprint was drawn WITH: a changed FM frequency or radio
            // section is a recompute, the same as picking another radio.
            val radio = "$radioKey $params"
            val list = try {
                PythonRnsService.call("peer_positions")?.let { JSONArray(it) }
            } catch (e: Exception) {
                null            // the bridge's {"error": ...} is not an array
            }
            if (list == null) {
                handler.post {
                    peerRefreshing = false
                    coveragePeerStatus.text = "Other nodes: unavailable — the bridge is not up yet."
                }
                return@execute
            }
            val ids = (0 until list.length()).map { list.getJSONObject(it).optString("node_id") }
            Log.d(TAG, "peer pass: ${ids.joinToString()}")
            peerStates.keys.retainAll(ids.toSet())
            fun footprints() = (0 until list.length()).mapNotNull { i ->
                val p = list.getJSONObject(i)
                val st = peerStates[p.optString("node_id")] ?: return@mapNotNull null
                CoverageMapView.PeerFootprint(
                    p.optString("node_id"), p.optString("name"),
                    p.optDouble("lat"), p.optDouble("lon"), p.optDouble("age_s"),
                    st.coverage, st.note, st.lat, st.lon)
            }
            val byId = (0 until list.length()).associate { i ->
                list.getJSONObject(i).let { it.optString("node_id") to it }
            }
            val needs = byId.filter { (id, p) ->
                val prev = peerStates[id]
                val moved = prev != null && FloatArray(1).also {
                    Location.distanceBetween(prev.lat, prev.lon, p.optDouble("lat"), p.optDouble("lon"), it)
                }[0] >= FOLLOW_RECOMPUTE_M
                prev == null || prev.coverage == null || prev.radio != radio || moved
            }.keys
            for (id in PeerQueue.order(ids, needs, peerWaitingSince,
                                       android.os.SystemClock.elapsedRealtime())) {
                val p = byId.getValue(id)
                val lat = p.optDouble("lat")
                val lon = p.optDouble("lon")
                val t0 = android.os.SystemClock.elapsedRealtime()
                val json = PythonRnsService.callModule(
                    "coverage", "compute_peer", lat.toString(), lon.toString(), radioKey, params)
                val obj = try { json?.let { JSONObject(it) } } catch (e: Exception) { null }
                // One line per footprint, so how long a peer takes -- a saved
                // area's sweep against a download -- is measurable on a phone.
                Log.i(TAG, "peer footprint $id in ${android.os.SystemClock.elapsedRealtime() - t0} ms: " +
                    (obj?.optString("error")?.takeIf { it.isNotEmpty() } ?: obj?.optString("data_source") ?: "no answer") +
                    (obj?.optJSONObject("timing")?.let { " $it" } ?: ""))
                peerStates[id] = when {
                    obj == null -> PeerState(lat, lon, radio, null, "coverage unavailable", 0.0, 0.0)
                    obj.optBoolean("pending") -> PeerState(lat, lon, radio, null,
                        "waiting to download map data", 0.0, 0.0)
                    obj.has("error") -> PeerState(lat, lon, radio, null,
                        "no coverage: ${obj.optString("error")}", 0.0, 0.0)
                    else -> {
                        val s = obj.optJSONObject("stats")
                        PeerState(lat, lon, radio, obj.optJSONArray("coverage"),
                            if (obj.optBoolean("data_complete", true)) null else "near the edge of its map data",
                            s?.optDouble("min_km") ?: 0.0, s?.optDouble("max_km") ?: 0.0)
                    }
                }
                val shown = footprints()
                handler.post { coverageMap.setPeers(shown) }
            }
            val shown = footprints()
            // Nodes heard on the mesh that have sent no position: on the
            // mesh but not drawable, which the map alone cannot tell apart
            // from a node that is not there.
            val waiting = try {
                PythonRnsService.call("peers_awaiting_position")?.let { JSONArray(it) }
            } catch (e: Exception) {
                null
            }?.let { a -> (0 until a.length()).map { a.getJSONObject(it).optString("name") } }
                ?: emptyList()
            val waitingText = if (waiting.isEmpty()) "" else
                " Waiting for a position from ${waiting.joinToString(", ")} — heard on " +
                    "the mesh but no location sent yet (its ATAK may not have a GPS fix)."
            val text = if (list.length() == 0) {
                if (waiting.isEmpty()) "No other nodes have reported a position in the last 15 min."
                else waitingText.trim()
            } else {
                val parts = (0 until list.length()).mapNotNull { i ->
                    val p = list.getJSONObject(i)
                    val st = peerStates[p.optString("node_id")] ?: return@mapNotNull null
                    val name = p.optString("name")
                    if (st.coverage != null) String.format(Locale.US, "%s %.2f–%.2f km", name, st.minKm, st.maxKm)
                    else "$name: ${st.note}"
                }
                "Other nodes, drawn for $radioLabel from where each last reported: " +
                    parts.joinToString("; ") + "." + waitingText
            }
            handler.post {
                peerRefreshing = false
                coverageMap.setPeers(shown)
                coveragePeerStatus.text = text
            }
        }
    }

    private fun toggleSelectMode() {
        val on = !coverageMap.selectMode
        coverageMap.selectMode = on
        coverageDownloadButton.text = if (on) "Cancel download" else "Download an area for offline use"
        if (on) {
            coverageStatus.text = "Drag a box on the map around the area you want to keep " +
                "offline. Pinch still zooms."
        } else {
            coverageMap.clearSelection()
        }
    }

    /** The box is drawn: ask Python how many tiles it needs, refuse it if
     *  it is too big (before any data is spent), else ask to download. */
    private fun onCoverageSelection(sel: DoubleArray?) {
        if (sel == null) return
        coverageExecutor.execute {
            val json = PythonRnsService.callModule(
                "coverage", "area_tile_count",
                sel[0].toString(), sel[1].toString(), sel[2].toString(), sel[3].toString()
            )
            handler.post {
                val obj = try { json?.let { JSONObject(it) } } catch (e: Exception) { null }
                if (obj == null || obj.has("error")) {
                    coverageStatus.text = "Cannot size that area: ${obj?.optString("error") ?: "bridge not up"}"
                    return@post
                }
                val tiles = obj.optInt("tiles")
                val limit = obj.optInt("limit")
                val hKm = (sel[2] - sel[0]) * 110.54
                val wKm = (sel[3] - sel[1]) * 111.32 * Math.cos(Math.toRadians(sel[0]))
                if (tiles > limit) {
                    // Stay in select mode: the next drag replaces the box.
                    coverageStatus.text = String.format(Locale.US,
                        "%.1f × %.1f km needs %d map tiles; the limit is %d. Drag a smaller box.",
                        wKm, hKm, tiles, limit)
                    return@post
                }
                // Measured on the OnePlus 2026-10-20: 0.17-0.63 MB a tile
                // (2-7.5 MB for 12), denser streets at the top end.
                MaterialAlertDialogBuilder(this)
                    .setTitle("Download this area?")
                    .setMessage(String.format(Locale.US,
                        "%.1f × %.1f km, plus 2 km around it so a point anywhere inside has " +
                            "data all round: %d map tiles, about %.0f–%.0f MB.\n\n" +
                            "Kept on the phone for coverage with no signal. It replaces the " +
                            "area you downloaded before, if any.",
                        wKm, hKm, tiles, Math.max(1.0, tiles * 0.2), Math.max(1.0, tiles * 0.65)))
                    .setPositiveButton("Download") { _, _ -> downloadSelectedArea() }
                    .setNegativeButton("Cancel") { _, _ ->
                        if (coverageMap.selectMode) toggleSelectMode()
                    }
                    .setOnCancelListener { if (coverageMap.selectMode) toggleSelectMode() }
                    .show()
            }
        }
    }

    private fun downloadSelectedArea() {
        val sel = coverageMap.currentSelection() ?: return
        if (coverageComputing) return
        coverageComputing = true
        coverageDownloadButton.isEnabled = false
        coverageStatus.text = "Downloading Overture buildings and streets…"
        val poll = startCoverageProgressPoll()
        coverageExecutor.execute {
            val json = PythonRnsService.callModule(
                "coverage", "download_area",
                sel[0].toString(), sel[1].toString(), sel[2].toString(), sel[3].toString()
            )
            handler.post {
                coverageComputing = false
                coverageDownloadButton.isEnabled = true
                handler.removeCallbacks(poll)
                if (coverageMap.selectMode) toggleSelectMode()
                val obj = try { json?.let { JSONObject(it) } } catch (e: Exception) { null }
                if (obj == null || obj.has("error")) {
                    coverageStatus.text = "Download failed: ${obj?.optString("error") ?: "bridge not up"}"
                    return@post
                }
                Toast.makeText(this, String.format(Locale.US,
                    "Saved %d buildings, %d streets (%d KB, Overture %s)",
                    obj.optInt("buildings"), obj.optInt("roads"), obj.optInt("size_kb"),
                    obj.optString("release")), Toast.LENGTH_LONG).show()
                refreshSavedAreas()
                // Compute at the middle of the new area straight away.
                coverageTx = doubleArrayOf((sel[0] + sel[2]) / 2, (sel[1] + sel[3]) / 2)
                refreshCoverage()
            }
        }
    }

    /** Long-press: compute from there -- and, inside an area the operator
     *  downloaded, offer to delete it instead (the replacement for the old
     *  Saved areas list). The app's own automatic and other-node areas are
     *  replaced without asking, and one of them nearly always surrounds the
     *  phone, so they do not interrupt a long-press. */
    private fun onCoverageLongPress(lat: Double, lon: Double) {
        val here = coverageMap.areasAt(lat, lon)
        Log.d(TAG, "coverage long-press $lat,$lon in areas: " +
            here.joinToString { "${it.kind}:${it.file}" })
        val mine = here.filter { it.kind == "overture" || it.kind == "bundle" }
        if (mine.isEmpty()) {
            coverageTx = doubleArrayOf(lat, lon)
            refreshCoverage()
            return
        }
        val items = arrayOf("Compute coverage from here") + mine.map {
            String.format(Locale.US, "Delete this downloaded area (%d buildings)", it.buildings)
        }
        MaterialAlertDialogBuilder(this)
            .setItems(items) { _, i ->
                if (i == 0) {
                    coverageTx = doubleArrayOf(lat, lon)
                    refreshCoverage()
                } else {
                    confirmDeleteArea(mine[i - 1])
                }
            }
            .show()
    }

    private fun confirmDeleteArea(area: CoverageMapView.SavedArea) {
        MaterialAlertDialogBuilder(this)
            .setMessage(String.format(Locale.US,
                "Delete this downloaded area (%d buildings)? It can be downloaded again.",
                area.buildings))
            .setPositiveButton("Delete") { _, _ ->
                coverageExecutor.execute {
                    PythonRnsService.callModule("coverage", "delete_area", area.file)
                    handler.post { refreshSavedAreas() }
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Show coverage.download_progress() in the status line while a run is
     *  in flight -- an area download, or a compute that is fetching Overture
     *  for a place with no saved area. Stops itself when the run ends; the
     *  caller also removes it. Polled on basemapExecutor because the run
     *  itself occupies coverageExecutor. */
    private fun startCoverageProgressPoll(): Runnable {
        val poll = object : Runnable {
            override fun run() {
                if (!coverageComputing) return
                basemapExecutor.execute {
                    val p = PythonRnsService.callModule("coverage", "download_progress")
                    handler.post {
                        val o = try { p?.let { JSONObject(it) } } catch (e: Exception) { null }
                        if (coverageComputing && o != null && o.optBoolean("active")) {
                            val total = o.optInt("total")
                            coverageStatus.text = if (total > 0)
                                "${o.optString("stage")}… ${o.optInt("done")} of $total"
                            else "${o.optString("stage")}…"
                        }
                    }
                }
                handler.postDelayed(this, 700)
            }
        }
        handler.postDelayed(poll, 700)
        return poll
    }

    private fun refreshSavedAreas() {
        coverageExecutor.execute {
            val json = PythonRnsService.callModule("coverage", "list_areas")
            handler.post {
                val arr = try { json?.let { JSONArray(it) } } catch (e: Exception) { null }
                coverageMap.setAreas(arr)
            }
        }
    }

    /** Streets for orienting the map, fetched for the visible area plus half
     *  a screen around it, and only when the view leaves what was fetched. */
    private fun maybeFetchBasemap(s: Double, w: Double, n: Double, e: Double) {
        val have = basemapBox
        if (have != null && s >= have[0] && w >= have[1] && n <= have[2] && e <= have[3]) return
        if (basemapFetching) return
        val dLat = (n - s) / 2
        val dLon = (e - w) / 2
        val want = doubleArrayOf(s - dLat, w - dLon, n + dLat, e + dLon)
        basemapFetching = true
        basemapExecutor.execute {
            val t0 = android.os.SystemClock.elapsedRealtime()
            val json = PythonRnsService.callModule(
                "coverage", "basemap",
                want[0].toString(), want[1].toString(), want[2].toString(), want[3].toString()
            )
            Log.d(TAG, "basemap in ${android.os.SystemClock.elapsedRealtime() - t0} ms")
            handler.post {
                basemapFetching = false
                val obj = try { json?.let { JSONObject(it) } } catch (e: Exception) { null }
                val roads = obj?.optJSONArray("roads") ?: return@post
                basemapBox = want
                coverageMap.setBasemap(roads, obj.optJSONArray("labels"))
            }
        }
    }

    /**
     * DEBUG BUILDS ONLY: feed a fake fix into the Coverage tab's follower, to
     * test following without a system mock-location provider -- which would
     * move EVERY app's position, ATAK's included, and so broadcast a false
     * position over the mesh. This touches only this tab.
     *
     *   adb shell am broadcast -a io.github.stress5094.customrnsbridge.COVERAGE_FIX \
     *       --es lat 20.9575 --es lon -140.6383
     *
     * And COVERAGE_PEER records a fake PEER position in the bridge's
     * peer-position book (only that book: nothing is sent or shown to ATAK),
     * to test the other-nodes footprints without a second phone:
     *
     *   adb shell am broadcast -a io.github.stress5094.customrnsbridge.COVERAGE_PEER \
     *       --es node test01 --es lat 20.9175 --es lon -140.5733
     *
     * And SETTINGS_SCAN hands the settings-sharing screens a bundle as if the
     * camera had scanned it, to test them with one phone:
     *
     *   adb shell am broadcast -a io.github.stress5094.customrnsbridge.SETTINGS_SCAN \
     *       --es text RTAKS1....
     */
    private var coverageFixHook: android.content.BroadcastReceiver? = null

    private fun registerCoverageFixHook() {
        if (applicationInfo.flags and android.content.pm.ApplicationInfo.FLAG_DEBUGGABLE == 0) return
        if (coverageFixHook != null) return
        val r = object : android.content.BroadcastReceiver() {
            override fun onReceive(c: Context, intent: Intent) {
                if (intent.action == "$packageName.SETTINGS_SCAN") {
                    // As if the camera had read it (settings sharing, tested
                    // with one phone): --es text RTAKS1....
                    intent.getStringExtra("text")?.let { shareUi.onScanned(it) }
                    return
                }
                val lat = intent.getStringExtra("lat")?.toDoubleOrNull() ?: return
                val lon = intent.getStringExtra("lon")?.toDoubleOrNull() ?: return
                if (intent.action == "$packageName.COVERAGE_PEER") {
                    val node = intent.getStringExtra("node") ?: return
                    Log.i(TAG, "coverage test peer $node at $lat,$lon")
                    peerExecutor.execute {
                        PythonRnsService.call("_note_peer_position", node, lat.toString(), lon.toString())
                        handler.post { refreshPeers() }
                    }
                    return
                }
                Log.i(TAG, "coverage test fix $lat,$lon")
                coverageLocationListener.onLocationChanged(Location("coverage-test").apply {
                    latitude = lat
                    longitude = lon
                    accuracy = 5f
                    time = System.currentTimeMillis()
                })
            }
        }
        val filter = android.content.IntentFilter("$packageName.COVERAGE_FIX").apply {
            addAction("$packageName.COVERAGE_PEER")
            addAction("$packageName.SETTINGS_SCAN")
        }
        if (Build.VERSION.SDK_INT >= 33) {
            registerReceiver(r, filter, Context.RECEIVER_EXPORTED)
        } else {
            registerReceiver(r, filter)
        }
        coverageFixHook = r
    }

    private fun startFollowingLocation() {
        if (followingLocation || !hasLocationPermission()) return
        val lm = getSystemService(LOCATION_SERVICE) as? LocationManager ?: return
        for (provider in listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER)) {
            try {
                lm.requestLocationUpdates(provider, FOLLOW_MIN_TIME_MS, FOLLOW_MIN_DISTANCE_M,
                    coverageLocationListener, Looper.getMainLooper())
                followingLocation = true
            } catch (e: SecurityException) {
                // Permission revoked between the check and the call.
            } catch (e: IllegalArgumentException) {
                // This device has no such provider.
            }
        }
    }

    private fun stopFollowingLocation() {
        if (!followingLocation) return
        (getSystemService(LOCATION_SERVICE) as? LocationManager)
            ?.removeUpdates(coverageLocationListener)
        followingLocation = false
    }

    /** Recompute at the live fix if following and it has moved far enough
     *  from the last computed point. A run in progress is left to finish;
     *  its completion calls this again. */
    private fun maybeFollow() {
        if (coverageTx != null || coverageComputing || currentTab != 3) return
        val fix = coverageFix ?: return
        val at = coverageComputedAt
        if (at != null) {
            val d = FloatArray(1)
            Location.distanceBetween(at[0], at[1], fix.latitude, fix.longitude, d)
            if (d[0] < FOLLOW_RECOMPUTE_M) return
        }
        refreshCoverage()
    }

    private fun hasLocationPermission(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_COARSE_LOCATION) ==
            PackageManager.PERMISSION_GRANTED

    /** The freshest cached fix across the providers, or null. A planning tool
     *  tolerates a slightly stale fix better than blocking on a live GPS lock;
     *  ATAK usually keeps one warm on the same phone anyway. */
    private fun lastLocation(): Location? {
        val lm = getSystemService(LOCATION_SERVICE) as? LocationManager ?: return null
        var best: Location? = null
        for (provider in listOf(
            LocationManager.GPS_PROVIDER,
            LocationManager.NETWORK_PROVIDER,
            LocationManager.PASSIVE_PROVIDER
        )) {
            val loc = try {
                lm.getLastKnownLocation(provider)
            } catch (e: SecurityException) {
                null
            } ?: continue
            if (best == null || loc.time > best.time) best = loc
        }
        return best
    }

    private fun fetchMeshView(): JSONObject? = try {
        callPython("get_mesh_view")?.let { JSONObject(it) }
    } catch (e: Exception) {
        Log.w(TAG, "get_mesh_view failed", e)
        null
    }

    private fun renderMesh(view: JSONObject) {
        if (!view.optBoolean("ok", false)) {
            meshNote.text = "Could not build the picture: ${view.optString("error")}"
            meshNote.setTextColor(COLOR_DOWN)
            return
        }
        lastMeshView = view
        meshDiagram.setModel(view)
        renderMeshNote(view)
        renderMeshList(view)
    }

    /** One line saying what the picture is, and what it cannot say. */
    private fun renderMeshNote(view: JSONObject) {
        meshNote.setTextColor(COLOR_IDLE)
        val n = view.optJSONArray("nodes")?.length() ?: 0
        if (n == 0) {
            meshNote.text = "No other nodes heard yet."
            return
        }
        meshNote.text = "Inner ring: heard directly. Outer ring: only reached through " +
            "others. Probable links are guesses from what other nodes say they hear, LoRa only."
    }

    private fun renderMeshList(view: JSONObject) {
        meshListBox.removeAllViews()
        val nodes = view.optJSONArray("nodes") ?: return
        val lines = view.optJSONArray("lines")
        val selected = meshDiagram.selected
        for (i in 0 until nodes.length()) {
            val node = nodes.getJSONObject(i)
            val id = node.optString("node_id")
            val grade = node.optString("grade")
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(8), dp(6), dp(8), dp(6))
                if (id == selected) background = rounded(getColor(R.color.status_highlight), 6)
                setOnClickListener {
                    meshDiagram.selected = if (meshDiagram.selected == id) null else id
                    renderMeshList(view)
                }
            }
            val callsign = node.optString("callsign")
            row.addView(TextView(this).apply {
                text = node.optString("label") +
                    (if (callsign.isNotEmpty()) "  [$callsign]" else "") +
                    "  —  " + gradeWord(grade)
                textSize = 15f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(meshGradeInk(grade))
            })
            if (lines != null) for (j in 0 until lines.length()) {
                val l = lines.getJSONObject(j)
                if (l.optString("node_id") != id) continue
                row.addView(TextView(this).apply {
                    text = meshLineText(l)
                    textSize = 13f
                    setTextColor(meshGradeInk(l.optString("grade")))
                    setPadding(dp(12), dp(2), 0, 0)
                })
            }
            meshListBox.addView(row)
        }
    }

    /** "FM, direct — heard 12 s ago (FM: …) · 2 of 30 lost". The reason text is
     *  mesh_view's; this adds the radio's name and the counts. */
    private fun meshLineText(l: JSONObject): String {
        val name = when (l.optString("radio")) {
            "rnode" -> "RNode"
            "meshtastic" -> "Meshtastic"
            "fm" -> "FM"
            "wifi" -> "WiFi"
            else -> l.optString("radio")
        }
        val how = if (l.optBoolean("direct")) "direct" else "relayed"
        val counts = when {
            !l.optBoolean("direct") -> ""
            l.isNull("lost") -> " · loss unknown (heard on more than one radio)"
            else -> {
                val lost = l.optInt("lost")
                " · $lost of ${lost + l.optInt("heard")} lost"
            }
        }
        return "$name, $how — ${l.optString("reason")}$counts"
    }

    private fun gradeWord(grade: String) = when (grade) {
        "good" -> "good"
        "weak" -> "weak"
        else -> "gone"
    }

    private fun meshGradeInk(grade: String) = when (grade) {
        "good" -> COLOR_UP
        "weak" -> COLOR_WARN
        else -> COLOR_IDLE
    }

    /**
     * The network key, and the button for changing it.
     *
     * This sits directly under the local node's status rather than behind a
     * settings menu because it is the one setting that silently decides
     * whether anything works at all: two radios with different passphrases are
     * indistinguishable from two radios out of range, and the fingerprint is
     * what tells those apart at a glance.
     */
    private fun buildKeySection(): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(10), 0, 0)
        }
        keyLine = TextView(this).apply {
            text = "Network key …"
            textSize = 13f
            setTextColor(COLOR_IDLE)
            setPadding(dp(24), 0, 0, 0)
            layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
        }
        box.addView(keyLine)
        // The buttons that used to sit here moved to buildSettingsControls();
        // three ragged rows of different widths, with Range test orphaned on
        // its own, made the one you need while standing outside the hardest
        // to hit. They are one even grid now.
        return box
    }

    /**
     * Status tab: settings, log control and the restart -- everything you
     * reach for while the node is doing its job, as opposed to the bench
     * tools on the Debug tab that exist to set up or troubleshoot the
     * hardware underneath it.
     *
     * Even columns rather than right-aligned rows because a full-width tap
     * target is the difference between working and not while walking, and
     * because a ragged edge gives no clue which buttons belong together.
     */
    private fun buildSettingsControls(): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(10), 0, 0)
        }

        // The gap only between the two, so the row's outer edges line up with
        // the full-width buttons above and below.
        fun cell(button: Button, start: Int, end: Int) = button.apply {
            layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
                .apply { marginStart = start; marginEnd = end }
        }

        fun row(left: Button, right: Button) = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(cell(left, 0, dp(4)))
            addView(cell(right, dp(4), 0))
        }

        box.addView(row(
            Ui.tonal(this, "Passphrase").apply {
                setOnClickListener { showPassphraseDialog() }
            },
            Ui.tonal(this, "LoRa settings").apply {
                setOnClickListener { showRadioDialog() }
            }))

        // Full width and labelled with the answer, not the question: which
        // wire the radio is on is something you check at a glance before
        // wondering why nothing is arriving. It sits with the settings rather
        // than the measurements, and unlike "Radio" it is built entirely from
        // Android's own state -- so it still opens when the bridge is dead,
        // which is exactly when it is needed.
        linkButton = Ui.outlined(this, "Radio link").apply {
            setOnClickListener { showRadioLinkDialog() }
        }
        box.addView(linkButton)
        renderLink()
        // Settings by QR code, the in-person case, both ways: show this
        // phone's, or scan another's. Everything else is on the Share tab.
        box.addView(row(Ui.outlined(this, "Share QR").apply {
            setOnClickListener { shareUi.showQrShare() }
        }, Ui.outlined(this, "Scan QR").apply {
            setOnClickListener { shareUi.scanQr() }
        }))

        // Its own row, and last. It takes the radio and ATAK down for a few
        // seconds, so it should not sit where a thumb lands by accident.
        box.addView(Ui.danger(this, "Restart bridge").apply {
            setOnClickListener { confirmRestart() }
        }.apply { (layoutParams as? LinearLayout.LayoutParams)?.topMargin = dp(10) })

        // Below Restart, for the same reason Restart is last: it ends the whole
        // run, so it is behind a confirmation and away from the thumb.
        box.addView(Ui.danger(this, "Quit app").apply {
            setOnClickListener { confirmQuit() }
        })
        return box
    }

    /**
     * Debug tab (was "Radio test"): every bench-only button that exists to set up or
     * troubleshoot the hardware, not to run the mesh -- grouped by what
     * each group actually verifies, with a one-line caption per group
     * rather than a single undifferentiated column of eleven buttons.
     * Section labels reuse the heading style "Nodes"/"Messages"
     * already use on the Status tab instead of inventing a new one.
     */
    private fun buildRadioTestControls(): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(16), dp(20), dp(20))
        }

        // The gap only between the two, so the row's outer edges line up with
        // the full-width buttons above and below.
        fun cell(button: Button, start: Int, end: Int) = button.apply {
            layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
                .apply { marginStart = start; marginEnd = end }
        }

        fun row(left: Button, right: Button) = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            addView(cell(left, 0, dp(4)))
            addView(cell(right, dp(4), 0))
        }

        fun full(button: Button) = button.apply { textSize = 12f }

        fun sectionLabel(text: String) = TextView(this).apply {
            this.text = text
            Ui.heading(this)
            setPadding(0, dp(18), 0, dp(2))
        }

        fun sectionCaption(text: String) = TextView(this).apply {
            this.text = text
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, 0, 0, dp(8))
        }

        box.addView(TextView(this).apply {
            text = "The log, and bench diagnostics for setting up and troubleshooting " +
                    "hardware -- not needed for normal field use."
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, 0, 0, dp(4))
        })

        // Advanced features (operator, 2026-11-10): one switch that hides the
        // settings a normal user never needs -- the link force buttons below,
        // and in Radio link the WiFi mesh, custom FM, the no-PTT cable and
        // receive-only test mode. It only hides them; values already set stay.
        box.addView(sectionLabel("Advanced features"))
        box.addView(sectionCaption("Shows link forcing, status detail and the Share tab here, " +
            "and the WiFi mesh and FM tone settings in Radio link"))
        box.addView(com.google.android.material.materialswitch.MaterialSwitch(this).apply {
            text = "Show advanced features"
            isChecked = RadioLink.advancedEnabled(this@MainActivity)
            setOnCheckedChangeListener { _, on ->
                RadioLink.setAdvancedEnabled(this@MainActivity, on)
                advancedBox.visibility = if (on) View.VISIBLE else View.GONE
            }
        })
        advancedBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = if (RadioLink.advancedEnabled(this@MainActivity)) View.VISIBLE else View.GONE
        }
        advancedBox.addView(com.google.android.material.materialswitch.MaterialSwitch(this).apply {
            text = "Show detailed status on the Status tab"
            isChecked = RadioLink.statusDetails(this@MainActivity)
            setOnCheckedChangeListener { _, on ->
                RadioLink.setStatusDetails(this@MainActivity, on)
            }
        })
        advancedBox.addView(com.google.android.material.materialswitch.MaterialSwitch(this).apply {
            text = "Show the Share tab"
            isChecked = RadioLink.shareTabEnabled(this@MainActivity)
            setOnCheckedChangeListener { _, on ->
                RadioLink.setShareTabEnabled(this@MainActivity, on)
                nav.menu.findItem(4).isVisible = on
                if (!on && currentTab == 4) showTab(0)
            }
        })
        advancedBox.addView(TextView(this).apply {
            text = "Link"
            textSize = 13f
            setPadding(0, dp(8), 0, dp(2))
        })
        val overrideRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        fun applyOverride(value: String) {
            RadioLink.setLinkOverride(this@MainActivity, value)
            executor.execute { callPython("set_link_override", value) }
        }
        // "Auto" applies at once: it is the recovery choice. Forcing can
        // strand a node on a dead link (the 2026-09-26 field test's 7-minute
        // stall), so it takes a second, explicit tap on a dialog.
        for ((label, value) in listOf("Auto" to "auto", "Force LoRa" to "lora",
                                      "Force FM" to "fm", "Force WiFi" to "wifi")) {
            val b = Ui.outlined(this, label).apply {
                textSize = 12f
                minWidth = 0
                minimumWidth = 0
                setPadding(dp(4), paddingTop, dp(4), paddingBottom)
                layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
                    .apply { marginEnd = dp(4) }
                setOnClickListener {
                    if (value == "auto") {
                        applyOverride(value)
                    } else {
                        val name = linkName(value)
                        MaterialAlertDialogBuilder(this@MainActivity)
                            .setTitle("Force $name?")
                            .setMessage("Automatic failover will be suspended until you " +
                                "tap Auto again, even if $name stops working.")
                            .setPositiveButton(label) { _, _ -> applyOverride(value) }
                            .setNegativeButton("Cancel", null)
                            .show()
                    }
                }
            }
            overrideButtons.add(Triple(label, value, b))
            overrideRow.addView(b)
        }
        advancedBox.addView(overrideRow)
        overrideNote = TextView(this).apply {
            textSize = 12f
            setTextColor(COLOR_IDLE)
        }
        advancedBox.addView(overrideNote)
        box.addView(advancedBox)

        // The log moved here from Status (operator, 2026-10-22): it is for
        // troubleshooting, and it records from start-up whatever this says.
        box.addView(sectionLabel("Log"))
        box.addView(sectionCaption("Everything the bridge reports, to a file you can export"))
        logToggle = Ui.outlined(this, "Start log").apply {
            setOnClickListener {
                if (LogRecorder.isRecording()) {
                    LogRecorder.stop()
                    Toast.makeText(this@MainActivity, "Log stopped", Toast.LENGTH_SHORT).show()
                } else {
                    // Starts a fresh file rather than appending, so "start log"
                    // means a run that begins here.
                    LogRecorder.start(applicationContext)
                    Toast.makeText(this@MainActivity, "Recording a new log", Toast.LENGTH_SHORT)
                        .show()
                }
                renderLog()
            }
        }

        box.addView(row(logToggle, Ui.outlined(this, "Export").apply {
            setOnClickListener { exportLog() }
        }))
        logLine = TextView(this).apply {
            text = "Log …"
            textSize = 13f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(6), 0, 0)
        }
        box.addView(logLine)

        box.addView(sectionLabel("Link diagnostics"))
        box.addView(sectionCaption("Coverage and distance checks over the live mesh"))
        surveyButton = Ui.tonal(this, "Survey").apply {
            setOnClickListener { toggleSurvey() }
        }
        rangeButton = Ui.tonal(this, "Range test").apply {
            setOnClickListener { toggleRangeTest() }
        }
        box.addView(row(surveyButton, rangeButton))
        // Same idea as the log's Export: the CSV is app-private, and the
        // walking phone -- the one whose own GPS makes its distances the
        // trustworthy ones -- ends the walk out of adb range.
        box.addView(full(Ui.outlined(this, "Export range test").apply {
            setOnClickListener { exportRangeTest() }
        }))
        rangeLine = TextView(this).apply {
            textSize = 13f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(4), 0, 0)
            visibility = View.GONE
        }
        box.addView(rangeLine)
        surveyLine = TextView(this).apply {
            textSize = 13f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(4), 0, 0)
            visibility = View.GONE
        }
        box.addView(surveyLine)

        // THE FM BENCH BUTTONS LIVED HERE until 2026-10-01: a codec2
        // self-test, FM TX/RX tests, an FM chat screen, and a "beacon burst"
        // group that reused the production code/symbol-rate/lead-in with a
        // known beacon-sized payload. NOT restored with the FM transport
        // itself, and the one worth restoring is the last: the cheap TX/RX
        // buttons used a much smaller, faster code than the live beacon loop,
        // so a pass there said nothing about whether the beacon's ~5x-longer
        // burst survived the same channel. A bench button must send the SHAPE
        // the link actually sends -- which now means asking fm_link for the
        // live operating point, the way FmAudioBridgeService does, rather than
        // carrying numbers of its own. See docs/fm-lessons.md.
        //
        // The test tone survives: it is a plain Kotlin sine generator with no
        // dependency on the FM stack, and it is how a VOX/threshold cable's
        // dial gets set against a steady signal instead of being judged off
        // single short bursts.
        box.addView(sectionLabel("Audio bench"))
        box.addView(sectionCaption(
            "Verify the cable's audio path, no mesh traffic"))
        box.addView(full(Ui.outlined(this, "Test tone (10 s)").apply {
            setOnClickListener { runTestTone() }
        }))

        return box
    }

    private fun linkName(link: String) = when (link) {
        "wifi" -> "WiFi"
        "fm" -> "FM"
        "auto" -> "Auto"
        else -> "LoRa"
    }

    /** The Debug tab's force buttons: "*" marks the one chosen. They do
     * nothing useful until two links are built, so they say so then. */
    private fun renderOverrideButtons(override: String, twoLinks: Boolean) {
        if (overrideButtons.isEmpty()) return
        for ((label, value, b) in overrideButtons) {
            b.text = if (override == value) "* $label" else label
            b.isEnabled = twoLinks
        }
        overrideNote.text = if (twoLinks) "* marks the current choice. Auto lets " +
            "the app fail over and back by itself."
            else "Only one link is set up, so there is nothing to force."
    }

    private fun renderLink() {
        linkButton.text = "Radio link: " + RadioLink.label(this) +
                if (RadioLink.pendingRestart(this)) "  (restart to apply)" else ""
    }

    /**
     * The permissions are asked for here rather than in the bridge service
     * because only an Activity can ask. Denying them is survivable -- the
     * dialog still opens, and USB still works -- so a refusal must not become
     * a dead end.
     */
    private fun showRadioLinkDialog() {
        if (!RadioLinkDialog.hasPermissions(this)) {
            requestBluetoothPermissions(REQ_BLUETOOTH)
            return
        }
        openRadioLinkDialog()
    }

    private fun openRadioLinkDialog() {
        RadioLinkDialog(this) { changed ->
            renderLink()
            // THE FM LINK CANNOT HEAR ANYTHING WITHOUT RECORD_AUDIO, and only
            // an Activity can ask -- FmAudioBridgeService logs the refusal
            // explicitly because otherwise it presents as "No audio" on the
            // status screen, which is identical to an unplugged cable and is
            // where anyone would look first. Asked the moment FM is selected
            // rather than at the first burst, so the dialog lands while the
            // operator is still in the settings they just changed.
            if (RadioLink.fmInUse(this) && !hasRecordAudio()) {
                restartAfterRecordAudio = changed
                ActivityCompat.requestPermissions(
                    this, arrayOf(Manifest.permission.RECORD_AUDIO),
                    REQ_RECORD_AUDIO_FM)
                return@RadioLinkDialog
            }
            if (changed) confirmRestart()
        }.show()
    }

    private fun hasRecordAudio() =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    // Whether the radio-link change that triggered the microphone request also
    // needs a restart. Held across the permission round trip rather than
    // restarting first, so the operator answers one dialog at a time -- and so
    // a grant is in place before the process comes back up and the FM service
    // opens its sound card.
    private var restartAfterRecordAudio = false

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_RECORD_AUDIO_FM) {
            val granted = grantResults.isNotEmpty() &&
                grantResults.all { it == PackageManager.PERMISSION_GRANTED }
            if (!granted) {
                // SURVIVABLE, and said rather than enforced: the operator may
                // have selected FM as a backup on a node whose LoRa link is
                // fine, and refusing to save the setting over a permission
                // would be worse than a link that reports it cannot hear.
                Toast.makeText(
                    this,
                    "Without microphone permission the FM link can transmit " +
                        "but will never hear anything",
                    Toast.LENGTH_LONG
                ).show()
            }
            if (restartAfterRecordAudio) {
                restartAfterRecordAudio = false
                confirmRestart()
            }
            return
        }
        if (requestCode == REQ_LOCATION) {
            if (grantResults.isNotEmpty() &&
                grantResults.any { it == PackageManager.PERMISSION_GRANTED }
            ) {
                // Now that a provider is available, run the deferred compute.
                refreshCoverage()
            } else {
                coverageStatus.text =
                    "Location permission denied — coverage cannot be computed."
            }
            return
        }
        if (requestCode != REQ_BLUETOOTH) return
        if (grantResults.isEmpty() ||
            grantResults.any { it != PackageManager.PERMISSION_GRANTED }
        ) {
            Toast.makeText(
                this,
                "Without Bluetooth permission this app can only reach the radio by cable",
                Toast.LENGTH_LONG
            ).show()
        }
        openRadioLinkDialog()
    }

    /**
     * Stop everything and come back up, equivalent to force-stopping from
     * Android settings and tapping the icon again.
     *
     * Earns its place because of a real failure: on 2026-08-16 a node's
     * receiver hung -- still transmitting, hearing nothing, no error logged,
     * interface still reporting online -- and the only thing that cleared it
     * was restarting the process. Without this button that means finding the
     * app in Android settings, which is not a thing anyone will do correctly
     * while a bench is running.
     */
    private fun confirmRestart() {
        MaterialAlertDialogBuilder(this)
            .setTitle("Restart bridge")
            .setMessage(
                "Stops the radio, ATAK connection and mesh, then starts again. " +
                        "Takes a few seconds, and any message waiting to be " +
                        "acknowledged is lost."
            )
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Restart") { _, _ -> restartApp() }
            .show()
    }

    /**
     * Quit: stop everything and end the process, so the app does not have to be
     * force-closed from Android settings after a bench session.
     */
    private fun confirmQuit() {
        MaterialAlertDialogBuilder(this)
            .setTitle("Quit app")
            .setMessage(
                "Stops the radio link, the ATAK connection and the mesh, and ends " +
                        "the app completely. Any message waiting to be acknowledged " +
                        "is lost. Open the app again to start it."
            )
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Quit") { _, _ -> quitApp() }
            .show()
    }

    private fun quitApp() {
        Log.i(TAG, "quit requested from UI, pid ${android.os.Process.myPid()} exiting")
        // Every service is stopped explicitly, and for the reason restartApp()
        // gives: none of them overrides onStartCommand, so all are START_STICKY
        // and Android would resurrect them after the process dies -- the app
        // would "quit" and then reappear in the notification shade, holding the
        // USB port again.
        LogRecorder.stop()
        RadioLink.stopBridgeServices(this)
        stopService(Intent(this, CotTakServerBridgeService::class.java))

        // THEN WAIT before killing the process, and the wait is the point. The
        // stops above are queued onto this thread; each service's onDestroy is
        // what drops CM108 PTT (FmAudioBridgeService.closeLink() unkeys first).
        // Killing the process immediately can skip that, and a radio left keyed
        // does not just lose a message -- it holds the channel against every
        // node until the cable is unplugged. This runs after those onDestroy
        // calls because it is queued behind them on the same thread.
        finishAndRemoveTask()
        handler.postDelayed({
            android.os.Process.killProcess(android.os.Process.myPid())
        }, QUIT_GRACE_MS)
    }

    private fun restartApp() {
        // Logged before LogRecorder itself stops, so the outgoing file's last
        // line says why it ends rather than just going silent -- the new
        // process's own log (started automatically in onCreate()) names this
        // file in turn, so the two can be read as one continuous run.
        Log.i(TAG, "bridge restart requested from UI, pid " +
                "${android.os.Process.myPid()} exiting -- log continues in next run")
        // Stopped explicitly rather than left to die with the process. Neither
        // service overrides onStartCommand, so both are START_STICKY and
        // Android would resurrect them into the process we are trying to
        // replace -- with the USB port still held by the old instance, which
        // is exactly the state this button exists to escape.
        LogRecorder.stop()
        RadioLink.stopBridgeServices(this)
        stopService(Intent(this, CotTakServerBridgeService::class.java))

        // Handed to a trampoline in another process, which kills this one and
        // then launches the app fresh. Doing it here cannot work: whatever
        // starts the new activity has to outlive the death of this process.
        // An AlarmManager PendingIntent was tried first and quit without ever
        // coming back -- an alarm firing into a dead app is a background
        // activity start, blocked since Android 10. See RestartActivity.
        startActivity(
            Intent(this, RestartActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra(RestartActivity.EXTRA_PID, android.os.Process.myPid())
        )
    }

    /**
     * Preset, frequency and power.
     *
     * Next to the passphrase because the two failures are the same failure:
     * get either wrong on one device and it goes silent, looking exactly like
     * a dead radio or a peer out of range. The status screen prints both
     * settings on every node so they can be read side by side, which is the
     * only quick way to tell "misconfigured" from "broken".
     */
    private fun showRadioDialog() {
        // A Meshtastic board has its own settings (region, preset, channel and
        // key...), none of which the RNode screen below can change.
        if (RadioLink.isMeshtastic(this)) {
            MeshBoardDialog(
                this,
                { method, args -> callPython(method, *args) },
                onEditAppRadio = { showAppRadioDialog() }
            ).show()
            return
        }
        showAppRadioDialog()
    }

    /**
     * The app's own preset, frequency and power. For an RNode they go to the
     * radio; for a Meshtastic board they are the source of its managed profile
     * ("Use the app's settings"), so they must be editable there too.
     */
    private fun showAppRadioDialog() {
        executor.execute {
            val config = try {
                JSONObject(callPython("get_radio_config") ?: "{}")
            } catch (e: Exception) {
                null
            }
            handler.post {
                if (config == null) {
                    Toast.makeText(this, "Radio settings unavailable", Toast.LENGTH_LONG).show()
                } else {
                    buildRadioDialog(config).show()
                }
            }
        }
    }

    private fun buildRadioDialog(config: JSONObject): AlertDialog {
        val presetsJson = config.optJSONArray("presets")
        val presets = (0 until (presetsJson?.length() ?: 0))
            .map { presetsJson!!.optString(it) }
        // Shown instead of the bare names, and indexed identically: the value
        // sent back is still `presets[position]`, so the label is free to say
        // whatever helps without ever becoming the stored setting. Falls back
        // to the names if an older bridge does not supply them.
        val labelsJson = config.optJSONArray("preset_labels")
        val presetLabels = (0 until (labelsJson?.length() ?: 0))
            .map { labelsJson!!.optString(it) }
            .takeIf { it.size == presets.size } ?: presets

        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(12), dp(24), 0)
        }

        // A list rather than free entry: spreading factor, bandwidth and
        // coding rate only make sense as a set, and letting them be picked
        // independently is how a node ends up on a combination nothing else
        // in the world uses. The names are Meshtastic's.
        layout.addView(TextView(this).apply {
            text = "Preset"
            textSize = 12f
            setTextColor(COLOR_HEADING)
        })
        val presetSpinner = Spinner(this).apply {
            adapter = ArrayAdapter(
                this@MainActivity, android.R.layout.simple_spinner_dropdown_item,
                presetLabels)
            setSelection(presets.indexOf(config.optString("preset")).coerceAtLeast(0))
        }
        layout.addView(presetSpinner)

        layout.addView(TextView(this).apply {
            text = "Frequency (MHz)"
            textSize = 12f
            setTextColor(COLOR_HEADING)
            setPadding(0, dp(12), 0, 0)
        })
        val freqField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            setSingleLine()
            setText(config.optString("frequency_mhz"))
        }
        layout.addView(freqField)

        layout.addView(TextView(this).apply {
            // From Python: 37 for an RNode, 30 for a Meshtastic board.
            text = "TX power (dBm, max ${config.optInt("txpower_max_dbm", 37)})"
            textSize = 12f
            setTextColor(COLOR_HEADING)
            setPadding(0, dp(12), 0, 0)
        })
        val powerField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
            setText(config.optInt("txpower_dbm").toString())
        }
        layout.addView(powerField)

        layout.addView(TextView(this).apply {
            text = "Every node must match. Must be legal where you are — " +
                    "receiving is not transmitting."
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(12), 0, 0)
        })

        return MaterialAlertDialogBuilder(this)
            .setTitle("LoRa settings")
            .setView(layout)
            .setPositiveButton("Apply") { _, _ ->
                applyRadio(
                    presets.getOrElse(presetSpinner.selectedItemPosition) { "" },
                    freqField.text.toString().trim(),
                    powerField.text.toString().trim())
            }
            .setNegativeButton("Cancel", null)
            .create()
    }

    private fun applyRadio(preset: String, frequencyMhz: String, txPower: String) {
        Toast.makeText(this, "Applying…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val problem = callPython("set_radio_config", preset, frequencyMhz, txPower)
            handler.post {
                // Python returns "" on success and the reason otherwise, so an
                // empty string is the good case and anything else is shown
                // verbatim -- the reason is written for the operator.
                Toast.makeText(
                    this,
                    if (problem.isNullOrEmpty()) "Radio set: $preset" else problem,
                    Toast.LENGTH_LONG
                ).show()
                handler.removeCallbacks(refresh)
                handler.post(refresh)
            }
        }
    }

    // logLine now lives in buildSettingsControls(), rangeLine/surveyLine in
    // buildRadioTestControls() -- each next to the controls that write into
    // it, rather than a shared section gathering all three. Built once and
    // updated in place, unlike everything in localBox, which is torn down
    // and rebuilt on every 2 s refresh -- a button living there would be
    // replaced thirty times a minute, and a tap that landed mid-rebuild
    // would go nowhere.

    /**
     * A survey takes the radio off the operating frequency for minutes, so the
     * button says so and the line underneath stays visible for the duration.
     * Being off air without noticing is the failure this guards against --
     * every symptom of it looks like a dead radio or a dead peer.
     */
    private fun toggleSurvey() {
        executor.execute {
            val running = JSONObject(callPython("sweep_status") ?: "{}")
                .optBoolean("running")
            val reply = if (running) callPython("stop_sweep")
                        else callPython("start_sweep")
            handler.post {
                Toast.makeText(this, reply ?: "Survey unavailable", Toast.LENGTH_LONG).show()
            }
        }
    }

    /**
     * Records a CSV row per frame heard, carrying both ends' positions and the
     * signal stats, so a walk can be plotted rather than read. Modelled on
     * Meshtastic's Range Test module -- except the sender already exists, since
     * the mesh beacon is a numbered packet on a fixed interval, which is what
     * a range test transmitter is.
     */
    private fun toggleRangeTest() {
        executor.execute {
            val running = JSONObject(callPython("range_test_status") ?: "{}")
                .optBoolean("running")
            val reply = if (running) callPython("stop_range_test")
                        else callPython("start_range_test")
            handler.post {
                Toast.makeText(
                    this,
                    if (running) "Range test stopped — $reply"
                    else if (reply.isNullOrEmpty()) "Could not start the range test"
                    else "Recording to $reply",
                    Toast.LENGTH_LONG
                ).show()
                handler.removeCallbacks(refresh)
                handler.post(refresh)
            }
        }
    }

    private fun renderRangeTest(status: JSONObject?) {
        val running = status?.optBoolean("running") ?: false
        rangeButton.text = if (running) "Stop range test" else "Range test"
        if (!running) {
            rangeLine.visibility = View.GONE
            return
        }
        val rows = status?.optInt("rows") ?: 0
        val noFix = status?.optInt("no_fix") ?: 0
        // The no-fix count is shown because it is the failure that looks like
        // success: rows accumulate, the file grows, and every one of them is
        // missing the distance the whole test exists to measure.
        rangeLine.text = "range test %s  ·  %d row%s%s".format(
            status?.optString("file") ?: "", rows, if (rows == 1) "" else "s",
            if (noFix > 0) "  ·  $noFix with no fix" else "")
        rangeLine.setTextColor(if (noFix > 0 && noFix == rows) COLOR_DOWN else COLOR_IDLE)
        rangeLine.visibility = View.VISIBLE
    }

    private fun renderSurvey(status: JSONObject?) {
        val running = status?.optBoolean("running") ?: false
        surveyButton.text = if (running) "Stop survey" else "Survey"

        val progress = status?.optJSONObject("progress")
        val result = status?.optJSONArray("result")

        val text = when {
            running && progress != null ->
                "OFF AIR — surveying %.3f MHz (%d/%d)".format(
                    progress.optLong("freq_hz") / 1_000_000.0,
                    progress.optInt("done"), progress.optInt("total"))
            running -> "OFF AIR — survey starting"
            result != null && result.length() > 0 ->
                (0 until result.length()).joinToString("\n") { result.optString(it) }
            else -> ""
        }
        surveyLine.text = text
        surveyLine.setTextColor(if (running) COLOR_DOWN else COLOR_IDLE)
        surveyLine.visibility = if (text.isEmpty()) View.GONE else View.VISIBLE
    }

    /**
     * Export runs off the UI thread: it copies the whole run, which at the
     * 16 MiB cap is far too much to do on the main looper.
     */
    private fun exportLog() {
        Toast.makeText(this, "Exporting…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val where = LogRecorder.export(applicationContext)
            handler.post {
                Toast.makeText(
                    this,
                    where?.let { "Saved to $it" } ?: "Nothing to export",
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }

    /**
     * Copies the newest range test CSV to Downloads. Safe mid-test: every row
     * is flushed as it is written, so the copy holds everything heard so far.
     */
    private fun exportRangeTest() {
        Toast.makeText(this, "Exporting range test…", Toast.LENGTH_SHORT).show()
        executor.execute {
            val where = LogRecorder.exportRangeTest(applicationContext)
            handler.post {
                Toast.makeText(
                    this,
                    where?.let { "Saved to $it" } ?: "No range test to export",
                    Toast.LENGTH_LONG
                ).show()
            }
        }
    }

    /**
     * Red when it is off, because a test with no log is otherwise discovered
     * hours later, back at a laptop, by which point the ring buffer has
     * discarded the answer -- which is exactly how the 2026-08-14 range test
     * lost its record. The size ticking upward is what proves it is actually
     * recording rather than merely having been asked to.
     */
    private fun renderLog() {
        val recording = LogRecorder.isRecording()
        val file = LogRecorder.currentLogFile()
        logToggle.text = if (recording) "Stop log" else "Start log"
        logLine.text = when {
            recording && file != null ->
                "log ${file.name}  ·  ${formatBytes(LogRecorder.currentSizeBytes())}"
            file != null -> "log stopped  ·  ${formatBytes(LogRecorder.currentSizeBytes())}"
            else -> "Log NOT being recorded"
        }
        logLine.setTextColor(if (recording) COLOR_IDLE else COLOR_DOWN)
    }

    /**
     * Shows the current passphrase rather than masking it.
     *
     * Masking would be the reflex, and it would be wrong here: this is a
     * *group* secret whose entire purpose is to be copied onto the other
     * radios, so a field you cannot read is a field you cannot provision from.
     * It is already stored in plaintext in app-private storage, so hiding it
     * on screen protects nothing and costs the one workflow that matters.
     */
    private fun showPassphraseDialog() {
        val field = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setSingleLine()
            hint = "shared passphrase"
        }
        val note = TextView(this).apply {
            text = "Every node on this network needs the same passphrase. " +
                    "After setting it, check that the key shown on each device matches."
            textSize = 12f
            setTextColor(COLOR_IDLE)
            setPadding(0, dp(8), 0, 0)
        }
        val body = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(16), dp(24), 0)
            addView(field)
            addView(note)
        }

        val dialog = MaterialAlertDialogBuilder(this)
            .setTitle("Network passphrase")
            .setView(body)
            .setPositiveButton("Set", null)   // wired below so it can stay open
            .setNegativeButton("Cancel", null)
            .setNeutralButton("Generate", null)   // likewise: fills the field, sets nothing
            .create()

        dialog.setOnShowListener {
            // Six random everyday words (passphrase_gen): readable aloud and
            // typeable on another phone, 62 bits before the key derivation.
            // Only put in the field -- Set still has to be pressed, so a stray
            // tap cannot re-key a phone off its team.
            dialog.getButton(AlertDialog.BUTTON_NEUTRAL).setOnClickListener {
                executor.execute {
                    val made = callPython("generate_mesh_passphrase")
                    handler.post {
                        if (made.isNullOrEmpty()) {
                            Toast.makeText(this, "Could not make a passphrase",
                                Toast.LENGTH_LONG).show()
                        } else {
                            field.setText(made)
                            field.setSelection(made.length)
                            note.text = "A new passphrase starts a new network: every other " +
                                "phone needs these same words, typed or shared by QR code. " +
                                "Then check that the key shown on each device matches."
                        }
                    }
                }
            }
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val entered = field.text.toString().trim()
                if (entered.isEmpty()) {
                    // Refused rather than accepted-as-blank: an empty
                    // passphrase would derive a perfectly valid key that every
                    // install shares, which is worse than no encryption because
                    // it looks like encryption.
                    field.error = "Cannot be empty"
                    return@setOnClickListener
                }
                dialog.dismiss()
                applyPassphrase(entered)
            }
        }
        dialog.show()

        // Prefilled off the interpreter thread -- derive/read crosses into
        // Python, and the dialog should appear immediately either way.
        executor.execute {
            val current = callPython("get_mesh_passphrase") ?: ""
            handler.post { if (current.isNotEmpty()) field.setText(current) }
        }
    }

    private fun applyPassphrase(passphrase: String) {
        executor.execute {
            val fingerprint = callPython("set_mesh_passphrase", passphrase)
            handler.post {
                if (fingerprint.isNullOrEmpty()) {
                    Toast.makeText(this, "Could not set the passphrase", Toast.LENGTH_LONG).show()
                } else {
                    Toast.makeText(this, "Network key is now $fingerprint", Toast.LENGTH_LONG).show()
                }
                // The peer list was cleared by the re-key, so pull a fresh one
                // rather than leaving the old network on screen for 2 seconds.
                // removeCallbacks first: `refresh` reschedules itself, so
                // posting it again without cancelling would leave two chains
                // running and permanently double the poll rate.
                handler.removeCallbacks(refresh)
                handler.post(refresh)
            }
        }
    }

    private fun callPython(method: String, vararg args: String): String? =
        PythonRnsService.call(method, *args)

    /**
     * A plain 800 Hz tone (the modem's own first_tone, so it's testing the
     * same frequency the K1 PRO's threshold circuit has to react to) at full
     * digital and stream volume, held for 10s. No codec2 involved -- this
     * isolates the audio path itself from the modem, so a dial that won't
     * hold steady here is a hardware/level problem no amount of burst-side
     * tuning will fix.
     */
    private fun runTestTone() {
        Toast.makeText(this, "Playing 10s test tone at 800 Hz, full volume...",
            Toast.LENGTH_SHORT).show()
        executor.execute {
            try {
                val sampleRate = 8000
                val freqHz = 800.0
                val durationS = 10.0
                val n = (sampleRate * durationS).toInt()
                val pcm = ByteArray(n * 2)
                for (i in 0 until n) {
                    val sample = (32767.0 * Math.sin(2.0 * Math.PI * freqHz * i / sampleRate))
                        .toInt().coerceIn(-32768, 32767)
                    pcm[i * 2] = (sample and 0xFF).toByte()
                    pcm[i * 2 + 1] = ((sample shr 8) and 0xFF).toByte()
                }

                val audioManager = getSystemService(AUDIO_SERVICE) as AudioManager
                val usbOut = audioManager.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
                    .firstOrNull {
                        it.type == AudioDeviceInfo.TYPE_USB_HEADSET
                                || it.type == AudioDeviceInfo.TYPE_USB_DEVICE
                    }

                val minBuf = AudioTrack.getMinBufferSize(
                    sampleRate, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
                val track = AudioTrack.Builder()
                    .setAudioAttributes(
                        AudioAttributes.Builder()
                            .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                            .build())
                    .setAudioFormat(
                        AudioFormat.Builder()
                            .setSampleRate(sampleRate)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                            .build())
                    .setBufferSizeInBytes(maxOf(minBuf, pcm.size))
                    .setTransferMode(AudioTrack.MODE_STATIC)
                    .build()

                if (usbOut != null) {
                    track.preferredDevice = usbOut
                }
                track.setVolume(1.0f)
                audioManager.setStreamVolume(
                    AudioManager.STREAM_VOICE_CALL,
                    audioManager.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL), 0)

                track.write(pcm, 0, pcm.size)
                track.play()
                Thread.sleep((durationS * 1000).toLong() + 300)
                track.stop()
                track.release()

                handler.post {
                    Toast.makeText(this, "Test tone finished", Toast.LENGTH_SHORT).show()
                }
            } catch (e: Exception) {
                Log.e(TAG, "Test tone failed", e)
                handler.post {
                    Toast.makeText(this, "FAIL: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
    }

    /**
     * RX half of the beacon-code probe. The window is derived from
     * fm_beacon_probe.expected_burst_s() -- the SAME real CODENAME/RS/
     * REPEATS/LEAD_IN_S numbers the TX side actually builds against --
     * rather than a hand-maintained constant, which already went stale
     * once (found 2026-09-20: a fixed 12s window, sized for an older
     * ~7.4s burst, left under 1.1s of margin once REPEATS/RS changed and
     * the real burst grew to ~11s -- not nearly enough for feed()'s own
     * need for roughly another burst-length of trailing audio to flush
     * its last frame, on top of ordinary tap-timing slack). Window =
     * burst duration, doubled (burst itself + a full burst-length of
     * flush tail), plus a fixed lead-in allowance for the gap between
     * tapping this button and tapping TX on the other end.
     */
    private val beaconProbeRxLeadS = 3.0

    /**
     * Writes a small text result to Downloads under a unique, timestamped
     * name -- same MediaStore-on-API-29+/app-files-dir-below-it pattern as
     * LogRecorder.export, and readable off the phone the same two ways
     * (Files app / share target, or `adb pull` with no `run-as` needed).
     * Timestamped rather than a fixed name so a batch script's `ls -t` on
     * Downloads always finds the trial it just ran without the two of them
     * ever colliding on one filename.
     */
    private fun saveTextToDownloads(name: String, text: String): String? =
        saveBytesToDownloads(name, "text/plain", text.toByteArray())

    private fun saveBytesToDownloads(name: String, mime: String, data: ByteArray): String? {
        return try {
            val out = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                val values = ContentValues().apply {
                    put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                    put(MediaStore.MediaColumns.MIME_TYPE, mime)
                    put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
                }
                val uri = contentResolver
                    .insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: return null
                contentResolver.openOutputStream(uri) to "Downloads/$name"
            } else {
                val dir = File(getExternalFilesDir(null), "exports")
                dir.mkdirs()
                val file = File(dir, name)
                file.outputStream() to file.absolutePath
            }
            val (stream, where) = out
            (stream ?: return null).use { it.write(data) }
            where
        } catch (e: Exception) {
            Log.e(TAG, "could not save $name to Downloads", e)
            null
        }
    }

    private fun fetchStatus(): String? = try {
        if (Python.isStarted()) {
            Python.getInstance().getModule("rns_bridge").callAttr("get_status").toString()
        } else {
            null
        }
    } catch (e: Exception) {
        // Expected briefly at startup, before rns_bridge has finished importing.
        Log.d(TAG, "status unavailable", e)
        null
    }

    private fun render(json: String?) {
        localBox.removeAllViews()
        peerBox.removeAllViews()
        messageBox.removeAllViews()

        // Before the early returns below: whether the log is recording is
        // independent of whether the bridge is up, and matters most when it
        // is not. Same for which wire the radio is on -- that line is at its
        // most useful on a screen that is otherwise reporting nothing.
        renderLog()
        renderLink()

        if (json == null) {
            localBox.addView(line("Starting bridge…", COLOR_IDLE))
            hideSections()
            return
        }

        val status = try {
            JSONObject(json)
        } catch (e: Exception) {
            localBox.addView(line("Bad status payload: ${e.message}", COLOR_DOWN))
            hideSections()
            return
        }

        if (!status.optBoolean("ok", false)) {
            localBox.addView(line("Bridge error: ${status.optString("error")}", COLOR_DOWN))
            hideSections()
            return
        }

        peerHeading.visibility = View.VISIBLE

        val nodeId = status.optString("node_id")
        val name = status.optString("name")
        val callsign = status.optString("callsign")
        localBox.addView(TextView(this).apply {
            // Device name leads. The callsign is appended only when ATAK has
            // supplied one -- it must never be the thing you rely on to know
            // which node this is.
            text = "This node: " + (if (name.isEmpty()) nodeId else name) +
                    (if (callsign.isEmpty()) "" else "  [$callsign]")
            textSize = 16f
            setTypeface(typeface, Typeface.BOLD)
            setPadding(0, 0, 0, dp(2))
        })
        localBox.addView(line("id $nodeId", COLOR_IDLE))

        // Which radio this phone is actually using. Only worth a line over
        // Bluetooth: down a cable the answer is visible from across the room,
        // but two radios in a bag and two phones in two pockets is a pairing
        // nothing else on either screen states. "Identify" on the radio link
        // screen makes the box itself say which one it is.
        if (RadioLink.isBluetooth(this)) {
            localBox.addView(line("radio ${RadioLink.radioName(this)}, over Bluetooth", COLOR_IDLE))
        }
        // The same for firmware: an RNode and a Meshtastic board look identical
        // on a desk and behave very differently (a settings page that works on
        // one is refused by the other), and nothing else here says which is
        // plugged in.
        if (RadioLink.isMeshtastic(this)) {
            val wire = if (RadioLink.isBluetooth(this)) "Bluetooth" else "USB"
            localBox.addView(line("radio is a Meshtastic board, over $wire", COLOR_IDLE))
            // Settings that stop the board behaving like the radio the app is
            // built around, in plain words (see mt_config.warnings). Shown here
            // because each one looks like a dead or noisy radio and nothing else
            // on this screen would explain it.
            val board = status.optJSONObject("board")
            val warnings = board?.optJSONArray("warnings")
            if (warnings != null) {
                for (i in 0 until warnings.length()) {
                    localBox.addView(line(warnings.optString(i), COLOR_WARN))
                }
            }
            // The board compared with the APP's own settings -- preset,
            // frequency, power, the channel derived from the passphrase. A
            // difference is how two phones with one passphrase end up on boards
            // that cannot hear each other, so it is said here, with the remedy.
            val differs = board?.optJSONArray("profile_changes")?.length() ?: 0
            if (differs > 0) {
                localBox.addView(line(
                    "The board is not on this app's settings ($differs " +
                        (if (differs == 1) "difference" else "differences") +
                        ") - LoRa settings → Use the app's settings",
                    COLOR_WARN
                ))
            }
        }

        // Read off both phones and compare, the same way the key fingerprint
        // is used. A protocol gap makes two nodes ignore each other while both
        // look perfectly healthy, and the build stamp catches the case before
        // that one: only one phone got the new APK.
        // Informational lines (build stamp, FM net code and operating point,
        // receive summary, re-send counts) only with Debug → Advanced
        // features → Show detailed status. Warnings always show.
        val details = RadioLink.statusDetails(this)
        val protocol = status.optInt("protocol", 0)
        if (details) localBox.addView(line(
            "protocol v$protocol   ·   build ${BuildConfig.BUILD_STAMP}",
            COLOR_IDLE
        ))

        // Present only when an FM interface is actually BUILT -- FM-only mode
        // or LoRa-primary with FM backup. Its absence is what keeps every
        // LoRa-only node's screen exactly as it was: there is no audio path
        // there to report "no audio" about.
        val fm = status.optJSONObject("fm")
        val fmHealth = fm?.optJSONObject("health")

        val rnodeUp = status.optBoolean("rnode_online")
        val atakUp = status.optBoolean("atak_connected")
        val peerCount = status.optJSONArray("peers")?.length() ?: 0
        val reliable = status.optJSONObject("reliable")
        val waiting = reliable?.optJSONArray("inflight")?.length() ?: 0
        val failed = reliable?.optJSONArray("undelivered")?.length() ?: 0

        // Three words, in the order you would ask them: is the radio alive, is
        // ATAK attached, is anybody out there. Everything below is detail.
        val strip = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(8), 0, dp(10))
        }
        // A LINK THAT REPORTS online=true FOR ITS WHOLE LIFE CANNOT DRIVE
        // THIS PILL. FmInterface is such a link -- it is online from
        // construction, so "Radio ok" would show with the cable unplugged and
        // the radio off. What the receive path has actually OBSERVED is the
        // honest substitute; see fm_health.py for why it is graded rather than
        // binary. Two of the gradings are the point: "No traffic" is a WARNING
        // and not a fault, because an idle link and a broken one look
        // identical from here and claiming either would be a guess, while
        // "Garbled" IS a fault -- the link is provably carrying signal and
        // provably delivering none of it.
        if (fmHealth != null) {
            val fmState = fmHealth.optString("state")
            strip.addView(pill(
                when (fmState) {
                    "hearing" -> "Hearing"
                    // Decoding, but nothing usable came out of it -- see
                    // fm_health.py's GARBLED. Named for the symptom the
                    // operator can act on, not the mechanism.
                    "garbled" -> "Garbled"
                    "quiet" -> "No traffic"
                    "deaf" -> "Silent"
                    "down" -> "No audio"
                    else -> "Audio ?"
                },
                when (fmState) {
                    "hearing" -> STATE_UP
                    "quiet" -> STATE_WARN
                    "garbled" -> STATE_DOWN
                    else -> STATE_DOWN
                }))
        } else {
            strip.addView(pill(
                if (rnodeUp) "Radio ok" else "No radio",
                if (rnodeUp) STATE_UP else STATE_DOWN))
        }
        strip.addView(pill(
            if (atakUp) "ATAK ok" else "No ATAK",
            if (atakUp) STATE_UP else STATE_DOWN))
        // A mesh of nobody is not a fault -- it is what a node alone looks
        // like -- so it warns rather than alarms.
        strip.addView(pill(
            if (peerCount == 0) "No peers" else "Mesh $peerCount",
            if (peerCount == 0) STATE_WARN else STATE_UP))
        localBox.addView(strip)

        // THE FM LINK'S OWN HONESTY BLOCK: four things no other link has to
        // say, all of them consequences of there being no acknowledgements on
        // this one (see fm_resend.py). Each existed for a measured reason, so
        // each is commented where it is rendered rather than as a list here.
        if (fm != null) {
            // 1. WHAT THIS LINK IS RUNNING, first in the block, because the
            //    tone placement is the one FM setting whose mismatch produces
            //    no symptom but silence -- on both ends, with nothing in
            //    either log to say so. Two operators comparing this line is
            //    still the only check that exists.
            //
            //    Tinted as a warning when the placement is not the measured
            //    default: a non-standard one is not an error, but it IS
            //    something worth noticing before concluding the radios are at
            //    fault.
            // The net code first: the short line two operators read to each
            // other. Equal codes are on the same net whatever each node's
            // profile is called (fm_custom.net_code).
            fm.optString("net_code").takeIf { details && it.isNotEmpty() }?.let {
                localBox.addView(line("FM net code: $it (must match the other radios)",
                    if (fm.optBoolean("profile_is_default", true)) COLOR_IDLE
                    else COLOR_WARN))
            }
            fm.optString("operating_point").takeIf { details && it.isNotEmpty() }?.let {
                localBox.addView(line(it,
                    if (fm.optBoolean("profile_is_default", true)) COLOR_IDLE
                    else COLOR_WARN))
            }
            // What that placement has actually been MEASURED on, which for a
            // handheld operator running the desktop-measured default is the
            // one honest thing this screen can tell them. New in the rebuild
            // -- Phase 1 made every profile carry its own evidence precisely
            // so this line could exist.
            fm.optString("evidence").takeIf { details && it.isNotEmpty() }?.let {
                localBox.addView(line(it, COLOR_IDLE))
            }
            // A burst going out UNKEYED although the configuration asks for
            // CM108. Read from the audio service rather than from Python,
            // because it is a fact about the cable attached to this phone --
            // see FmAudioBridgeService.keylessReason, which also has the
            // measurement for why this is marginal rather than fatal.
            // TEST MODE, read live from the preference rather than from Python:
            // the node is online and receiving, so nothing else on this screen
            // looks wrong, and it reaches nobody. See RadioLink.fmTxOff.
            if (RadioLink.fmTxOff(this)) {
                localBox.addView(line(
                    "FM TEST MODE: this node is NOT transmitting. It hears " +
                        "traffic but sends no beacons, messages or resends, so " +
                        "other nodes will lose it. Turn it off in Radio link " +
                        "(Debug tab → Show advanced features) when the test is done.",
                    COLOR_WARN))
            }
            FmAudioBridgeService.keylessReason?.let {
                localBox.addView(line(
                    "FM is transmitting WITHOUT PTT ($it). The lead-in is " +
                        "sized for keyed transmission, so VOX will clip the " +
                        "front of every burst -- plug in a CM108 cable or " +
                        "expect marginal delivery.",
                    COLOR_WARN))
            }
            // 2. THE RECEIVE READING IN WORDS, because the states it
            //    distinguishes are the ones an operator can act on: "Silent"
            //    means check the radio, "No audio" means the app is broken.
            //    Tinted on saturation, which is the one measured, directly
            //    fixable condition here whose symptom otherwise reads as
            //    healthy.
            fmHealth?.optString("summary")?.takeIf {
                it.isNotEmpty() && (details || fmHealth.optBoolean("saturated"))
            }?.let {
                localBox.addView(line(it,
                    if (fmHealth.optBoolean("saturated")) COLOR_WARN else COLOR_IDLE))
            }
            // 3. THE RECEIVE LEVEL as a live number, with the target band and
            //    which way to turn the knob -- see fm_health.level_line(). The
            //    saturation warning above says only that the level is wrong in
            //    one direction; this says where it actually is, so correcting
            //    it cannot silently overshoot into "Silent", which is exactly
            //    what happened twice on 2026-09-30.
            // Shown without details when it is advice: which way to turn the
            // volume knob.
            fmHealth?.optString("level_line")?.takeIf {
                it.isNotEmpty() && (details ||
                    fmHealth.optString("level_state") !in setOf("good", "none"))
            }?.let {
                localBox.addView(line(it, when (fmHealth.optString("level_state")) {
                    "good" -> COLOR_UP
                    "none" -> COLOR_IDLE
                    else -> COLOR_WARN
                }))
            }
            // 4. THAT DELIVERY IS NOT CONFIRMABLE AT ALL, stated plainly
            //    rather than implied by the absence of an error.
            val inflight = fm.optJSONObject("resend")
                ?.optJSONArray("inflight")?.length() ?: 0
            val confirmed = fm.optInt("confirmed_total", 0)
            if (inflight > 0) {
                localBox.addView(line(
                    "$inflight message(s) still being re-sent -- confirmed " +
                        "only if a peer happens to report hearing them",
                    COLOR_WARN))
            } else if (details) {
                localBox.addView(line(
                    "nothing waiting to be re-sent", COLOR_IDLE))
            }
            // Worth stating separately from the count above: a confirmation
            // here is a peer volunteering that it heard something, on its own
            // beacon schedule. The absence of one means nothing at all, which
            // is the opposite of how a delivery indicator usually reads.
            if (details && confirmed > 0) {
                localBox.addView(line(
                    "$confirmed message(s) confirmed heard by a peer",
                    COLOR_IDLE))
            }
        }

        // Which links exist and which one is carrying traffic, as a row of
        // buttons: the active link filled, a link standing by as backup
        // outlined, one that is not set up dimmed. Tapping any opens Radio
        // link. The force buttons moved to the Debug tab (Advanced features),
        // so a forced link is spelled out here: it is the one setting that
        // can strand a node on a dead link with nothing else looking wrong.
        // The "link" block from get_status() exists only once two links are
        // BUILT; without it the one link there is is the active one.
        val linkBlock = status.optJSONObject("link")
        val fmBuilt = status.optJSONObject("fm") != null
        val loraBuilt = !RadioLink.isFm(this)
        val wifiBuilt = linkBlock?.optJSONObject("wifi_health") != null
        val activeLink = linkBlock?.optString("active_link")
            ?: if (loraBuilt) "lora" else "fm"
        val override = linkBlock?.optString("link_override", "auto") ?: "auto"
        val chips = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(0, dp(4), 0, dp(4))
        }
        fun chip(name: String, link: String, built: Boolean) {
            val active = built && link == activeLink
            val state = when {
                active -> "active"
                built -> "backup"
                else -> "off"
            }
            val b = if (active) Ui.filled(this, "$name · $state")
                    else Ui.outlined(this, "$name · $state")
            b.apply {
                textSize = 12f
                minWidth = 0
                minimumWidth = 0
                setPadding(dp(4), paddingTop, dp(4), paddingBottom)
                alpha = if (built) 1f else 0.45f
                layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
                    .apply { marginEnd = dp(4) }
                setOnClickListener { showRadioLinkDialog() }
            }
            chips.addView(b)
        }
        chip("LoRa", "lora", loraBuilt)
        chip("FM", "fm", fmBuilt)
        // WiFi is an advanced link: its button only appears once it is on, or
        // once advanced features are.
        if (wifiBuilt || RadioLink.advancedEnabled(this)) chip("WiFi", "wifi", wifiBuilt)
        localBox.addView(chips)
        if (linkBlock != null && override != "auto") {
            localBox.addView(line(
                "Link forced to " + linkName(override) + ": automatic failover is off. Debug tab → Advanced " +
                    "features → Auto turns it back on.",
                COLOR_WARN))
        }
        renderOverrideButtons(override, linkBlock != null)

        val duty = status.optDouble("duty_pct", 0.0)
        val load = status.optDouble("channel_load_pct", -1.0)
        localBox.addView(tileRow(
            tile("Airtime", "%.1f%%".format(duty), levelFor(duty, AIRTIME_WARN, AIRTIME_BAD)),
            tile("Channel",
                if (load < 0) "—" else "%.0f%%".format(load),
                if (load < 0) STATE_UP else levelFor(load, AIRTIME_WARN, AIRTIME_BAD))
        ))
        localBox.addView(tileRow(
            // Red here means the node is suppressing its own retries: this is
            // DEFAULT_CONGESTION_INFLIGHT, so the screen and reliable.py agree
            // on what "struggling" means.
            tile("Waiting", waiting.toString(),
                when {
                    waiting > WAITING_BAD -> STATE_DOWN
                    waiting >= WAITING_WARN -> STATE_WARN
                    else -> STATE_UP
                }),
            tile("Undelivered", failed.toString(),
                if (failed > 0) STATE_DOWN else STATE_UP)
        ))

        // Conditions on the channel rather than this node's own behaviour --
        // the number that says whether a weak signal is weak because the other
        // node is far away or because this end is sitting in noise. Absent
        // until the radio has reported its first stats frame.
        // Above the channel conditions: what the radio is SET to has to be
        // readable next to what it is HEARING, and the setting is the one you
        // compare against the other phone.
        val radioLine = status.optString("radio")
        if (radioLine.isNotEmpty()) localBox.addView(line(radioLine, COLOR_IDLE))

        // Demoted, not deleted. The channel line and the tx-policy counters are
        // the two most useful things on this screen when something is wrong and
        // the two least useful the rest of the time -- and the policy line is
        // raw log output, which is not something to read first while standing
        // outside. Folded behind a tap; the numbers that matter are tiles now.
        val channel = status.optString("channel")
        val policy = status.optString("policy")
        val detail = listOf(channel, policy).filter { it.isNotEmpty() }
        if (detail.isNotEmpty()) {
            val body = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                visibility = if (diagnosticsOpen) View.VISIBLE else View.GONE
            }
            detail.forEach { body.addView(line(it, COLOR_IDLE)) }
            localBox.addView(TextView(this).apply {
                text = if (diagnosticsOpen) "Diagnostics ⌄" else "Diagnostics ›"
                textSize = 13f
                setTextColor(COLOR_HEADING)
                setPadding(0, dp(8), 0, dp(4))
                setOnClickListener {
                    diagnosticsOpen = !diagnosticsOpen
                    body.visibility = if (diagnosticsOpen) View.VISIBLE else View.GONE
                    text = if (diagnosticsOpen) "Diagnostics ⌄" else "Diagnostics ›"
                }
            })
            localBox.addView(body)
        }

        renderKey(status)

        renderMessages(status.optJSONObject("reliable"))

        val peers = status.optJSONArray("peers")
        if (peers == null || peers.length() == 0) {
            peerBox.addView(line("No peers heard yet.", COLOR_IDLE))
            return
        }
        for (i in 0 until peers.length()) {
            peerBox.addView(peerRow(peers.getJSONObject(i)))
        }
    }

    /**
     * "Network key 3f9a · 12 heard" — the two facts that answer "why is the
     * other phone not showing up".
     *
     * The warning keys off `since_accept`, the run of rejections with nothing
     * decrypting in between, and NOT off the cumulative rejected count. The
     * 2026-08-04 bench proved why: when one phone re-keyed, the *other* one
     * had 33 successful frames behind it and 2 rejections in front, so any
     * rule comparing totals stayed quiet on the device that had just been cut
     * off — the one whose user most needed telling. `since_accept` returns to
     * zero the moment anything decrypts, so it describes now, not history.
     */
    private fun renderKey(status: JSONObject) {
        val key = status.optString("key")
        val meshStats = status.optJSONObject("mesh")
        val rejected = meshStats?.optInt("unauthenticated", 0) ?: 0
        val accepted = meshStats?.optInt("unwrapped", 0) ?: 0
        val sinceAccept = meshStats?.optInt("since_accept", 0) ?: 0

        if (!status.optBoolean("encrypted", false)) {
            keyLine.text = "Network key: not set up yet"
            keyLine.setTextColor(COLOR_IDLE)
            return
        }
        // Checked before the passphrase warning: both can be true at once, but
        // only one names the actual fix, and updating one phone before the
        // other is a far more likely way to arrive here than a typo.
        val wrongVersion = meshStats?.optInt("wrong_version", 0) ?: 0
        if (wrongVersion > 0) {
            val peer = meshStats?.optInt("peer_version", 0) ?: 0
            val mine = meshStats?.optInt("version", 0) ?: 0
            keyLine.text = "Another node is on protocol v$peer, this one is v$mine — update it"
            keyLine.setTextColor(COLOR_DOWN)
            return
        }

        val mismatch = sinceAccept >= MISMATCH_FRAMES
        val detail = StringBuilder("Network key $key")
        if (mismatch) {
            // Names the likely cause rather than reporting a number, because
            // "2 rejected" reads as trivia and "check the passphrase" is the
            // actual next step.
            detail.append("  ·  nothing decrypting — check the passphrase")
        } else {
            if (accepted > 0) detail.append("  ·  $accepted heard")
            if (rejected > 0) detail.append("  ·  $rejected rejected")
        }
        keyLine.text = detail.toString()
        keyLine.setTextColor(if (mismatch) COLOR_DOWN else COLOR_IDLE)
    }

    private fun hideSections() {
        peerHeading.visibility = View.GONE
        messageHeading.visibility = View.GONE
        messageBox.visibility = View.GONE
    }

    /**
     * One-shot traffic -- chat, drawings, routes -- awaiting acknowledgement,
     * and the ones that never got it.
     *
     * Position reports never appear here, and shouldn't: ATAK re-sends them
     * every few seconds, so a dropped one costs nothing. A dropped chat
     * message is gone for good, and before this section existed it looked
     * exactly like one that had arrived.
     */
    private fun renderMessages(reliable: JSONObject?) {
        val inflight = reliable?.optJSONArray("inflight")
        val undelivered = reliable?.optJSONArray("undelivered")
        val count = (inflight?.length() ?: 0) + (undelivered?.length() ?: 0)
        if (count == 0) {
            messageHeading.visibility = View.GONE
            messageBox.visibility = View.GONE
            return
        }
        messageHeading.visibility = View.VISIBLE
        messageBox.visibility = View.VISIBLE

        // Failures first. Something still in flight is not news; something
        // that never arrived is.
        if (undelivered != null) {
            for (i in 0 until undelivered.length()) {
                messageBox.addView(messageRow(undelivered.getJSONObject(i), false))
            }
        }
        if (inflight != null) {
            for (i in 0 until inflight.length()) {
                messageBox.addView(messageRow(inflight.getJSONObject(i), true))
            }
        }
    }

    private fun messageRow(msg: JSONObject, inFlight: Boolean): View {
        val peers = msg.optJSONArray(if (inFlight) "waiting_on" else "missing")
        val names = StringBuilder()
        for (i in 0 until (peers?.length() ?: 0)) {
            val peerName = peers!!.optString(i)
            if (peerName.isEmpty()) continue
            if (names.isNotEmpty()) names.append(", ")
            names.append(peerName)
        }
        val attempts = msg.optInt("attempts", 1)
        val age = msg.optDouble("age_s", 0.0)

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(6), 0, dp(6))
        }

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        header.addView(TextView(this).apply {
            text = if (inFlight) "◌" else "✕"
            setTextColor(if (inFlight) COLOR_IDLE else COLOR_DOWN)
            textSize = 16f
            setPadding(0, 0, dp(10), 0)
        })
        header.addView(TextView(this).apply {
            // Worded for what was observed: a message here has had NO answer
            // from anyone. One that a single node acknowledged leaves this
            // list as done, whoever else may still lack it (see the Python
            // log's "acknowledged by ... NOT confirmed for ..." line).
            text = if (inFlight) "Waiting for any node to acknowledge" else "Nobody acknowledged it"
            textSize = 15f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(if (inFlight) Ui.onSurface(this@MainActivity) else COLOR_DOWN)
        })
        row.addView(header)

        // A message displaced by the in-flight cap was never given its retries,
        // so "no reply from" would blame a peer for a decision this node made.
        val reason = msg.optString("reason")
        val displaced = !inFlight && reason.isNotEmpty() && reason != "no acknowledgement"

        val detail = StringBuilder()
        when {
            displaced -> detail.append(reason)
            names.isEmpty() -> detail.append(if (inFlight) "no peers" else "no peer answered")
            else -> detail.append(if (inFlight) "waiting on " else "no reply from ").append(names)
        }
        detail.append("  ·  ").append(if (attempts == 1) "1 try" else "$attempts tries")
        detail.append("  ·  ").append(if (age < 1) "just now" else "%.0fs ago".format(age))
        row.addView(line(detail.toString(), COLOR_IDLE))
        row.addView(line(msg.optString("label"), COLOR_IDLE))
        return row
    }

    private fun peerRow(peer: JSONObject): View {
        val connected = peer.optBoolean("connected")
        val nodeId = peer.optString("node_id")
        val name = peer.optString("name")
        val callsign = peer.optString("callsign")
        val age = peer.optDouble("age_s", 0.0)

        // SNR decides whether this link will still be there in a minute, so it
        // gets a column of its own instead of being fourth in a run-on
        // sentence. The bar carries the same state as the number, so the row
        // reads at arm's length without parsing digits.
        val snr = if (connected && !peer.isNull("snr")) peer.optDouble("snr") else null
        val linkState = when {
            !connected -> STATE_DOWN
            snr == null -> STATE_UP
            else -> levelForSignal(snr, SNR_WARN, SNR_BAD)
        }

        val outer = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(8), 0, dp(8))
        }
        outer.addView(View(this).apply {
            background = rounded(inkFor(linkState), 2)
            layoutParams = LinearLayout.LayoutParams(dp(4), dp(30))
                .apply { marginEnd = dp(10) }
        })

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
        }

        val header = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
        }
        header.addView(TextView(this).apply {
            // The device's own name, which exists whether or not that node is
            // running ATAK. Falls back to the node id only if an older peer
            // isn't advertising one.
            text = if (name.isEmpty()) nodeId else name
            textSize = 17f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(if (connected) Ui.onSurface(this@MainActivity) else COLOR_IDLE)
        })
        if (callsign.isNotEmpty()) {
            header.addView(TextView(this).apply {
                text = "  [$callsign]"
                textSize = 14f
                setTextColor(COLOR_IDLE)
            })
        }
        row.addView(header)

        // Signal is only meaningful for a peer we can currently hear; showing
        // the last-known value next to a DISCONNECTED node reads as if the
        // link were fine.
        val detail = StringBuilder()
        // Not "no beacon" any more: since milestone 6 step 2 the beacon stays
        // quiet whenever other traffic is already proving the link, so silence
        // here means nothing at all has been heard, not that a probe is missing.
        detail.append(if (connected) "connected" else "not heard")
        detail.append("  ·  ")
        detail.append(if (age < 1) "just now" else "%.0fs ago".format(age))
        // RSSI stays in the line; SNR has moved to its own column on the right.
        if (connected && !peer.isNull("rssi")) {
            detail.append("  ·  ").append("%d dBm".format(peer.optInt("rssi")))
        }
        // Loss, off the mesh sequence rather than the beacon's: it counts every
        // frame, so this is the whole link's loss rate and not the probes'.
        val heard = peer.optInt("heard")
        val lost = peer.optInt("lost")
        if (heard + lost > 0 && lost > 0) {
            detail.append("  ·  ")
                .append("%d%% lost".format(100 * lost / (heard + lost)))
        }
        if (name.isNotEmpty()) detail.append("  ·  ").append(nodeId)

        row.addView(line(detail.toString(), COLOR_IDLE))
        outer.addView(row)

        if (snr != null) {
            val signal = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                gravity = Gravity.CENTER_HORIZONTAL
                setPadding(dp(8), 0, 0, 0)
            }
            signal.addView(TextView(this).apply {
                text = "%.1f".format(snr)
                textSize = 15f
                setTypeface(typeface, Typeface.BOLD)
                setTextColor(inkFor(linkState))
            })
            signal.addView(TextView(this).apply {
                text = "SNR"
                textSize = 12f
                setTextColor(COLOR_IDLE)
            })
            outer.addView(signal)
        }
        return outer
    }

    private fun dot(label: String, value: String, up: Boolean): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(2), 0, dp(2))
        }
        row.addView(TextView(this).apply {
            text = if (up) "●" else "○"
            setTextColor(if (up) COLOR_UP else COLOR_DOWN)
            textSize = 14f
            setPadding(0, 0, dp(10), 0)
        })
        row.addView(TextView(this).apply {
            text = "$label: $value"
            textSize = 14f
            setTextColor(if (up) Ui.onSurface(this@MainActivity) else COLOR_DOWN)
        })
        return row
    }

    /** Higher is worse: airtime, channel load. */
    private fun levelFor(value: Double, warn: Double, bad: Double) = when {
        value >= bad -> STATE_DOWN
        value >= warn -> STATE_WARN
        else -> STATE_UP
    }

    /** Higher is better: SNR. */
    private fun levelForSignal(value: Double, warn: Double, bad: Double) = when {
        value <= bad -> STATE_DOWN
        value <= warn -> STATE_WARN
        else -> STATE_UP
    }

    private fun inkFor(state: Int) = when (state) {
        STATE_UP -> COLOR_UP
        STATE_WARN -> COLOR_WARN
        else -> COLOR_DOWN
    }

    private fun tintFor(state: Int) = when (state) {
        STATE_UP -> TINT_UP
        STATE_WARN -> TINT_WARN
        else -> TINT_DOWN
    }

    private fun rounded(fill: Int, radius: Int = 8) = GradientDrawable().apply {
        shape = GradientDrawable.RECTANGLE
        cornerRadius = dp(radius).toFloat()
        setColor(fill)
    }

    /**
     * One of the three words that answer "is this working" without reading
     * anything else. Colour is never the only signal -- the label changes too,
     * so it survives sunlight and colour blindness.
     */
    private fun pill(text: String, state: Int): View = TextView(this).apply {
        this.text = text
        textSize = 12f
        setTypeface(typeface, Typeface.BOLD)
        gravity = Gravity.CENTER
        setTextColor(inkFor(state))
        background = rounded(tintFor(state), 6)
        setPadding(dp(4), dp(7), dp(4), dp(7))
        layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
            .apply { marginEnd = dp(6) }
    }

    /**
     * A number worth glancing at, sized and coloured so that glancing is
     * enough. The label stays small and grey; the value carries the state.
     */
    private fun tile(label: String, value: String, state: Int): View {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(TINT_TILE)
            setPadding(dp(11), dp(9), dp(11), dp(9))
            layoutParams = LinearLayout.LayoutParams(0, WRAP, 1f)
                .apply { marginEnd = dp(6); bottomMargin = dp(6) }
        }
        box.addView(TextView(this).apply {
            text = label
            textSize = 12f
            setTextColor(COLOR_IDLE)
        })
        box.addView(TextView(this).apply {
            text = value
            textSize = 20f
            setTypeface(typeface, Typeface.BOLD)
            setTextColor(inkFor(state))
        })
        return box
    }

    private fun tileRow(vararg tiles: View): View =
        LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            tiles.forEach { addView(it) }
        }

    private fun line(text: String, color: Int) = TextView(this).apply {
        this.text = text
        textSize = 13f
        setTextColor(color)
        setPadding(dp(24), dp(1), 0, dp(1))
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()

    /** Whole KB/MB -- this is a glanceable "is it growing?", not a metric. */
    private fun formatBytes(bytes: Long) = when {
        bytes >= 1024L * 1024L -> "%.1f MB".format(bytes / (1024.0 * 1024.0))
        bytes >= 1024L -> "%d KB".format(bytes / 1024L)
        else -> "$bytes B"
    }
}
