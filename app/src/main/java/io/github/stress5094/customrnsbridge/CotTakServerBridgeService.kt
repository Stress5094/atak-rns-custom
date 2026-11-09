package io.github.stress5094.customrnsbridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import android.util.Log
import java.io.ByteArrayOutputStream
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.IOException
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.time.Instant
import java.time.format.DateTimeFormatter
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Owns a local TCP "TAK Server" endpoint ATAK connects to (Settings ->
 * Network Preferences -> add a server/streaming connection, host
 * 127.0.0.1, port ATAK_SERVER_PORT, non-SSL TCP), and republishes CoT
 * traffic from it as length-prefixed frames on 127.0.0.1:COT_BRIDGE_PORT
 * for Python -- that internal protocol is unchanged from the previous
 * multicast-based version.
 *
 * Why not multicast: ATAK's local SA multicast (239.2.3.1:6969) only
 * exists when there's an active local IP network (normally Wi-Fi) for the
 * OS to route it over -- with Wi-Fi off, neither ATAK nor this bridge had
 * any interface to send/receive on, regardless of the RNode/LoRa link's
 * own state. Loopback (127.0.0.1) is always up regardless of Wi-Fi/
 * cellular radio state, so making ATAK talk to us as a "server" over TCP
 * loopback removes that dependency entirely.
 *
 * Framing on the loopback socket to Python is unchanged: 4-byte
 * big-endian length prefix, then that many bytes of raw CoT payload.
 * Framing on the ATAK-facing socket is NOT length-prefixed -- ATAK's
 * streaming server protocol sends/expects raw, back-to-back CoT XML
 * documents with no framing of its own, so message boundaries are found
 * by watching for the closing `</event>` tag (CoT events don't nest, so
 * this is unambiguous). This matches how other CoT-over-TCP
 * implementations (e.g. PyTAK) handle the same protocol.
 */
class CotTakServerBridgeService : Service() {

    companion object {
        private const val TAG = "CotTakServerBridge"
        const val COT_BRIDGE_PORT = 7101
        // Deliberately NOT TAK Server's conventional 8087, even though that
        // is what this looked like: ATAK's own native mesh/multicast
        // networking also uses 8087 on a stock setup and binds far more than
        // loopback, so this service holding 127.0.0.1:8087 made ATAK fail to
        // create its own listener ("Failed to create socket to listen on TCP
        // port 8087 - will retry"), which starved ATAK's native path.
        //
        // Also deliberately not 7100/7101/7102: those are this app's OWN
        // internal Kotlin<->Python bridge ports (RadioBridgeService's
        // LORA_BRIDGE_PORT=7100 and FM_BRIDGE_PORT=7102, and this class's
        // COT_BRIDGE_PORT=7101 above). 7102 in particular is the FM audio
        // bridge, so using it here made the two fight for the port on every
        // start -- that service usually won and this one retried forever.
        // 7103 is used by neither ATAK nor any part of this app. The exact
        // number doesn't matter as long as it matches whatever you configure
        // as the server port in ATAK's own connection setup.
        private const val ATAK_SERVER_PORT = 7103
        private const val EVENT_CLOSE_TAG = "</event>"
        private const val NOTIF_CHANNEL_ID = "cot_bridge"
        private const val NOTIF_ID = 2
        // ATAK's "Data Reception Timeout" fires when a streaming server
        // connection goes idle for too long with no incoming data --
        // commonly reported around ~30s. Comfortably under that, so a
        // quiet stretch with no real CoT to relay never looks idle to ATAK.
        private const val KEEPALIVE_INTERVAL_SECONDS = 10L

        // How often to ask Python whether the self-GPS-race flag (see
        // checkSelfPositionPending()'s doc comment) is stuck, and, while it
        // is, the minimum gap between reconnect attempts. Different
        // numbers on purpose: checking is nearly free, so polling faster
        // than the reconnect itself is worth catching a fix landing sooner
        // (see the plain 5s POLL loop); the reconnect itself must stay
        // spaced out, since forcing it faster than ATAK can plausibly
        // reconnect (see forceAtakReconnect()'s comment, ~9s measured on
        // this bench) would just mean the socket that just reopened gets
        // slammed shut again before it could have carried anything.
        private const val SELF_POSITION_POLL_SECONDS = 5L
        private const val ATAK_RECONNECT_COOLDOWN_MS = 25_000L
    }

