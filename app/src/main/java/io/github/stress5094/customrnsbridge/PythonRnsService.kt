package io.github.stress5094.customrnsbridge

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ApplicationInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.util.Log
import com.chaquo.python.PyException
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File

/**
 * Call startBridge(context) once, e.g. from MainActivity.onCreate() after
 * USB permission has been granted. Order matters: the Kotlin loopback
 * servers must be listening before Python tries to connect to them, which
 * is why the two services are started first and rns_bridge.py polls with
 * a retry/backoff rather than assuming the socket is instantly available.
 */
object PythonRnsService {
    private const val TAG = "PythonRnsService"

    // Held here, not just as a local in startBridge(): a MulticastLock with
    // no live reference is eligible for GC as soon as startBridge() returns,
    // and its finalizer releases it silently within seconds of being
    // collected (the same safety net WakeLock has, logged only as an
    // uncaught finalizer exception elsewhere in logcat, nothing that names
    // this lock) -- confirmed on real hardware: dumpsys wifi showed the
    // previous local-only version's lock sessions lasting ~8-20s, not the
    // process lifetime the code intended, and AutoInterface never found a
    // peer as a result. A companion-object var keeps it reachable for as
    // long as the process the comment below already says it should live.
    private var multicastLock: WifiManager.MulticastLock? = null

    // adb hook for the 2026-10-12 over-the-air profile comparison. DEBUG BUILDS
    // ONLY (checked below), because it lets any app on the phone key the radio.
    //   adb shell am broadcast -a io.github.stress5094.customrnsbridge.FM_TEST \
    //       --es cmd profile --es name twotone
    //   ... --es cmd train --es n 12 --es gap 9 --es label A
    //   ... --es cmd beacons --es on 0
    //   ... --es cmd custom --es spec 4T-1000/500-500Bd-H1024.2048
    //   ... --es cmd sweep_send          /  --es cmd sweep_listen --es seconds 30
    //   ... --es cmd rangetest --es on 1 (start)  /  --es on 0 (stop)
    // Results are in logcat under the [fm-test] prefix (python.stdout).
    private const val SETTINGS_TICK_MS = 10_000L
    private const val FM_TEST_ACTION = "io.github.stress5094.customrnsbridge.FM_TEST"
    private var fmTestReceiver: BroadcastReceiver? = null

