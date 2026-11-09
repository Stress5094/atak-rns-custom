package io.github.stress5094.customrnsbridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log
import java.io.IOException
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The half of the RNode link that does not care how the radio is attached:
 * a plain byte pipe on 127.0.0.1:bridgePort that Python (via Chaquopy)
 * connects to as a client using
 * `serial.serial_for_url("socket://127.0.0.1:<port>")`.
 *
 * The port is per-subclass (see bridgePort below), not shared, so a LoRa
 * service (USB or BLE) and a second link's service can run concurrently
 * -- two ports, one loopback socket each,
 * rather than the single shared port this class used before that feature
 * existed, which forced the two to be mutually exclusive.
 *
 * This class does NOT speak the RNode/KISS protocol — it moves raw bytes in
 * both directions and lets RNS's RNodeInterface handle framing on the Python
 * side. That is what makes the transport swappable at all: over USB and over
 * Bluetooth the RNode speaks the same bytes, so everything above this line
 * (the codec, the mesh, the acknowledgements, the airtime policy) is unaware
 * of which one is in use.
 *
 * Everything in here was learned on the bench against the USB link and none
 * of it is USB-specific, so a second transport inherits it rather than
 * re-learning it: one Python client at a time, a dead radio must DROP the
 * Python connection rather than go on accepting writes, and nothing on these
 * threads may take the process down with it.
 *
 * Subclasses own the wire itself — opening it, writing to it, and noticing
 * when it dies. They hand bytes arriving from the radio to deliverFromLink()
 * and report a dead radio through handleLinkFailure().
 */
abstract class RadioBridgeService : Service() {

    companion object {
        // Unchanged value, kept for USB/BLE specifically, so a LoRa-only
        // install sees no change at all.
        const val LORA_BRIDGE_PORT = 7100
        // NOT 7101 -- that's rns_bridge.py's unrelated COT_BRIDGE_PORT (the
        // loopback ATAK's own TCP connection lands on), already in use.
        //
        // The FM audio link's own port, matching rns_bridge.py's
        // FM_BRIDGE_PORT. Separate from LORA_BRIDGE_PORT because
        // FmAudioBridgeService runs CONCURRENTLY with the USB/BLE service when
        // FM is enabled as a backup link -- they can no longer share one.
        // Held reserved through the 2026-10-01 teardown so the rebuild could
        // take it back without colliding with anything that claimed it
        // meanwhile.
        const val FM_BRIDGE_PORT = 7102
        private const val NOTIF_CHANNEL_ID = "rnode_bridge"
        private const val NOTIF_ID = 1
        private const val REOPEN_DELAY_S = 3L
        private const val LINK_GRACE_MS = 5_000L
    }

    /** Log tag of the concrete transport, so logcat says which one is talking. */
    protected abstract val tag: String

    /**
     * The loopback port this subclass's service binds. Per-subclass rather
     * than shared, so USB/BLE (LoRa) and a second link can each hold their own
     * concurrently -- see the class doc above.
     */
    protected abstract val bridgePort: Int

    /** Second line of the ongoing notification, e.g. "RNode link active (USB)". */
    protected abstract val linkDescription: String

    /**
     * Open the radio link, or start opening it — a transport that connects
     * asynchronously (Bluetooth) may return long before isLinkOpen() is true.
     *
     * `firstAttempt` is false on the automatic retry path, which runs every
     * few seconds for as long as the radio is missing. Anything that puts a
     * dialog on screen or logs at warning level belongs behind it, or an
     * outage buries itself in its own noise.
     */
    protected abstract fun openLink(firstAttempt: Boolean)

    /** True only when the radio is attached AND writable. */
    protected abstract fun isLinkOpen(): Boolean

    /** Write to the radio. Throws IOException if the write did not happen. */
    @Throws(IOException::class)
    protected abstract fun writeToLink(data: ByteArray)

    /** Release the radio side after a failure. Must be idempotent. */
    protected abstract fun closeLink()

    /** Transport setup that has to happen before the first openLink(). */
    protected open fun prepareLink() {}

    /** Final teardown: receivers, handles, anything closeLink() leaves alone. */
    protected open fun releaseLink() {}

    /** A new Python connection replaced the previous one. A transport that
     * keeps parsing state about Python's byte stream drops it here. */
    protected open fun onPythonConnected() {}