    // Written by the executor thread that binds the listener, read by
    // onDestroy() on the main thread to unblock its accept() loop -- needs
    // the same @Volatile as atakSocket/pythonSocket below, or onDestroy()
    // can observe a stale null and skip the close(), leaking the accept
    // thread and leaving the port permanently bound.
    @Volatile
    private var atakServerSocket: ServerSocket? = null

    // Written by the accept loop, read by the pump thread and the scheduled
    // keepalive -- three different threads, so the reference has to be
    // volatile or a thread can keep using a socket that was replaced or
    // retired minutes ago.
    @Volatile
    private var atakSocket: Socket? = null

    @Volatile
    private var pythonServerSocket: ServerSocket? = null

    @Volatile
    private var pythonSocket: Socket? = null
    private val executor = Executors.newCachedThreadPool()
    private val scheduledExecutor = Executors.newSingleThreadScheduledExecutor()

    // 0 (never attempted) rather than an Optional/nullable: SystemClock's
    // epoch is boot time, always positive, so 0 can never collide with a
    // real attempt and needs no separate "has this ever fired" flag.
    @Volatile
    private var lastAtakReconnectAttemptMs = 0L

    // Set in onDestroy() before the server sockets are closed, and read by
    // bindWithRetry()'s loop and both accept loops. Exists because startAtak
    // Server()/startPythonLoopbackServer() bind on the executor pool, not on
    // the main thread: without a flag, a service destroyed mid-bind would
    // either keep retrying forever or bind a socket nothing owns any more
    // (which is exactly how a restart could leave the port held while the
    // replacement instance could not take it).
    @Volatile
    private var shuttingDown = false

    override fun onCreate() {
        super.onCreate()
        startForegroundWithNotification()
        startAtakServer()
        startPythonLoopbackServer()
        scheduledExecutor.scheduleWithFixedDelay(
            { sendKeepalive() },
            KEEPALIVE_INTERVAL_SECONDS, KEEPALIVE_INTERVAL_SECONDS, TimeUnit.SECONDS
        )
        // Deliberately on THIS service's own schedule, not MainActivity's
        // 2s status poll: MainActivity is paused (its poll loop stopped)
        // for almost this app's entire real operating life, since ATAK --
        // not our own status screen -- is what's normally in the
        // foreground. A recovery mechanism that only ran while an operator
        // happened to have our screen open would essentially never run in
        // the field. This service is what's actually alive the whole time.
        scheduledExecutor.scheduleWithFixedDelay(
            { checkSelfPositionPending() },
            SELF_POSITION_POLL_SECONDS, SELF_POSITION_POLL_SECONDS, TimeUnit.SECONDS
        )
    }

    /**
     * The cold-GPS-race recovery this class exists to drive -- see
     * tx_policy.TxPolicy.self_position_pending()'s own doc comment for the
     * full story: ATAK sends its self-SA CoT once when this connection
     * comes up, and if GPS had not locked at that exact moment, does not
     * appear to try again on its own. `self_position_pending` is cheap for
     * Python to compute (see rns_bridge.self_position_pending(), which
     * this calls the same way MainActivity's callPython() does), so
     * polling it independently of the status screen costs nothing.
     */
    private fun checkSelfPositionPending() {
        val pending = PythonRnsService.call("self_position_pending")
            ?.equals("True", ignoreCase = true) ?: false
        if (!pending) return
        val now = SystemClock.elapsedRealtime()
        if (now - lastAtakReconnectAttemptMs < ATAK_RECONNECT_COOLDOWN_MS) return
        lastAtakReconnectAttemptMs = now
        forceAtakReconnect()
    }

