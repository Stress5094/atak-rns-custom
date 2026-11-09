package io.github.stress5094.customrnsbridge

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothStatusCodes
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.core.content.ContextCompat
import java.io.IOException
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The same byte pipe as UsbSerialBridgeService, over Bluetooth Low Energy
 * instead of a cable.
 *
 * The RNode speaks the identical KISS byte stream either way — BLE only
 * replaces the wire — so everything above this (the codec, the mesh, the
 * acknowledgements, the airtime policy, and the whole Python side) is
 * unchanged and unaware. What arrives here in notifications goes into the
 * same loopback socket the cable fed.
 *
 * WHY BLE AND NOT THE SIMPLER KIND OF BLUETOOTH. The classic "wireless serial
 * cable" profile (RFCOMM/SPP) is about ten lines on Android, and Reticulum's
 * own Android interface supports both. It is not an option on the boards this
 * runs on: the Heltec LoRa32 v3 is an ESP32-S3, and that chip has no classic
 * Bluetooth radio at all. BLE is the only Bluetooth those boards have.
 *
 * The transport used here is the Nordic UART Service, which is what the RNode
 * firmware exposes and what Reticulum's own BLE code talks to — one
 * characteristic for each direction, notifications inbound, writes outbound.
 *
 * WHY THE SERVICE ONLY EVER CONNECTS, NEVER SCANS. Picking a radio is a
 * decision with consequences — connect to the wrong RNode and you are on
 * somebody else's link — so it is made once, by a person, in the picker on the
 * status screen, and stored. This service either connects to what was chosen
 * or, if nothing was, to the sole paired RNode. Two paired RNodes and no
 * choice saved is not a coin toss: it waits and says so. That also keeps the
 * scan permission out of the background service entirely.
 *
 * A MESHTASTIC BOARD IS NOT A SERIAL PIPE OVER BLUETOOTH, and that is the one
 * place this class does more than move bytes. Its GATT service has a ToRadio
 * characteristic that takes one whole protobuf per write, a FromRadio
 * characteristic that hands back one protobuf per READ (empty when there is
 * nothing waiting), and a FromNum characteristic that notifies when something
 * new is waiting. There is no stream and no notification carrying data. So in
 * Meshtastic mode writes are cut into whole messages and reads are polled on a
 * FromNum nudge, and [MeshtasticBleFraming] converts both to the framing the
 * board uses down a cable -- which keeps Python, the one place the Meshtastic
 * protocol is spoken, unaware of which wire it is on. Which mode is fixed for
 * the life of the service: a firmware change is a restart (RadioLink).
 */
class BleSerialBridgeService : RadioBridgeService() {

    companion object {
        private const val TAG = "BleSerialBridge"

        // Nordic UART Service. RX is what the phone writes to; TX is what the
        // RNode notifies on. Named from the RADIO's point of view, which is
        // the opposite of how they read from here -- worth stating, because
        // swapping them produces a connection that looks perfect and moves no
        // bytes.
        val UART_SERVICE: UUID = UUID.fromString("6e400001-b5a3-f393-e0a9-e50e24dcca9e")
        private val UART_RX_CHAR: UUID = UUID.fromString("6e400002-b5a3-f393-e0a9-e50e24dcca9e")
        private val UART_TX_CHAR: UUID = UUID.fromString("6e400003-b5a3-f393-e0a9-e50e24dcca9e")
        private val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

        // Meshtastic's API service, as the official Python package and the
        // Android app use it. FromRadio is READ, not notified; FromNum is the
        // doorbell that says a read is worth doing.
        val MESH_SERVICE: UUID = UUID.fromString("6ba1b218-15a8-461f-9fa8-5dcae273eafd")
        private val MESH_TORADIO: UUID = UUID.fromString("f75c76d2-129e-4dad-a1dd-7866124401e7")
        private val MESH_FROMRADIO: UUID = UUID.fromString("2c55e69e-4993-11ed-b878-0242ac120002")
        private val MESH_FROMNUM: UUID = UUID.fromString("ed9da18c-a800-4f66-a670-aa7547e34453")

        // An empty FromRadio means "nothing yet", and right after a write the
        // answer is often a few tens of milliseconds behind it. The package
        // retries five empty reads at 0.1 s before giving up; three is enough
        // here because FromNum calls the next drain anyway.
        private const val EMPTY_READS_BEFORE_IDLE = 3
        private const val EMPTY_READ_PAUSE_MS = 100L
        private const val DIAG_FIRST_N = 12
        private const val MESH_DISCOVERY_DELAY_MS = 600L

        /** RNode firmware advertises itself as "RNode " plus a short id. */
        const val RNODE_NAME_PREFIX = "RNode "

        private const val TARGET_MTU = 512
        private const val MAX_ATTR_LEN = 512

        // Every BLE connection starts at the 23-byte default until an MTU
        // exchange succeeds, which leaves 20 bytes of payload per write.
        private const val BASE_CHUNK = 20

        // A write that has not completed in this long is not slow, it is gone.
        // A healthy one completes inside a connection interval or two, tens of
        // milliseconds.
        private const val WRITE_TIMEOUT_MS = 5000L

        // Android's own connect attempt can sit for half a minute before
        // reporting anything when the radio is simply out of range. This
        // watchdog gives up sooner so the service's normal retry can run --
        // without it, a radio switched off at the wrong moment leaves the
        // bridge waiting on a callback that may never come.
        private const val CONNECT_TIMEOUT_MS = 20_000L
        private const val PAIRING_WAIT_MS = 5_000L
        private const val MTU_REPLY_WAIT_MS = 3_000L
    }