    private fun registerFmTestHook(context: Context) {
        if (context.applicationInfo.flags and ApplicationInfo.FLAG_DEBUGGABLE == 0) return
        if (fmTestReceiver != null) return
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(c: Context, intent: Intent) {
                val cmd = intent.getStringExtra("cmd") ?: return
                // Off the main thread: Chaquopy calls can block on the GIL.
                Thread {
                    when (cmd) {
                        "profile" -> call("fm_test_profile", intent.getStringExtra("name").orEmpty())
                        // Custom settings by net code or JSON; refused, not
                        // substituted, if they cannot be built.
                        "custom" -> call("fm_test_custom", intent.getStringExtra("spec").orEmpty())
                        // The tone sweep (fm_tune): send it, or listen for one.
                        "sweep_send" -> call("fm_tune_send")
                        "sweep_listen" -> call("fm_tune_listen", intent.getStringExtra("seconds") ?: "30")
                        "train" -> call(
                            "fm_test_train",
                            intent.getStringExtra("n") ?: "10",
                            intent.getStringExtra("gap") ?: "9",
                            intent.getStringExtra("label").orEmpty()
                        )
                        "beacons" -> call("fm_test_beacons", intent.getStringExtra("on") ?: "1")
                        "rangetest" -> call("fm_test_rangetest", intent.getStringExtra("on") ?: "1")
                        else -> Log.w(TAG, "unknown FM_TEST cmd $cmd")
                    }
                }.start()
            }
        }
        val filter = IntentFilter(FM_TEST_ACTION)
        if (Build.VERSION.SDK_INT >= 33) {
            context.applicationContext.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            context.applicationContext.registerReceiver(receiver, filter)
        }
        fmTestReceiver = receiver
    }

    fun startBridge(context: Context) {
        // Before anything else, so the radio parameters and the negotiated
        // bitrate are in the file rather than only in a ring buffer that will
        // have discarded them by the time anyone reads it. See LogRecorder.
        LogRecorder.start(context)

        if (!Python.isStarted()) {
            Python.start(AndroidPlatform(context))
        }

        RadioLink.startBridgeServices(context)
        context.startForegroundService(Intent(context, CotTakServerBridgeService::class.java))

        // Android's WiFi radio silently drops incoming multicast to save
        // power unless an app explicitly asks for an exception -- without
        // this, AutoInterface's peer discovery (IPv6 multicast) just never
        // finds anyone, with no error anywhere. Held for the process
        // lifetime deliberately, same as everything else here: this app has
        // no teardown path today (a restart kills and relaunches the
        // process rather than tearing down in place -- see RestartActivity),
        // so there is nowhere that would release it early anyway. Acquired
        // even when WiFi ends up not building (e.g. the mesh is briefly
        // down) -- harmless, and simpler than threading the outcome of a
        // background-thread interface build back to this call.
        if (RadioLink.wifiEnabled(context)) {
            val wifiManager =
                context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            multicastLock = wifiManager?.createMulticastLock(TAG)?.apply {
                setReferenceCounted(false)
                acquire()
            }
        }

        registerFmTestHook(context)

        Thread {
            try {
                // Point the Coverage tab at any bundled building data (see
                // tools/overture_to_geojson.py). Coverage is a standalone
                // module with no RNS import, so it can be told its data path
                // here, before the mesh starts, and answered regardless.
                // Sideloaded to app-external storage so a cable/file manager
                // can drop a bundle onto the phone without a rebuild.
                val coverageDir = File(context.getExternalFilesDir(null), "coverage")
                    .apply { mkdirs() }
                callModule("coverage", "set_bundled_dir", coverageDir.absolutePath)

                val py = Python.getInstance()
                val bridge = py.getModule("rns_bridge")
                // Identify this node by the DEVICE, before start() so the very
                // first beacon carries it. Deliberately not ATAK's callsign:
                // when ATAK is the thing that's broken, you still need to know
                // which physical radio you're talking to to confirm the link.
                bridge.callAttr("set_node_name", deviceName())
                // The app build this phone is running, so a field-test log
                // is self-describing without having to ask "which APK was
                // this?" afterward -- see the session-start line in start().
                bridge.callAttr("set_session_info", BuildConfig.BUILD_STAMP)
                // What the RADIO calls itself, which nothing in the protocol
                // carries -- only the phone's Bluetooth stack knows it. Blank
                // over USB, where the cable answers the question. The BLE
                // service overwrites this with the name of whatever it
                // actually connected to, which is the more honest answer when
                // no specific radio was chosen.
                bridge.callAttr(
                    "set_radio_label",
                    if (RadioLink.isBluetooth(context)) RadioLink.bleName(context).orEmpty() else ""
                )
                bridge.callAttr("set_transport", RadioLink.transport(context))
                bridge.callAttr("set_radio_firmware", RadioLink.radioFirmware(context))
                bridge.callAttr("set_fm_backup_enabled", RadioLink.fmBackupEnabled(context))
                bridge.callAttr("set_fm_clear", RadioLink.fmClear(context))
                // THE ORDERING MATTERS, which is why these belong here rather
                // than after start(): a tone placement decides what BOTH the
                // transmit burst and the receive modem are built with, and a
                // node on a different placement than its peers is not reachable
                // on FM at all -- nothing on that link can report the mismatch,
                // because reporting it would require the link to work.
                //
                // NOT ORDERED AGAINST FmAudioBridgeService, though, and that is
                // worth knowing rather than fixing here: this thread runs
                // alongside the bridge services startBridgeServices() launched
                // above, so the audio service can open its sound card before
                // this call lands. That is why it asks fm_link for the
                // operating point per burst rather than capturing it once --
                // see FmAudioBridgeService.AudioPlan. Moving this call earlier
                // would not fix it either: the service's own startup is
                // asynchronous.
                bridge.callAttr(
                    "set_fm_profile", RadioLink.fmToneProfile(context),
                    RadioLink.fmVoxForBridge(context),
                    RadioLink.fmCustomSpec(context)
                )
                // The WiFi link -- independent of the transport entirely,
                // see RadioLink.wifiEnabled()'s own comment.
                bridge.callAttr("set_wifi_enabled", RadioLink.wifiEnabled(context))
                bridge.callAttr("set_wifi_ifname", RadioLink.wifiIfname(context))
                // How the FM modem finds libcodec2.so: the bundled arm64 .so
                // sits in the app's nativeLibraryDir, which nothing
                // find_library() tries will resolve through the app's linker
                // namespace. See fm_link._prepare_native(). Dormant on a
                // LoRa-only node, which is why it is passed unconditionally
                // rather than behind an FM check -- it costs one string and
                // the alternative is a link that cannot load its modem
                // because a flag was not set.
                bridge.callAttr("set_native_lib_dir", context.applicationInfo.nativeLibraryDir)
                bridge.callAttr("start")
                // AFTER start(), not before: start() itself calls
                // _set_active_link() to establish the default (LoRa in dual
                // mode), and an override applied any earlier would just be
                // overwritten by that. Re-applies the operator's last
                // override choice so it survives a process restart instead
                // of silently reverting to automatic.
                bridge.callAttr("set_link_override", RadioLink.linkOverride(context))
                Log.i(TAG, "rns_bridge.start() returned (background threads running)")
                // Settings sharing over WiFi: listen for hosts and/or host,
                // as last chosen (settings_link.net_start).
                SettingsShareUi.netStart(context.applicationContext)
                // Settings sharing's clock: a change accepted for a set time
                // switches then, while ATAK is in front, which is why it is
                // here and not on a screen. Never ends: this process has no teardown path.
                Thread {
                    while (true) {
                        try {
                            Thread.sleep(SETTINGS_TICK_MS)
                            SettingsShareUi.tick(context.applicationContext)
                        } catch (e: InterruptedException) {
                            return@Thread
                        } catch (e: Exception) {
                            Log.w(TAG, "settings tick failed", e)
                        }
                    }
                }.apply { name = "settings-tick"; isDaemon = true }.start()
            } catch (e: PyException) {
                Log.e(TAG, "rns_bridge failed to start", e)
            }
        }.start()
    }

    /**
     * Call a function in rns_bridge.py and hand back whatever it returned as
     * a string, or null when Python is not up or the call failed.
     *
     * Callers are UI code, so this never throws: a bridge that is still
     * starting, or has fallen over, must show as "unavailable" rather than as
     * a crash in an app whose job is to keep running.
     *
     * MUST NOT be called on the UI thread -- Chaquopy calls cross into the
     * interpreter and can block on its GIL.
     */
    fun call(method: String, vararg args: String): String? = try {
        if (Python.isStarted()) {
            val bridge = Python.getInstance().getModule("rns_bridge")
            val result =
                if (args.isEmpty()) bridge.callAttr(method)
                else bridge.callAttr(method, *Array<Any>(args.size) { args[it] })
            // Null when Python returned None, which callers treat as failure.
            result?.toString()
        } else {
            null
        }
    } catch (e: Exception) {
        Log.e(TAG, "$method failed", e)
        null
    }

    /**
     * Call a function in a Python module that is NOT rns_bridge, e.g. the
     * Coverage tab's `coverage.compute()`. Same contract as call() -- returns
     * a string or null, never throws, must not run on the UI thread.
     *
     * rns_bridge imports RNS, which is slow to start and only exists to serve
     * the mesh; the coverage engine is pure stdlib and answers without it, so
     * it gets its own door rather than dragging the whole mesh in.
     */
    fun callModule(module: String, method: String, vararg args: String): String? = try {
        if (Python.isStarted()) {
            val m = Python.getInstance().getModule(module)
            val result =
                if (args.isEmpty()) m.callAttr(method)
                else m.callAttr(method, *Array<Any>(args.size) { args[it] })
            result?.toString()
        } else {
            null
        }
    } catch (e: Exception) {
        Log.e(TAG, "$module.$method failed", e)
        null
    }

    /**
     * "Pixel 7", "OnePlus CPH2451" -- recognisable at a glance and available
     * with no configuration. MODEL already contains MANUFACTURER on some
     * devices, so avoid printing it twice.
     */
    private fun deviceName(): String {
        val manufacturer = Build.MANUFACTURER.orEmpty().trim()
        val model = Build.MODEL.orEmpty().trim()
        val name = when {
            model.isEmpty() -> manufacturer
            manufacturer.isEmpty() -> model
            model.startsWith(manufacturer, ignoreCase = true) -> model
            else -> "$manufacturer $model"
        }
        return name.replaceFirstChar { it.uppercase() }.ifEmpty { "Android" }
    }
}