    /**
     * Closes the live ATAK socket, if any, so ATAK notices the pipe is gone
     * and reconnects on its own -- the same path startAtakServer()'s accept
     * loop already handles for every OTHER cause of ATAK dropping this
     * connection. Does nothing (rather than erroring) when there is no
     * socket to close: checkSelfPositionPending()'s own cooldown means this
     * fires rarely, but a socket that closed for some unrelated reason a
     * moment earlier must not turn a no-op into a crash.
     */
    private fun forceAtakReconnect() {
        val socket = atakSocket ?: run {
            Log.i(TAG, "Reconnect requested, but ATAK is not connected right now")
            return
        }
        Log.i(TAG, "Forcing ATAK to reconnect (self position still unknown)")
        try {
            socket.close()
        } catch (e: IOException) {
            // already broken; nothing to salvage
        }
        if (atakSocket === socket) {
            atakSocket = null
        }
    }

    /**
     * A minimal, self-contained CoT event with no real-world meaning --
     * exists purely to put bytes on the wire so ATAK's connection doesn't
     * look idle. type="t-x-c-t" is CoT's generic control/ping convention;
     * ATAK isn't expected to render this on the map. Not verified against
     * a real ATAK "Data Reception Timeout" fix directly -- if the timeout
     * still occurs with this in place, the interval or event shape below
     * are the first things to adjust.
     */
    private fun buildKeepaliveEvent(): ByteArray {
        val now = Instant.now()
        val stale = now.plusSeconds(KEEPALIVE_INTERVAL_SECONDS * 2)
        val fmt = DateTimeFormatter.ISO_INSTANT
        val xml = "<event version=\"2.0\" uid=\"rns-bridge-ping\" type=\"t-x-c-t\" " +
                "time=\"${fmt.format(now)}\" start=\"${fmt.format(now)}\" stale=\"${fmt.format(stale)}\" how=\"m-g\">" +
                "<point lat=\"0.0\" lon=\"0.0\" hae=\"0.0\" ce=\"9999999.0\" le=\"9999999.0\"/>" +
                "</event>"
        return xml.toByteArray(Charsets.UTF_8)
    }

    private fun sendKeepalive() {
        val socket = atakSocket ?: return
        try {
            val payload = buildKeepaliveEvent()
            socket.getOutputStream().write(payload)
            socket.getOutputStream().flush()
            Log.d(TAG, "Sent keepalive to ATAK (${payload.size} bytes)")
        } catch (e: IOException) {
            // A broken pipe here means ATAK has gone away, so retiring the
            // socket keeps the rest of the service from writing into a corpse
            // until the accept loop hands over a live one.
            Log.w(TAG, "Keepalive send failed, retiring ATAK socket", e)
            if (atakSocket === socket) {
                try {
                    socket.close()
                } catch (e2: IOException) {
                    // already broken; nothing to salvage
                }
                atakSocket = null
            }
        }
    }