    private var serverSocket: ServerSocket? = null
    @Volatile private var clientSocket: Socket? = null
    protected val ioExecutor = Executors.newCachedThreadPool()
    private val reopenExecutor = Executors.newSingleThreadScheduledExecutor()
    private val reopenPending = AtomicBoolean(false)

    // Set the instant a live link is lost, cleared the instant one comes back
    // -- however it comes back. openLink() is called from four places (this
    // class's own start()/retryOpen(), plus a subclass's own event-driven
    // reattach handling), so the "back up" check lives in attemptOpen()
    // below, wrapping every call site, rather than only in retryOpen() --
    // logging it in just one place would silently miss a reattach broadcast
    // finding the radio first.
    @Volatile private var linkDownSince: Long? = null

    final override fun onCreate() {
        super.onCreate()
        prepareLink()
        startForegroundWithNotification()
        startLoopbackServer()
        attemptOpen(firstAttempt = true)
        // Mirrors retryOpen()'s own "still not open? schedule another look"
        // check. Without this, a radio that is not yet present at startup
        // sat with NO retry scheduled at all until something else (a write
        // attempt from Python) incidentally noticed via pumpSocketToLink's
        // own check -- on a link that sends nothing of its own until RNS
        // has real traffic queued (FM was one), that could be up to a
        // beacon interval away. See the USB
        // attach/detach handling for the event-driven half of this fix.
        if (!isLinkOpen()) scheduleReopen()
    }