    override val tag = TAG
    override val linkDescription
        get() = if (meshtastic) "Meshtastic link active (Bluetooth)" else "RNode link active (Bluetooth)"
    override val bridgePort = LORA_BRIDGE_PORT

    private var adapter: BluetoothAdapter? = null
    private var gatt: BluetoothGatt? = null
    private var rxChar: BluetoothGattCharacteristic? = null
    private var txChar: BluetoothGattCharacteristic? = null

    /** Meshtastic board rather than RNode; read once, in prepareLink(). */
    private var meshtastic = false
    private var fromRadioChar: BluetoothGattCharacteristic? = null
    private val deframer = MeshtasticBleFraming.ToRadioDeframer()
    private var drainExecutor: ExecutorService? = null
    private val drainPending = AtomicBoolean(false)

    // The first few messages each way after every link open are logged with
    // their size and leading bytes: when a board connects and Python still
    // times out, this is what says which direction stopped.
    private val diagWrites = java.util.concurrent.atomic.AtomicInteger(0)
    private val diagReads = java.util.concurrent.atomic.AtomicInteger(0)

    private fun diag(counter: java.util.concurrent.atomic.AtomicInteger, what: String, b: ByteArray) {
        if (counter.incrementAndGet() > DIAG_FIRST_N) return
        val head = b.take(12).joinToString("") { "%02x".format(it) }
        Log.i(TAG, "[mt-ble] $what #${counter.get()} ${b.size} B $head")
    }

    /** Handed the status and value of the last FromRadio read. */
    private class ReadResult(val status: Int, val value: ByteArray)
    private val readResult = ArrayBlockingQueue<ReadResult>(1)

    @Volatile private var linkOpen = false
    // Set while OUR MTU request is outstanding: only its reply opens the link.
    @Volatile private var awaitingMtu = false
    @Volatile private var chunkSize = BASE_CHUNK
    private val connecting = AtomicBoolean(false)

    /** Handed the status of the last write by the GATT callback. */
    private val writeResult = ArrayBlockingQueue<Int>(1)

    /** Only one GATT operation may be outstanding at a time -- reads included,
     * which only the Meshtastic mode does. */
    private val writeMutex = Any()

    private val handler = Handler(Looper.getMainLooper())