    private fun startForegroundWithNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                NOTIF_CHANNEL_ID, "CoT bridge", NotificationManager.IMPORTANCE_LOW
            )
            (getSystemService(NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(channel)
        }
        val notif = Notification.Builder(this, NOTIF_CHANNEL_ID)
            .setContentTitle("ATAK-RNS bridge")
            .setContentText("Listening for ATAK server connection")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .build()
        startForeground(NOTIF_ID, notif)
    }

    /**
     * Bind a loopback listening socket, retrying until it succeeds or the
     * service is shutting down.
     *
     * Why not bind once and give up: this service can be torn down and
     * recreated (a package replace, a stopService/startService pair, the OS
     * restarting the foreground service) faster than the previous instance's
     * socket is released. A plain ServerSocket() constructor in that window
     * throws EADDRINUSE -- observed in the field as the app logging
     * "CoT bridge listening on 127.0.0.1:7101" immediately followed by
     * "BindException: bind failed: EADDRINUSE", then serving nothing on the
     * ATAK port for the rest of the process's life, which ATAK saw as its
     * ~25 s "Data Reception Timeout" on every reconnect. Retrying turns that
     * permanent failure into a sub-second delay.
     *
     * SO_REUSEADDR is set explicitly (Java enables it by default for
     * ServerSocket, but this must not silently depend on that) so a listener
     * whose accepted connections are still in TIME_WAIT cannot block the
     * rebind either.
     *
     * Returns null only when shuttingDown is set, so a caller can return
     * without treating a normal shutdown as an error.
     */
    private fun bindWithRetry(port: Int, label: String): ServerSocket? {
        var attempt = 0
        while (!shuttingDown) {
            try {
                val server = ServerSocket()
                server.reuseAddress = true
                server.bind(InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 1)
                return server
            } catch (e: IOException) {
                attempt++
                val delayMs = minOf(500L * attempt, 5000L)
                // First few and then occasional, so a genuinely foreign
                // holder of the port stays visible without flooding logcat.
                if (attempt <= 3 || attempt % 12 == 0) {
                    Log.w(
                        TAG,
                        "$label bind on 127.0.0.1:$port failed (attempt $attempt), " +
                            "retrying in ${delayMs}ms",
                        e
                    )
                }
                try {
                    Thread.sleep(delayMs)
                } catch (interrupted: InterruptedException) {
                    return null
                }
            }
        }
        return null
    }

    /** ATAK connects here as a client, treating this as its configured server. */
    private fun startAtakServer() {
        executor.execute {
            val server = bindWithRetry(ATAK_SERVER_PORT, "ATAK server bridge") ?: return@execute
            atakServerSocket = server
            Log.i(TAG, "ATAK server bridge listening on 127.0.0.1:$ATAK_SERVER_PORT")
            try {
                while (!shuttingDown) {
                    val socket = server.accept()
                    Log.i(TAG, "ATAK connected to server bridge")
                    atakSocket = socket
                    pumpAtakToPython(socket)
                }
            } catch (e: IOException) {
                if (!shuttingDown) Log.e(TAG, "ATAK server bridge error", e)
            } finally {
                // ALWAYS close, even when onDestroy() could not: a shutdown
                // landing between the successful bind above and the first
                // accept() check makes the loop exit immediately, and without
                // this the just-bound listener would leak with nothing left
                // referencing it -- leaving the port permanently un-bindable
                // (EADDRINUSE forever) for the next instance. See
                // bindWithRetry()'s own comment.
                closeQuietly(server)
                if (atakServerSocket === server) {
                    atakServerSocket = null
                }
            }
        }
    }

    /** Local server: Python connects here as a client, same as before. */
    private fun startPythonLoopbackServer() {
        executor.execute {
            val server = bindWithRetry(COT_BRIDGE_PORT, "CoT bridge") ?: return@execute
            pythonServerSocket = server
            Log.i(TAG, "CoT bridge listening on 127.0.0.1:$COT_BRIDGE_PORT")
            try {
                while (!shuttingDown) {
                    val socket = server.accept()
                    Log.i(TAG, "Python connected to CoT bridge")
                    pythonSocket = socket
                    pumpPythonToAtak(socket)
                }
            } catch (e: IOException) {
                if (!shuttingDown) Log.e(TAG, "CoT bridge server error", e)
            } finally {
                closeQuietly(server)
                if (pythonServerSocket === server) {
                    pythonServerSocket = null
                }
            }
        }
    }

    private fun closeQuietly(socket: java.io.Closeable?) {
        try {
            socket?.close()
        } catch (ignored: IOException) {
            // Already closed (e.g. onDestroy() got there first); nothing to do.
        }
    }

    /** ATAK's raw CoT XML stream -> split on </event> -> length-prefixed frame -> Python */
    private fun pumpAtakToPython(socket: Socket) {
        executor.execute {
            val buf = ByteArray(8192)
            val pending = ByteArrayOutputStream()
            try {
                val input = socket.getInputStream()
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    pending.write(buf, 0, n)

                    var text = pending.toString(Charsets.UTF_8.name())
                    var closeIdx = text.indexOf(EVENT_CLOSE_TAG)
                    while (closeIdx >= 0) {
                        val end = closeIdx + EVENT_CLOSE_TAG.length
                        val eventText = text.substring(0, end)
                        val payload = eventText.toByteArray(Charsets.UTF_8)
                        Log.d(TAG, "Captured CoT event, ${payload.size} bytes")

                        val out = pythonSocket?.getOutputStream()
                        if (out != null) {
                            val dos = DataOutputStream(out)
                            dos.writeInt(payload.size)
                            dos.write(payload)
                            dos.flush()
                        } else {
                            Log.w(TAG, "No Python connection to forward ${payload.size} bytes to")
                        }

                        text = text.substring(end)
                        pending.reset()
                        pending.write(text.toByteArray(Charsets.UTF_8))
                        closeIdx = text.indexOf(EVENT_CLOSE_TAG)
                    }
                }
            } catch (e: IOException) {
                Log.w(TAG, "ATAK->Python pump ended", e)
            }
        }
    }

    /** Python (from RNS) -> length-prefixed frame -> raw CoT XML written to ATAK's socket */
    private fun pumpPythonToAtak(socket: Socket) {
        executor.execute {
            try {
                val dis = DataInputStream(socket.getInputStream())
                while (true) {
                    val len = dis.readInt()
                    if (len <= 0 || len > 1_000_000) break
                    val payload = ByteArray(len)
                    dis.readFully(payload)
                    writeToAtak(payload)
                }
            } catch (e: IOException) {
                // Only a failure reading from PYTHON ends this pump. A failure
                // writing to ATAK must not -- see writeToAtak().
                Log.w(TAG, "Python->ATAK pump ended (Python socket closed)", e)
            }
        }
    }

    /**
     * Writing to ATAK has to be able to fail without taking the pump down with
     * it. ATAK drops and re-establishes this connection routinely (its own
     * "Data Reception Timeout"), and it was observed doing so on real hardware
     * -- a `Broken pipe` from sendKeepalive() at 09:46:16, with ATAK
     * reconnecting nine seconds later.
     *
     * Previously the write happened directly inside pumpPythonToAtak's loop,
     * whose try/catch sat OUTSIDE the while -- so one broken pipe exited the
     * whole thread. Nothing restarted it: the accept loop only spawns a new
     * pump when a new PYTHON client connects, and Python's socket was still
     * perfectly healthy. Net effect was that inbound CoT died permanently on
     * the first ATAK reconnect, silently, while transmission kept working --
     * exactly the asymmetry that makes a bridge look half-alive.
     */
    private fun writeToAtak(payload: ByteArray) {
        val target = atakSocket
        if (target == null) {
            Log.w(TAG, "No ATAK connection to re-inject ${payload.size} bytes into")
            return
        }
        try {
            val out = target.getOutputStream()
            out.write(payload)
            out.flush()
            Log.d(TAG, "Re-injected CoT event to ATAK, ${payload.size} bytes")
        } catch (e: IOException) {
            Log.w(TAG, "Write to ATAK failed, dropped ${payload.size} bytes; awaiting reconnect", e)
            // Drop the dead socket so the next event logs honestly rather than
            // retrying a broken pipe. The accept loop installs the replacement.
            if (atakSocket === target) {
                try {
                    target.close()
                } catch (e2: IOException) {
                    // already broken; nothing to salvage
                }
                atakSocket = null
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        // First, so bindWithRetry() and both accept loops stop rather than
        // racing the closes below -- see shuttingDown's own comment.
        shuttingDown = true
        atakServerSocket?.close()
        atakSocket?.close()
        pythonServerSocket?.close()
        pythonSocket?.close()
        executor.shutdownNow()
        scheduledExecutor.shutdownNow()
    }

    override fun onBind(intent: Intent?): IBinder? = null
}