    private fun startForegroundWithNotification() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                NOTIF_CHANNEL_ID, "RNode bridge", NotificationManager.IMPORTANCE_LOW
            )
            (getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager)
                .createNotificationChannel(channel)
        }
        val notif = Notification.Builder(this, NOTIF_CHANNEL_ID)
            .setContentTitle("ATAK-RNS bridge")
            .setContentText(linkDescription)
            .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
            .build()
        startForeground(NOTIF_ID, notif)
    }

    /** Local server: Python connects here as a client (socket:// url on the Python side). */
    private fun startLoopbackServer() {
        ioExecutor.execute {
            try {
                // Reuse rather than wait out TIME_WAIT: switching transports
                // stops one bridge service and starts another on the same
                // port, and a bind that fails there leaves an app with no way
                // back to the radio at all.
                serverSocket = ServerSocket().apply {
                    reuseAddress = true
                    bind(java.net.InetSocketAddress(InetAddress.getByName("127.0.0.1"), bridgePort), 1)
                }
                Log.i(tag, "RNode bridge listening on 127.0.0.1:$bridgePort")
                while (true) {
                    val socket = serverSocket!!.accept()
                    Log.i(tag, "Python connected to RNode bridge")
                    // Close the previous connection (if any) before adopting
                    // the new one -- otherwise the old pumpSocketToLink
                    // thread keeps running indefinitely, since nothing forces
                    // its blocking read() to return until its socket errors
                    // out on its own. Two such threads can end up writing to
                    // the same radio concurrently, corrupting the KISS byte
                    // stream on every reconnect.
                    //
                    // Adopt the new socket BEFORE closing the old one. The old
                    // pump wakes from its blocking read the instant we close
                    // it, and anything keyed on "is this still the current
                    // socket?" must already see the answer as no.
                    val previous = clientSocket
                    clientSocket = socket
                    try {
                        previous?.close()
                    } catch (e: IOException) {
                        // ignore -- already closed/broken, which is fine
                    }
                    onPythonConnected()
                    pumpSocketToLink(socket)
                }
            } catch (e: IOException) {
                Log.e(tag, "RNode bridge server error", e)
            } catch (e: Throwable) {
                // Same reason as in pumpSocketToLink: an uncaught throw on
                // this executor takes the process down, and this accept loop
                // runs for the life of the service.
                Log.e(tag, "RNode bridge server failed unexpectedly", e)
            }
        }
    }

    /** Radio -> socket -> Python. Called by the subclass for every inbound chunk. */
    protected fun deliverFromLink(data: ByteArray) {
        try {
            clientSocket?.getOutputStream()?.write(data)
        } catch (e: IOException) {
            // Not a link failure: Python may simply not be connected yet, or
            // may be mid-reconnect. The radio is fine.
            Log.w(tag, "Socket write failed (Python not connected yet?)", e)
        }
    }

    /**
     * Retire the Python connection and leave the radio alone.
     *
     * The distinction matters for a transport that takes time to come up.
     * Bluetooth spends a second or two connecting, and Python — which polls
     * for this socket the moment the service starts — will get here first and
     * try to write before there is anywhere for the bytes to go. Refusing that
     * write is right; tearing down the half-finished connection underneath it
     * is not, and the two together livelock: each end restarts the other just
     * as it is about to succeed. Down a cable the port is open before Python
     * can ask, which is why this only ever mattered once there was a second
     * kind of wire.
     */
    private fun dropPythonConnection() {
        try { clientSocket?.close() } catch (e: IOException) { /* already broken */ }
        clientSocket = null
    }

    /**
     * The RNode link is gone. Tear it down and, crucially, DROP THE PYTHON
     * CONNECTION.
     *
     * Found on the bench 2026-07-31: without this, a USB failure was logged
     * and otherwise ignored. The TCP socket to Python stayed open, so RNS went
     * on writing into a socket nobody was draining — `txb` climbed, every
     * beacon logged as sent, and the interface reported `online=True` for four
     * minutes while the radio was physically gone. A dead radio that says it
     * is dead is diagnosable; one that reports healthy poisons every other
     * measurement on the device.
     *
     * Closing the socket is all the signalling that is needed. RNS's
     * RNodeInterface sets `online = False` on the resulting read error and
     * enters `reconnect_port()`, and `_build_rnode_interface()` in
     * rns_bridge.py already overrides `open_port()` to establish a fresh
     * connection on every retry.
     *
     * Idempotent: the read path, the write pump and the transport's own
     * "device went away" broadcast can all notice the same death within
     * milliseconds of each other.
     */
    @Synchronized
    protected fun handleLinkFailure(reason: String) {
        val hadLink = isLinkOpen()

        closeLink()
        dropPythonConnection()

        if (hadLink) {
            Log.e(tag, "RNode link down ($reason) — dropped Python connection so RNS reconnects")
            // Guarded rather than unconditionally (re)set: handleLinkFailure
            // can fire more than once for the same outage (this service and
            // RNS are both noticing independently), and only the FIRST one
            // should mark when the downtime actually started.
            linkDownSince = linkDownSince ?: System.currentTimeMillis()
        } else {
            // Still worth a line, quietly. On a transport that fails while
            // connecting rather than while working, this is the only place the
            // reason is ever stated — and "could not find the RNode serial
            // service" is a different afternoon from "out of range".
            Log.i(tag, "RNode link still down ($reason)")
        }
        scheduleReopen()
    }

    /**
     * At most one retry chain at a time. Both this service and RNS are
     * retrying independently — a failed Python reconnect lands back in
     * handleLinkFailure — so without the guard every crossed attempt would
     * fork another chain.
     */
    protected fun scheduleReopen() {
        if (!reopenPending.compareAndSet(false, true)) return
        try {
            reopenExecutor.schedule({
                reopenPending.set(false)
                retryOpen()
            }, REOPEN_DELAY_S, TimeUnit.SECONDS)
        } catch (e: RejectedExecutionException) {
            // The service is already destroyed and a reader thread reported
            // the link failing after it. MEASURED 2026-10-16 (OnePlus): a USB
            // board unplugged just after a switch to Bluetooth did exactly
            // this, on the serial library's thread, and took the app down.
            Log.i(tag, "Link failure after the service stopped; not reopening")
        }
    }

    /**
     * Wraps every openLink() call site so "the link came back" is logged
     * exactly once no matter which of the four call sites actually found it
     * -- a scheduled retry here, or a subclass's own event-driven reattach
     * handling. Logging only inside retryOpen()
     * would silently miss the reattach path, which exists specifically to
     * notice a reconnect faster than the retry schedule would.
     */
    protected fun attemptOpen(firstAttempt: Boolean) {
        openLink(firstAttempt)
        val since = linkDownSince
        if (isLinkOpen() && since != null) {
            val downtimeS = (System.currentTimeMillis() - since) / 1000
            Log.i(tag, "RNode link back up after ${downtimeS}s downtime")
            linkDownSince = null
        }
    }

    private fun retryOpen() {
        if (isLinkOpen()) return  // recovered, possibly via a reattach broadcast
        try {
            attemptOpen(firstAttempt = false)
        } catch (e: Exception) {
            Log.w(tag, "RNode reopen attempt failed", e)
        }
        if (!isLinkOpen()) scheduleReopen()
    }

    /**
     * Python -> socket -> radio.
     *
     * The two failure directions mean opposite things and must not be caught
     * together. A read failure is Python's end going away — a reconnect, or
     * this service retiring the socket on purpose — and says nothing about the
     * radio. A write failure is the radio.
     */
    /**
     * Python's first bytes can arrive just before the link does: at startup
     * both race, and a BLE link takes a second or two to come up. MEASURED
     * 2026-10-16 (OnePlus, Meshtastic over BLE): Python connected 0.7 s before
     * the link opened, was dropped, and came online 15 s later than it could
     * have. So a short wait first; a link that is really down still gets
     * dropped, just LINK_GRACE_MS later. True if the link opened while this
     * socket was still the current one.
     */
    private fun awaitLinkOpen(socket: Socket): Boolean {
        val end = System.currentTimeMillis() + LINK_GRACE_MS
        while (System.currentTimeMillis() < end) {
            if (clientSocket !== socket || socket.isClosed) return false
            if (isLinkOpen()) {
                Log.i(tag, "RNode link opened while Python waited; keeping the connection")
                return true
            }
            try { Thread.sleep(100) } catch (e: InterruptedException) { return false }
        }
        return isLinkOpen()
    }

    private fun pumpSocketToLink(socket: Socket) {
        ioExecutor.execute {
            val buf = ByteArray(4096)
            try {
                val input = socket.getInputStream()
                while (true) {
                    val n = try {
                        input.read(buf)
                    } catch (e: IOException) {
                        Log.i(tag, "Python end of RNode bridge closed: ${e.message}")
                        break
                    }
                    if (n < 0) break

                    // Deliberately not a silent no-op when the link is down: a
                    // dropped write is the same lie in a new place — Python
                    // reconnects, writes are accepted, nothing reaches the air.
                    // Refusing keeps RNS honest until the radio is genuinely
                    // back.
                    //
                    // Checked rather than thrown-and-caught: during an outage
                    // RNS reconnects every few seconds and each attempt lands
                    // here, so an exception with a stack trace per attempt
                    // buries the outage in its own noise.
                    if (!isLinkOpen() && !awaitLinkOpen(socket)) {
                        Log.i(tag, "RNode link is not open; dropping this Python connection")
                        dropPythonConnection()
                        scheduleReopen()
                        break
                    }
                    try {
                        writeToLink(buf.copyOf(n))
                    } catch (e: IOException) {
                        Log.w(tag, "Radio write failed", e)
                        handleLinkFailure("radio write failed: ${e.message}")
                        break
                    }
                }
            } catch (e: IOException) {
                // getInputStream() itself, which the loop's own catch cannot
                // reach. RNS reconnects every few seconds during an outage and
                // this service retires the previous socket when it does, so a
                // socket can be closed between being handed to this task and
                // the task starting -- and then this is the FIRST thing that
                // touches it.
                //
                // Until this was caught, the block had a finally and no catch:
                // the exception left the lambda, reached the executor's
                // uncaught handler, and Android killed the whole process. That
                // is the crash observed on 2026-08-04, three times, during a
                // USB reconnect storm. Same meaning as a read failure -- the
                // Python end went away -- so the same quiet handling.
                Log.i(tag, "Python end of RNode bridge closed before reading: ${e.message}")
            } catch (e: Throwable) {
                // Nothing on this executor may take the process down with it.
                // This service exists to keep running through exactly the
                // conditions that produce surprises here.
                Log.e(tag, "RNode bridge pump failed unexpectedly", e)
            } finally {
                try { socket.close() } catch (e: IOException) { /* already closed */ }
            }
        }
    }

    final override fun onDestroy() {
        super.onDestroy()
        reopenExecutor.shutdownNow()
        releaseLink()
        serverSocket?.close()
        clientSocket?.close()
        ioExecutor.shutdownNow()
    }

    final override fun onBind(intent: Intent?): IBinder? = null
}