    /**
     * Bluetooth being switched off is this transport's version of the cable
     * being pulled, and gets the same treatment for the same reason: a bridge
     * that keeps reporting healthy after the radio is gone poisons every
     * measurement taken while it lies.
     */
    private val adapterStateReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != BluetoothAdapter.ACTION_STATE_CHANGED) return
            when (intent.getIntExtra(BluetoothAdapter.EXTRA_STATE, BluetoothAdapter.ERROR)) {
                BluetoothAdapter.STATE_TURNING_OFF, BluetoothAdapter.STATE_OFF ->
                    handleLinkFailure("Bluetooth turned off")
                BluetoothAdapter.STATE_ON -> {
                    Log.i(TAG, "Bluetooth back on, retrying the RNode connection")
                    scheduleReopen()
                }
            }
        }
    }

    override fun prepareLink() {
        meshtastic = RadioLink.isMeshtastic(this)
        if (meshtastic) drainExecutor = Executors.newSingleThreadExecutor()
        adapter = (getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter
        registerReceiver(
            adapterStateReceiver,
            IntentFilter(BluetoothAdapter.ACTION_STATE_CHANGED),
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU)
                Context.RECEIVER_NOT_EXPORTED else 0
        )
    }

    override fun isLinkOpen() = linkOpen

    @SuppressLint("MissingPermission") // checked in hasConnectPermission()
    override fun openLink(firstAttempt: Boolean) {
        if (linkOpen || connecting.get()) return

        val adapter = this.adapter
        if (adapter == null) {
            Log.e(TAG, "This device has no Bluetooth adapter")
            return
        }
        // Not fatal, and deliberately not a dead end: permission can be
        // granted while this service is running, and the retry that follows
        // will simply succeed. The service cannot ask for it itself -- only an
        // Activity can -- so MainActivity requests it on the way in.
        if (!hasConnectPermission()) {
            if (firstAttempt) Log.w(TAG, "No Bluetooth connect permission yet; waiting for it")
            return
        }
        if (!adapter.isEnabled) {
            if (firstAttempt) Log.w(TAG, "Bluetooth is switched off")
            return
        }

        val device = chooseDevice(adapter, firstAttempt) ?: return
        if (device.bondState == BluetoothDevice.BOND_BONDING) {
            // Pairing runs over the same radio link as this connection, so an
            // attempt of ours that times out and closes takes the pairing down
            // with it. MEASURED 2026-10-16 (OnePlus, RAK4631): createBond, then
            // our 20 s timeout fired 3 s later and the board never got as far
            // as asking for the PIN -- no prompt, bond back to NONE. The retry
            // loop comes back here every few seconds until pairing finishes.
            Log.i(TAG, "${radioKind()} ${device.address} is pairing; connecting after it finishes")
            return
        }
        if (device.bondState == BluetoothDevice.BOND_NONE) {
            // Not refused: some RNode builds expose the UART service without
            // requiring encryption, and those work unpaired. The ones that
            // don't fail at the first read with an authentication error, which
            // is far less obvious than this line.
            Log.w(TAG, "${radioKind()} ${device.address} is not paired; if it never connects, pair it from the radio link screen")
        }

        connecting.set(true)
        Log.i(TAG, "Connecting to ${radioKind()} ${device.address}")
        gatt = device.connectGatt(this, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        val attempt = gatt
        handler.postDelayed({ checkConnectTimeout(device, attempt) }, CONNECT_TIMEOUT_MS)
    }

    /** Gives up on a connection attempt -- unless a pairing started meanwhile
     * (from the radio link screen, or Android's own prompt) and is using this
     * link: closing it then cancels the pairing (see openLink). */
    @SuppressLint("MissingPermission")
    private fun checkConnectTimeout(device: BluetoothDevice, attempt: BluetoothGatt?) {
        if (gatt !== attempt || !connecting.get() || linkOpen) return
        if (device.bondState == BluetoothDevice.BOND_BONDING) {
            Log.i(TAG, "BLE connection attempt waiting for pairing to finish")
            handler.postDelayed({ checkConnectTimeout(device, attempt) }, PAIRING_WAIT_MS)
            return
        }
        Log.w(TAG, "BLE connection attempt timed out")
        handleLinkFailure("BLE connect timed out")
    }

    /**
     * The radio this node is meant to be talking to: the one chosen in the
     * picker, or — for a device that has only ever been paired with one RNode
     * — that one, so the common case needs no configuration at all.
     */
    @SuppressLint("MissingPermission") // caller checked hasConnectPermission()
    private fun chooseDevice(adapter: BluetoothAdapter, firstAttempt: Boolean): BluetoothDevice? {
        val saved = RadioLink.bleAddress(this)
        if (saved != null) {
            return try {
                adapter.getRemoteDevice(saved)
            } catch (e: IllegalArgumentException) {
                Log.e(TAG, "Saved ${radioKind()} address $saved is not a valid Bluetooth address", e)
                null
            }
        }

        // No "sole paired board" fallback for Meshtastic: its boards advertise
        // under any name the owner gave them, so a bonded list cannot be
        // filtered down to boards reliably, and guessing would pick headphones.
        if (meshtastic) {
            if (firstAttempt) Log.w(TAG, "No Meshtastic board chosen — pick one on the radio link screen")
            return null
        }

        val paired = adapter.bondedDevices.orEmpty().filter { isRNode(it) }
        return when (paired.size) {
            1 -> paired.first()
            0 -> {
                if (firstAttempt) Log.w(TAG, "No paired RNode found — pair one from the radio link screen")
                null
            }
            else -> {
                // Guessing here would be the worst kind of bug to debug: it
                // works, on the wrong radio.
                if (firstAttempt) Log.w(
                    TAG,
                    "${paired.size} paired RNodes and none chosen — pick one on the radio link screen"
                )
                null
            }
        }
    }

    override fun writeToLink(data: ByteArray) {
        val gatt = this.gatt ?: throw IOException("BLE link is not open")
        val characteristic = rxChar ?: throw IOException("BLE link has no write characteristic")
        if (meshtastic) {
            writeMeshtastic(gatt, characteristic, data)
            return
        }

        // Blocking, one chunk at a time, because that is exactly the
        // backpressure the cable gave for free: the pump thread must not run
        // ahead of what the radio has actually taken. GATT allows one
        // outstanding operation per connection, so this is also the only
        // correct way to do it.
        synchronized(writeMutex) {
            var offset = 0
            while (offset < data.size) {
                val n = minOf(chunkSize, data.size - offset)
                val chunk = data.copyOfRange(offset, offset + n)
                writeResult.clear()
                if (!submitWrite(gatt, characteristic, chunk)) {
                    throw IOException("BLE stack refused the write")
                }
                val status = writeResult.poll(WRITE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                    ?: throw IOException("BLE write timed out after ${WRITE_TIMEOUT_MS}ms")
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    throw IOException("BLE write failed with status $status")
                }
                offset += n
            }
        }
    }

    /**
     * One GATT write per ToRadio, whole. Unlike the RNode's byte stream, a
     * message split across writes is two broken protobufs, so it must fit the
     * negotiated MTU; Android falls back to a long (prepared) write above that,
     * which the firmware is not known to accept, hence the warning.
     */
    private fun writeMeshtastic(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        data: ByteArray,
    ) {
        val messages = synchronized(deframer) { deframer.feed(data) }
        if (messages.isEmpty()) Log.i(TAG, "[mt-ble] ${data.size} B from Python held, no whole ToRadio yet (skipped ${deframer.skipped})")
        for (message in messages) {
            if (message.isEmpty()) continue
            if (message.size > chunkSize) {
                Log.w(TAG, "ToRadio of ${message.size} B exceeds the $chunkSize B MTU payload; sending as a long write")
            }
            diag(diagWrites, "ToRadio", message)
            synchronized(writeMutex) {
                writeResult.clear()
                if (!submitWrite(gatt, characteristic, message)) {
                    throw IOException("BLE stack refused the write")
                }
                val status = writeResult.poll(WRITE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                    ?: throw IOException("BLE write timed out after ${WRITE_TIMEOUT_MS}ms")
                if (status != BluetoothGatt.GATT_SUCCESS) {
                    throw IOException("BLE write failed with status $status")
                }
            }
        }
        // Most writes are questions (want_config, an admin read, a heartbeat),
        // and the board may answer without ringing FromNum first.
        if (messages.isNotEmpty()) requestDrain()
    }

    /**
     * Read FromRadio until it comes back empty, handing each message to Python
     * in stream framing. One drain at a time, on its own thread, so the
     * Bluetooth callback thread is never blocked on a read; a nudge that
     * arrives mid-drain queues exactly one more pass, so nothing rung while a
     * pass was finishing is missed.
     */
    private fun requestDrain() {
        val executor = drainExecutor ?: return
        if (!drainPending.compareAndSet(false, true)) return
        try {
            executor.execute {
                drainPending.set(false)
                try {
                    drainFromRadio()
                } catch (e: IOException) {
                    handleLinkFailure("FromRadio read failed: ${e.message}")
                } catch (e: Throwable) {
                    // Same rule as the bridge's own threads: nothing here may
                    // take the process down.
                    Log.e(TAG, "FromRadio drain failed unexpectedly", e)
                }
            }
        } catch (e: java.util.concurrent.RejectedExecutionException) {
            drainPending.set(false) // service is shutting down
        }
    }

    private fun drainFromRadio() {
        var empty = 0
        var count = 0
        var bytes = 0
        var configComplete = false
        var reads = 0
        var readingNs = 0L
        val started = System.nanoTime()
        try {
            drainLoop@ while (linkOpen) {
            val readStart = System.nanoTime()
            val message = readFromRadio() ?: return
            readingNs += System.nanoTime() - readStart
            reads++
            diag(diagReads, "FromRadio", message)
            if (message.isEmpty()) {
                if (++empty >= EMPTY_READS_BEFORE_IDLE) break@drainLoop
                Thread.sleep(EMPTY_READ_PAUSE_MS)
                continue
            }
            empty = 0
            count++
            bytes += message.size
            // FromRadio field 7, config_complete_id: the end of the setup
            // download Python waits for at every connect.
            if (message[0] == 0x38.toByte()) configComplete = true
            if (message.size > MeshtasticBleFraming.MAX_MESSAGE) {
                Log.w(TAG, "Dropped a FromRadio of ${message.size} B, over the stream's limit")
                continue
            }
            deliverFromLink(MeshtasticBleFraming.wrap(message))
            }
        } finally {
            // One line per drain that moved anything: how long a setup download
            // takes over the air link is what decides whether Python's connect
            // timeout is long enough.
            //
            // "in" is WALL time, and a drain keeps going while messages keep
            // arriving, so under steady traffic it is the ARRIVAL spacing, not
            // the read cost -- misread exactly that way on 2026-10-16. "reading"
            // is the time actually spent in GATT reads (empty ones included);
            // reading / wall near 1 is a link that cannot keep up.
            if (count > 0) Log.i(TAG, "[mt-ble] drained $count FromRadio, $bytes B in " +
                    "${(System.nanoTime() - started) / 1_000_000} ms " +
                    "(reading ${readingNs / 1_000_000} ms over $reads reads)" +
                    if (configComplete) ", config complete" else "")
        }
    }

    /** One FromRadio, or null when the link closed underneath the read. */
    @SuppressLint("MissingPermission") // the link cannot be open without it
    private fun readFromRadio(): ByteArray? {
        val gatt = this.gatt ?: return null
        val characteristic = fromRadioChar ?: return null
        synchronized(writeMutex) {
            readResult.clear()
            if (!gatt.readCharacteristic(characteristic)) {
                throw IOException("BLE stack refused the FromRadio read")
            }
            val result = readResult.poll(WRITE_TIMEOUT_MS, TimeUnit.MILLISECONDS)
                ?: throw IOException("FromRadio read timed out after ${WRITE_TIMEOUT_MS}ms")
            if (!linkOpen) return null
            if (result.status != BluetoothGatt.GATT_SUCCESS) {
                // 5/15 are insufficient authentication/encryption: an unpaired
                // board, which is the likeliest first-time failure.
                throw IOException("FromRadio read failed with status ${result.status}" +
                        if (result.status == 5 || result.status == 15) " (not paired?)" else "")
            }
            return result.value
        }
    }

    @SuppressLint("MissingPermission") // the link cannot be open without it
    private fun submitWrite(
        gatt: BluetoothGatt,
        characteristic: BluetoothGattCharacteristic,
        chunk: ByteArray,
    ): Boolean {
        val writeType =
            if (characteristic.properties and BluetoothGattCharacteristic.PROPERTY_WRITE != 0)
                BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
            else
                BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE

        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            gatt.writeCharacteristic(characteristic, chunk, writeType) ==
                    BluetoothStatusCodes.SUCCESS
        } else {
            @Suppress("DEPRECATION")
            run {
                characteristic.writeType = writeType
                characteristic.value = chunk
                gatt.writeCharacteristic(characteristic)
            }
        }
    }

    @SuppressLint("MissingPermission") // closing needs no permission we did not already have
    override fun closeLink() {
        linkOpen = false
        connecting.set(false)
        awaitingMtu = false
        rxChar = null
        txChar = null
        fromRadioChar = null
        chunkSize = BASE_CHUNK
        synchronized(deframer) { deframer.reset() }

        // Unblock anything waiting on a completion that is never coming.
        writeResult.offer(BluetoothGatt.GATT_FAILURE)
        readResult.offer(ReadResult(BluetoothGatt.GATT_FAILURE, ByteArray(0)))

        val current = gatt
        gatt = null
        try {
            current?.disconnect()
            // close() is not optional and not the same as disconnect():
            // Android hands out a limited number of GATT client slots, and one
            // leaked per failed attempt means an app that reconnects fine for
            // half an hour and then never again.
            current?.close()
        } catch (e: Exception) {
            Log.w(TAG, "Error closing GATT connection", e)
        }
    }

    // A frame Python half-wrote before reconnecting would otherwise swallow
    // the 0xC3 wake-up bytes of the next connection as its payload.
    override fun onPythonConnected() {
        synchronized(deframer) { deframer.reset() }
    }

    override fun releaseLink() {
        closeLink()
        drainExecutor?.shutdownNow()
        handler.removeCallbacksAndMessages(null)
        unregisterReceiver(adapterStateReceiver)
    }

    private val gattCallback = object : BluetoothGattCallback() {

        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (status == BluetoothGatt.GATT_SUCCESS && newState == BluetoothGatt.STATE_CONNECTED) {
                if (!meshtastic) {
                    Log.i(TAG, "BLE connected, discovering services")
                    if (!gatt.discoverServices()) {
                        handleLinkFailure("service discovery could not be started")
                    }
                    return
                }
                // Measured 2026-10-15 on a Tracker L1 after a settings write
                // restarted it: discovery "completed" in 7-34 ms -- Android's
                // cache, not the board -- and the subscribe that followed never
                // completed, four connects running. The one that worked
                // discovered in ~1.1 s. So for a Meshtastic board the cache is
                // dropped and discovery waits out the encryption setup a bonded
                // link does first, as the Meshtastic Android app does.
                val cleared = refreshGattCache(gatt)
                Log.i(TAG, "BLE connected, discovering services (cache cleared: $cleared)")
                handler.postDelayed({
                    if (this@BleSerialBridgeService.gatt === gatt && !linkOpen && !gatt.discoverServices()) {
                        handleLinkFailure("service discovery could not be started")
                    }
                }, MESH_DISCOVERY_DELAY_MS)
            } else if (newState == BluetoothGatt.STATE_DISCONNECTED) {
                // status 133 is Android's catch-all GATT error and normally
                // means the radio was not there to answer.
                handleLinkFailure("BLE disconnected (status $status)")
            }
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                handleLinkFailure("service discovery failed (status $status)")
                return
            }
            if (meshtastic) {
                subscribeMeshtastic(gatt)
                return
            }
            val service = gatt.getService(UART_SERVICE)
            if (service == null) {
                // Connected to something that is not an RNode, or to one whose
                // firmware predates BLE support. Saying which is more useful
                // than retrying in silence.
                handleLinkFailure("connected device does not expose the RNode serial service")
                return
            }
            rxChar = service.getCharacteristic(UART_RX_CHAR)
            txChar = service.getCharacteristic(UART_TX_CHAR)
            val tx = txChar
            if (rxChar == null || tx == null) {
                handleLinkFailure("RNode serial service is missing a characteristic")
                return
            }
            if (!gatt.setCharacteristicNotification(tx, true)) {
                handleLinkFailure("could not subscribe to the RNode's output")
                return
            }
            val cccd = tx.getDescriptor(CCCD)
            if (cccd == null) {
                handleLinkFailure("RNode output characteristic has no notification descriptor")
                return
            }
            // Subscribing is a two-step affair: the local flag above, and this
            // descriptor write that tells the radio to actually send. Skipping
            // the second gives a connection that transmits fine and receives
            // nothing.
            val enable = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            val ok = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                gatt.writeDescriptor(cccd, enable) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                run {
                    cccd.value = enable
                    gatt.writeDescriptor(cccd)
                }
            }
            if (!ok) handleLinkFailure("could not enable notifications")
        }

        @SuppressLint("MissingPermission")
        private fun subscribeMeshtastic(gatt: BluetoothGatt) {
            val service = gatt.getService(MESH_SERVICE)
            if (service == null) {
                handleLinkFailure("connected device does not expose the Meshtastic service — is it an RNode, or is the board's Bluetooth off?")
                return
            }
            rxChar = service.getCharacteristic(MESH_TORADIO)
            fromRadioChar = service.getCharacteristic(MESH_FROMRADIO)
            txChar = service.getCharacteristic(MESH_FROMNUM)
            val fromNum = txChar
            if (rxChar == null || fromRadioChar == null || fromNum == null) {
                handleLinkFailure("Meshtastic service is missing a characteristic")
                return
            }
            if (!gatt.setCharacteristicNotification(fromNum, true)) {
                handleLinkFailure("could not subscribe to the board's FromNum")
                return
            }
            val cccd = fromNum.getDescriptor(CCCD)
            if (cccd == null) {
                handleLinkFailure("FromNum has no notification descriptor")
                return
            }
            // The same two-step subscribe as the RNode's; onDescriptorWrite
            // then asks for the MTU, which matters more here -- a ToRadio has
            // to fit one write.
            val enable = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            val ok = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                gatt.writeDescriptor(cccd, enable) == BluetoothStatusCodes.SUCCESS
            } else {
                @Suppress("DEPRECATION")
                run {
                    cccd.value = enable
                    gatt.writeDescriptor(cccd)
                }
            }
            if (!ok) handleLinkFailure("could not enable FromNum notifications")
        }

        @SuppressLint("MissingPermission")
        override fun onDescriptorWrite(
            gatt: BluetoothGatt,
            descriptor: BluetoothGattDescriptor,
            status: Int,
        ) {
            if (descriptor.uuid != CCCD) return
            if (status != BluetoothGatt.GATT_SUCCESS) {
                handleLinkFailure("enabling notifications failed (status $status)")
                return
            }
            // A setup download is one GATT read per message; the default
            // connection interval made each ~90 ms (measured). Same request as
            // the Meshtastic Android app.
            if (meshtastic) gatt.requestConnectionPriority(BluetoothGatt.CONNECTION_PRIORITY_HIGH)
            // Only now ask for a bigger MTU. A full RNode frame is around 500
            // bytes and the default leaves 20 per write, so this is the
            // difference between one write and twenty-five.
            awaitingMtu = true
            if (!gatt.requestMtu(TARGET_MTU)) {
                awaitingMtu = false
                Log.w(TAG, "MTU request could not be started; continuing at $BASE_CHUNK bytes")
                linkUp()
                return
            }
            // A stack that already exchanged the MTU by itself may not answer
            // again; the link must not hang on a reply that is not coming.
            handler.postDelayed({
                if (this@BleSerialBridgeService.gatt === gatt && awaitingMtu) {
                    awaitingMtu = false
                    Log.w(TAG, "No MTU reply; continuing at $chunkSize byte chunks")
                    linkUp()
                }
            }, MTU_REPLY_WAIT_MS)
        }

        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            if (gatt !== this@BleSerialBridgeService.gatt) return
            // Not necessarily the answer to OUR request: Android 14 exchanges
            // the MTU by itself on connecting. MEASURED 2026-10-16 (OnePlus,
            // RAK4631, just bonded): onMtuChanged 2 ms after connect, before
            // discovery had started, and taking it as the end of setup opened
            // a link with no characteristics -- the first write failed with
            // "no write characteristic". The size is kept either way; only the
            // reply to the request onDescriptorWrite makes opens the link.
            val ours = awaitingMtu
            awaitingMtu = false
            if (status == BluetoothGatt.GATT_SUCCESS) {
                // Three bytes of that MTU are ATT header; the couple more are
                // the margin Reticulum's own BLE code leaves, and there is no
                // reason to be braver than it on the same hardware.
                chunkSize = minOf(mtu - 5, MAX_ATTR_LEN).coerceAtLeast(BASE_CHUNK)
                Log.i(TAG, "BLE MTU is $mtu, writing in $chunkSize byte chunks")
            } else {
                // Slower, not broken: KISS is a byte stream and does not care
                // where the chunk boundaries fall.
                Log.w(TAG, "MTU negotiation failed (status $status); continuing at $BASE_CHUNK bytes")
            }
            if (ours) linkUp()
        }

        @Deprecated("Superseded by the value-carrying form on API 33+")
        @Suppress("DEPRECATION") // the pre-33 form is what Android 12 delivers
        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
        ) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) return
            onNotified(characteristic, characteristic.value ?: return)
        }

        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            onNotified(characteristic, value)
        }

        private fun onNotified(characteristic: BluetoothGattCharacteristic, value: ByteArray) {
            when (characteristic.uuid) {
                UART_TX_CHAR -> if (!meshtastic) deliverFromLink(value)
                // The value is a counter, not data: the data is read.
                MESH_FROMNUM -> if (meshtastic) requestDrain()
            }
        }

        @Deprecated("Superseded by the value-carrying form on API 33+")
        @Suppress("DEPRECATION")
        override fun onCharacteristicRead(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) return
            if (characteristic.uuid != MESH_FROMRADIO) return
            readResult.offer(ReadResult(status, characteristic.value ?: ByteArray(0)))
        }

        override fun onCharacteristicRead(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
            status: Int,
        ) {
            if (characteristic.uuid != MESH_FROMRADIO) return
            readResult.offer(ReadResult(status, value))
        }

        /** Hidden BluetoothGattCallback method (not in the SDK stubs, so no
         * `override`), called whenever the connection's timing changes. Logged
         * because read speed depends on it and nothing else reports it.
         * MEASURED 2026-10-16 (OnePlus, RAK4631): 7.5 ms during setup, a
         * moment at 45 ms, then 15 ms for good. */
        @Suppress("unused")
        fun onConnectionUpdated(gatt: BluetoothGatt, interval: Int, latency: Int, timeout: Int, status: Int) {
            Log.i(TAG, "[mt-ble] connection timing: interval ${interval * 1.25} ms, " +
                    "peripheral latency $latency, supervision timeout ${timeout * 10} ms, status $status")
        }

        override fun onCharacteristicWrite(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            status: Int,
        ) {
            if (characteristic.uuid != UART_RX_CHAR && characteristic.uuid != MESH_TORADIO) return
            writeResult.offer(status)
        }
    }

    /** The link is only up once bytes can actually move in both directions. */
    @SuppressLint("MissingPermission") // the link cannot be open without it
    private fun linkUp() {
        if (linkOpen) return
        if (rxChar == null || txChar == null || (meshtastic && fromRadioChar == null)) {
            // Every path here comes after subscribing, so this is a callback
            // arriving out of order; refusing it is cheaper than a link that
            // fails its first write.
            Log.w(TAG, "BLE link not opened: setup has not found the characteristics yet")
            return
        }
        linkOpen = true
        connecting.set(false)
        Log.i(TAG, "${radioKind()} BLE link open")
        diagWrites.set(0)
        diagReads.set(0)
        // Anything the board queued while the link was coming up.
        if (meshtastic) requestDrain()

        // Tell Python what this radio is called, so the radio's own screen can
        // say so. The name of the device actually connected, not the saved
        // preference -- when no specific radio was chosen, the preference has
        // nothing in it. Off this thread: a Chaquopy call can block on the
        // interpreter, and this one is the Bluetooth stack's.
        val name = gatt?.device?.name ?: return
        ioExecutor.execute { PythonRnsService.call("set_radio_label", name) }
    }

    /** BluetoothGatt.refresh() is hidden API: there is no public way to drop
     * a stale service cache. Best effort; false means it was not available. */
    private fun refreshGattCache(gatt: BluetoothGatt): Boolean = try {
        gatt.javaClass.getMethod("refresh").invoke(gatt) as? Boolean ?: false
    } catch (e: Exception) {
        false
    }

    private fun radioKind() = if (meshtastic) "Meshtastic board" else "RNode"

    private fun hasConnectPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
                ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_CONNECT) ==
                PackageManager.PERMISSION_GRANTED

    @SuppressLint("MissingPermission") // name lookup only, behind the same check
    private fun isRNode(device: BluetoothDevice): Boolean =
        device.name?.startsWith(RNODE_NAME_PREFIX) == true &&
                (device.type == BluetoothDevice.DEVICE_TYPE_LE ||
                        device.type == BluetoothDevice.DEVICE_TYPE_DUAL)
}
