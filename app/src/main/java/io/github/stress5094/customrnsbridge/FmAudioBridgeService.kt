package io.github.stress5094.customrnsbridge

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.hardware.usb.UsbManager
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.Build
import android.util.Log
import com.chaquo.python.Python
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean

/**
 * The FM link's hardware half: owns the USB sound card, and republishes it as
 * the same byte pipe RadioBridgeService serves to Python, so fm_link.py's
 * FmInterface can be just another RNS Interface as far as rns_bridge.py's
 * start() is concerned.
 *
 * Unlike the USB/BLE cases, "the link" here is not a byte-oriented serial
 * port -- it is an audio channel that has to be modulated and demodulated by
 * the codec2 FSK-LDPC modem, via Chaquopy calls into fm_link.py. This class
 * does not know what a waveform is: it plays the samples it is handed, feeds
 * back the samples it captures, and keys the radio around the first.
 *
 * Half-duplex by construction, matching the physical channel: this is one
 * simplex FM frequency, so RX and TX can never usefully overlap. The RX loop
 * keeps draining AudioRecord throughout (so its buffer never overflows and
 * blocks), but skips feeding samples to the decoder while `isTransmitting` is
 * set -- this phone's own played-back burst leaking back into its own mic
 * would otherwise look exactly like a real received transmission to the
 * demodulator.
 *
 * WHAT IS NEW SINCE `0355f92^`'s VERSION, which this is otherwise a port of:
 *
 *  * EVERY NUMBER THAT HAS TO MATCH THE WAVEFORM COMES FROM PYTHON, in one
 *    call (fm_link.audio_plan()). The sample rate, the keying method, the
 *    burst duration and the listen-before-transmit bound were four Kotlin
 *    constants here, each a second copy of a Python one, and every one of
 *    them is silent when the two disagree -- a wrong sample rate transmits at
 *    the wrong pitch and logs nothing at either end. See AudioPlan below.
 *  * IT KEYS THE RADIO. The old transport was VOX-only: it played audio and
 *    let the cable notice. The operating point Phase 0 measured is
 *    CM108-keyed with a 0.10 s lead-in against the old 1.6 s VOX one, and
 *    that is where much of the burst-duration saving came from, so keying is
 *    now part of the configuration rather than something the cable does.
 *    Cm108Ptt is the port that does it; keylessFallback is what happens when
 *    the cable has no GPIO.
 */
class FmAudioBridgeService : RadioBridgeService() {

    companion object {
        private const val TAG = "FmAudioBridge"

        // The Kotlin side's half of fm_link.py's length-prefix framing
        // (FrameParser there is the mirror of this). See that module's
        // docstring for why the framing exists at all: it is socket-local
        // only, never sent over the air.
        private const val FRAME_HEADER_BYTES = 2

        // One AudioRecord read, as a fraction of a second. 0.2 s -- frequent
        // enough for reasonable receive latency and for
        // listen-before-transmit's reading to stay inside its own staleness
        // bound, without an excessive number of Chaquopy calls into
        // feed_audio(). In SAMPLES it depends on the sample rate, which is
        // the waveform's to choose, so it is a duration here and a count at
        // the call site.
        private const val RX_CHUNK_S = 0.2

        // Listen-before-transmit. FM has no CSMA of its own -- this link has
        // no fallback of any kind against two nodes keying up over each other
        // -- so the only thing that can avoid a collision is a node choosing
        // not to transmit when it can already hear one in progress.
        //
        // NOT a raw energy/RMS check. Measured directly on real hardware
        // (2026-09-20): this project's receive audio sits at a constantly loud
        // RMS (~0.65-0.75) with ZERO real traffic -- volume, squelch, and even
        // swapping the cable entirely made no measurable difference, pointing
        // at a raw/unfiltered discriminator tap where squelch and volume do
        // not apply. No energy threshold can discriminate real traffic from
        // that. fm_link.channel_busy() is used instead, which asks fm_carrier
        // whether the energy in the receive audio is spread the way FSK tones
        // are rather than the way noise is. Measured 2026-10-06 on a real
        // capture of a phone burst: idle 0.577 +/- 0.016 against 0.97-0.99 in
        // a burst, from audio whose RMS is 0.26 idle and 0.29 in the burst.
        //
        // NOT codec2's own sync state, which this used until 2026-10-06: that
        // reads idle for the whole of a burst and goes busy only once a frame
        // has decoded, so a node listening during another's burst heard a clear
        // channel and keyed up over it.
        private const val CHANNEL_BUSY_POLL_MS = 60L

        // ~5 s at the 0.2 s receive chunk. Diagnostic only.
        private const val CARRIER_LOG_EVERY_CHUNKS = 25L

        // Bench hook's request file, in filesDir. See playBenchRequests().
        private const val BENCH_PLAY_REQUEST = "fm_play_req"

        // A reading older than one chunk cannot answer "is the channel busy
        // RIGHT NOW" -- and the most common reason it goes stale is this
        // phone's OWN previous transmission: startRxLoop()'s isTransmitting
        // guard skips every sample while a burst plays, so the newest reading
        // right after one ends is however long that burst was. If a stale
        // reading were treated as "must be idle", listen-before-transmit
        // would be a no-op for every back-to-back send -- exactly the case it
        // exists to protect, since fm_resend's resends fire in bursts of
        // their own. So a stale reading means "UNKNOWN", not "idle":
        // waitForChannelClear() waits for a fresh one rather than assuming
        // anything from a stale one.
        //
        // Sized from the chunk period rather than fixed, because that is what
        // determines how often a fresh reading can possibly arrive. A bound
        // BELOW the chunk period is the trap: every reading reads stale, the
        // wait never finds a fresh one, and listen-before-transmit silently
        // degrades to "always time out and send anyway".
        private const val CHANNEL_BUSY_MAX_AGE_CHUNKS = 2

        /**
         * Why this node is transmitting UNKEYED although its configuration
         * asks for CM108, or null when keying is working or not configured.
         *
         * A COMPANION VAR RATHER THAN A FIELD IN PYTHON'S STATUS BLOCK,
         * deliberately: this is a fact about the USB cable attached to this
         * phone, and Phase 4 spent real effort keeping Android hardware out
         * of the Python layer -- fm_link.py is the only FM module that
         * imports RNS, and none of them import anything Android. Pushing a
         * cable's GPIO situation through the bridge to come back out in
         * get_status() would cross that boundary for one string.
         *
         * READ BY MainActivity, which puts it on the status screen, because
         * the symptom of a lead-in too short for the keying in force is "some
         * configurations never sync" rather than a clean failure -- a
         * diagnosis nobody reaches from the symptom. See keyIfConfigured().
         */
        @Volatile var keylessReason: String? = null
    }

    override val tag = TAG
    override val linkDescription = "FM link active (audio)"
    override val bridgePort = FM_BRIDGE_PORT

    /**
     * The waveform's own numbers, as fm_link.audio_plan() reports them.
     *
     * HELD AS AN OBJECT AND RE-ASKED PER BURST, not captured once. The
     * profile is applied by PythonRnsService on a background thread that is
     * not ordered against this service's startup, so the first plan this
     * service sees may predate set_fm_profile() -- re-asking means a burst is
     * always built, keyed and waited against the configuration actually in
     * force. The exception is `sampleRateHz`, which AudioRecord was opened
     * with and cannot change under it; a plan that disagrees with the open
     * recorder is reported as a link failure so it reopens at the right rate.
     */
    private data class AudioPlan(
        val sampleRateHz: Int,
        val keying: String,
        val leadInS: Double,
        val burstS: Double,
        val pttKeyupS: Double,
        val channelBusyWaitS: Double,
        val backoffMaxS: Double,
        val interBurstGapS: Double,
        val profile: String,
        val summary: String,
    ) {
        val chunkSamples: Int get() = maxOf(160, (sampleRateHz * RX_CHUNK_S).toInt())
        val chunkMs: Long get() = (chunkSamples * 1000L) / sampleRateHz
        val channelBusyMaxAgeMs: Long get() = chunkMs * CHANNEL_BUSY_MAX_AGE_CHUNKS
        val channelBusyMaxWaitMs: Long get() = (channelBusyWaitS * 1000).toLong()
        val keysPtt: Boolean get() = keying == KEYING_CM108

        companion object {
            const val KEYING_CM108 = "cm108"

            /**
             * NO native_lib_dir, because audio_plan() needs none: like
             * hw_mtu() it is arithmetic over fm_config and cannot fail for
             * want of libcodec2, which is what lets it be asked this early in
             * startup.
             *
             * Null when Python cannot answer -- the caller must not invent a
             * sample rate, because a burst at the wrong rate is a burst at the
             * wrong pitch and nothing at either end logs it.
             */
            fun fetch(context: Context): AudioPlan? = try {
                if (!Python.isStarted()) {
                    Python.start(com.chaquo.python.android.AndroidPlatform(context))
                }
                val json = JSONObject(
                    Python.getInstance().getModule("fm_link")
                        .callAttr("audio_plan").toString())
                AudioPlan(
                    sampleRateHz = json.getInt("sample_rate_hz"),
                    keying = json.getString("keying"),
                    leadInS = json.getDouble("lead_in_s"),
                    burstS = json.getDouble("burst_s"),
                    pttKeyupS = json.getDouble("ptt_keyup_s"),
                    channelBusyWaitS = json.getDouble("channel_busy_wait_s"),
                    backoffMaxS = json.getDouble("backoff_max_s"),
                    interBurstGapS = json.optDouble("inter_burst_gap_s", 0.0),
                    profile = json.optString("profile"),
                    summary = json.optString("summary"),
                )
            } catch (e: Exception) {
                Log.e(TAG, "fm_link.audio_plan() failed", e)
                null
            }
        }
    }

    private var audioManager: AudioManager? = null
    @Volatile private var record: AudioRecord? = null
    @Volatile private var rxThread: Thread? = null
    @Volatile private var running = false
    private val isTransmitting = AtomicBoolean(false)

    // The plan the open AudioRecord was built against. Null until openLink()
    // has succeeded once.
    @Volatile private var plan: AudioPlan? = null

    private var ptt: Cm108Ptt? = null

    // Listen-before-transmit's live channel-occupancy reading (from
    // fm_link.channel_busy()). Written only by startRxLoop(), and only while
    // it is genuinely listening (not during our own TX -- see
    // CHANNEL_BUSY_MAX_AGE_CHUNKS); read by waitForChannelClear() on
    // whichever thread is about to key up.
    @Volatile private var lastChannelBusy = false
    @Volatile private var lastRxSampledAtMs = 0L
    private var rxChunks = 0L
    private var loggedBusy = false

    // Bytes accumulated from Python's socket writes (process_outgoing()'s
    // packed frames) until at least one complete frame is available.
    private var txAccum = ByteArray(0)

    /** When this node's last burst ended (elapsedRealtime), 0 = none yet. See
     *  spaceFromLastBurst(). */
    @Volatile private var lastBurstEndMs = 0L

    // Event-driven detection, added 2026-09-22 after real hardware showed the
    // old purely-poll-driven design could go tens of seconds without even
    // LOOKING for the cable: openLink() only ever ran at startup and via
    // retryOpen()'s 3 s loop, and that loop itself was only ever bootstrapped
    // indirectly, by Python's first write attempt failing -- and FmInterface
    // sends nothing on its own until RNS has real traffic queued, which can be
    // up to a beacon interval away. Two independent signals now trigger an
    // immediate recheck instead of waiting on any of that:
    //
    //   * UsbManager's attach/detach broadcast -- the same pattern
    //     UsbSerialBridgeService already uses for the raw-serial RNode link.
    //     Not filtered to a specific vendor/product id (unlike that sibling's
    //     RNode probe): any USB audio device attaching is worth a recheck.
    //   * AudioManager.AudioDeviceCallback -- the more precise signal for THIS
    //     transport, since what openLink() cares about is an audio ROUTE
    //     existing, not raw USB enumeration, and there can be a real gap
    //     between the two while the audio HAL loads a driver for a freshly
    //     attached device.
    //
    // Both just re-run the same scan openLink() always did; the 3 s poll loop
    // stays as a backstop for whatever either one misses.
    private val usbAttachReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            when (intent.action) {
                UsbManager.ACTION_USB_DEVICE_ATTACHED -> {
                    Log.i(TAG, "USB device attached, rechecking for FM audio input")
                    attemptOpen(firstAttempt = false)
                }
                UsbManager.ACTION_USB_DEVICE_DETACHED ->
                    handleLinkFailure("USB device detached")
            }
        }
    }

    private val audioDeviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(addedDevices: Array<AudioDeviceInfo>) {
            if (addedDevices.any { it.isUsbAudioDevice() }) {
                Log.i(TAG, "USB audio input added, rechecking link")
                attemptOpen(firstAttempt = false)
            }
        }

        override fun onAudioDevicesRemoved(removedDevices: Array<AudioDeviceInfo>) {
            if (removedDevices.any { it.isUsbAudioDevice() }) {
                handleLinkFailure("USB audio input removed")
            }
        }
    }

    private fun AudioDeviceInfo.isUsbAudioDevice() =
        type == AudioDeviceInfo.TYPE_USB_HEADSET || type == AudioDeviceInfo.TYPE_USB_DEVICE

    override fun prepareLink() {
        audioManager = getSystemService(Context.AUDIO_SERVICE) as AudioManager
        audioManager?.registerAudioDeviceCallback(audioDeviceCallback, null)
        val filter = IntentFilter().apply {
            addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED)
            addAction(UsbManager.ACTION_USB_DEVICE_DETACHED)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(usbAttachReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(usbAttachReceiver, filter)
        }
    }

    // Synchronized so the three independent triggers that can call this (the
    // 3 s retryOpen() poll, the USB broadcast, and the audio device callback)
    // cannot race each other into building two AudioRecords off the same
    // read-then-act "if (record != null) return" check below. Shares its
    // monitor with the base class's own @Synchronized handleLinkFailure() --
    // both synchronize on this same service instance, so the two stay
    // serialized against each other too.
    @Synchronized
    override fun openLink(firstAttempt: Boolean) {
        if (record != null) return  // already open

        val am = audioManager ?: return
        val usbIn = am.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull { it.isUsbAudioDevice() }
        if (usbIn == null) {
            if (firstAttempt) Log.w(TAG, "No USB audio input device found")
            else Log.d(TAG, "Still no USB audio input device")
            return
        }

        // CHECKED EXPLICITLY, because the failure is otherwise mute and
        // mis-diagnosable: without RECORD_AUDIO, AudioRecord.Builder() throws
        // or the recorder initialises and reads nothing but zeros, and the
        // symptom on the status screen is "No audio" -- identical to an
        // unplugged cable, which is where anyone would look first. There is
        // nothing this service can do about it (only an Activity can ask), so
        // it says exactly what is wrong and waits for the retry loop.
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            if (firstAttempt) {
                Log.e(TAG, "RECORD_AUDIO is not granted -- the FM link cannot " +
                        "hear anything. Grant the microphone permission (the " +
                        "radio link dialog asks for it when FM is selected).")
            }
            return
        }

        val fetched = AudioPlan.fetch(applicationContext)
        if (fetched == null) {
            // NOT a guessed rate. 8000 Hz happens to be every profile's rate
            // today, but a hardcoded fallback here is exactly the second copy
            // this class stopped keeping: it would open the recorder at a rate
            // the waveform may not be using and nothing would report it.
            if (firstAttempt) {
                Log.e(TAG, "cannot read fm_link.audio_plan() -- not opening the " +
                        "audio link rather than guessing a sample rate")
            }
            return
        }

        val minBuf = AudioRecord.getMinBufferSize(
            fetched.sampleRateHz, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuf <= 0) {
            Log.e(TAG, "AudioRecord.getMinBufferSize rejected " +
                    "${fetched.sampleRateHz}Hz mono (got $minBuf)")
            return
        }

        val rec = try {
            AudioRecord.Builder()
                // MIC, not VOICE_COMMUNICATION -- measured 2026-09-10/11 on a
                // OnePlus 8T: the telephony-call audio pipeline
                // VOICE_COMMUNICATION pulls in (echo cancellation, noise
                // suppression, AGC) gated a real over-the-air burst down to
                // near-silence on that device, where MIC captured it cleanly.
                .setAudioSource(MediaRecorder.AudioSource.MIC)
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(fetched.sampleRateHz)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .build())
                .setBufferSizeInBytes(minBuf * 4)
                .build()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to build AudioRecord", e)
            return
        }
        rec.preferredDevice = usbIn
        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            Log.e(TAG, "AudioRecord failed to initialize")
            rec.release()
            return
        }

        rec.startRecording()
        plan = fetched
        record = rec
        running = true
        // preferredDevice above is a REQUEST -- routedDevice (valid only once
        // startRecording() has run) is the ground truth for what the OS
        // actually captured from. This project has been bitten before by an
        // OEM audio HAL silently ignoring the request and falling back to the
        // built-in mic, which looks identical to a real RF/signal problem
        // until this is checked directly.
        val routed = rec.routedDevice
        Log.i(TAG, "FM audio link open at ${fetched.sampleRateHz} Hz, requested " +
                "${usbIn.productName} (type ${usbIn.type}), " +
                "routedDevice=${routed?.productName} (type ${routed?.type})")
        Log.i(TAG, "FM operating point: ${fetched.summary}")
        if (fetched.keysPtt) connectPtt()
        startRxLoop()
    }

    override fun isLinkOpen() = record != null

    /**
     * Python -> socket -> here. Called by the base class's pump thread with
     * whatever chunk size one read() happened to return -- not necessarily
     * one whole frame, and not necessarily only one -- so accumulate and
     * extract complete length-prefixed frames before doing anything with
     * them.
     */
    @Throws(IOException::class)
    override fun writeToLink(data: ByteArray) {
        if (record == null) throw IOException("FM audio link is not open")
        txAccum += data
        while (true) {
            if (txAccum.size < FRAME_HEADER_BYTES) return
            val len = ((txAccum[0].toInt() and 0xFF) shl 8) or (txAccum[1].toInt() and 0xFF)
            val total = FRAME_HEADER_BYTES + len
            if (txAccum.size < total) return
            val payload = txAccum.copyOfRange(FRAME_HEADER_BYTES, total)
            txAccum = txAccum.copyOfRange(total, txAccum.size)
            transmitOneFrame(payload)
        }
    }

    override fun closeLink() {
        running = false
        // UNKEYED FIRST, and before anything else can return early: a link
        // being torn down mid-burst with PTT asserted leaves the radio
        // transmitting a dead carrier until the cable is unplugged, which
        // jams the channel for every other node on it.
        //
        // AND THE HANDLE IS DROPPED, not kept across the reopen. The usual
        // reason this runs is that the USB device went away, which leaves a
        // claimed interface that still accepts calls and reaches nothing --
        // the precise failure this project has spent the most time on. A
        // reopen builds a fresh Cm108Ptt and finds out for real; the cost is
        // that Android may re-ask for USB permission after a re-plug, which is
        // a prompt against a link that would otherwise silently stop keying.
        try { ptt?.unkey() } catch (e: Exception) { /* best effort */ }
        try { ptt?.close() } catch (e: Exception) { /* best effort */ }
        ptt = null
        keylessReason = null
        try { record?.stop() } catch (e: Exception) { /* already stopped */ }
        record?.release()
        record = null
        txAccum = ByteArray(0)
    }

    override fun releaseLink() {
        running = false
        rxThread?.interrupt()
        closeLink()
        audioManager?.unregisterAudioDeviceCallback(audioDeviceCallback)
        try {
            unregisterReceiver(usbAttachReceiver)
        } catch (e: IllegalArgumentException) {
            // Not registered -- prepareLink() never ran, or this is a second
            // releaseLink() call. Either way, nothing to undo.
        }
    }

    // -- keying -------------------------------------------------------------

    /**
     * Claim the CM108 GPIO the configured keying method needs.
     *
     * ASYNCHRONOUS AND NOT WAITED ON. Cm108Ptt.connect() may have to put a
     * USB permission dialog in front of the operator, and blocking the audio
     * link's startup on that would mean a node that cannot receive either
     * until someone taps a dialog. Receiving does not need PTT; transmitting
     * checks keyless() at the time it keys.
     */
    private fun connectPtt() {
        if (ptt != null) return
        val p = Cm108Ptt(applicationContext)
        ptt = p
        p.connect(object : Cm108Ptt.Callback {
            override fun onReady() {
                Log.i(TAG, "CM108 PTT ready")
                keylessReason = null
            }

            override fun onError(message: String) {
                Log.e(TAG, "CM108 PTT unavailable: $message")
                keylessReason = message
            }
        })
    }

    /**
     * Whether a burst about to play will go out UNKEYED although the
     * configuration asked for CM108.
     *
     * FALLS BACK RATHER THAN REFUSING, and the margin is the reason. A cable
     * with no GPIO output report is VOX-only (the K1 PRO is one -- the udev
     * rules in this repo claimed otherwise and were wrong), and VOX attack
     * measured 85-90 ms on the desktop radios through the desktop cable
     * against the CM108 profile's 0.10 s lead-in. So a keyless burst at this
     * operating point is MARGINAL, not hopeless: roughly 10 ms of margin
     * instead of the ~200 ms the VOX lead-in budgets. Refusing would make
     * such a node mute, which is a worse outcome than a marginal link an
     * operator can see and act on.
     *
     * AND IT MUST BE VISIBLE, which is the whole point of keylessReason being
     * readable from outside: a lead-in too short for the keying in use
     * presents as "some configurations never sync", not as a clean failure,
     * and that is a diagnosis nobody reaches from the symptom. MainActivity
     * puts it on the status screen.
     */
    private fun keyIfConfigured(p: AudioPlan): Boolean {
        if (!p.keysPtt) return false
        val keyed = ptt?.takeIf { it.isReady() }?.key() ?: false
        if (!keyed && keylessReason == null) {
            keylessReason = "the CM108 keying command did not reach the cable"
        }
        return keyed
    }

    // -- transmit -----------------------------------------------------------

    /**
     * Waits for the channel to read idle before returning, up to the bound
     * fm_link.channel_busy_wait_s() derives from the live burst -- see that
     * function for why it gives up rather than blocking forever, and
     * CHANNEL_BUSY_MAX_AGE_CHUNKS for why a stale reading waits for a fresh
     * one instead of assuming idle.
     *
     * Called BEFORE isTransmitting is set, deliberately: setting it first
     * would stop startRxLoop() from ever producing the fresh reading this is
     * waiting on.
     */
    /**
     * The detector's level, floor and threshold as one log fragment.
     * Offline field logs are read days later by someone who cannot re-run
     * the test, so every listen-before-transmit decision records the numbers
     * it was made on. Never throws: a log helper must not cost a transmission.
     */
    private fun carrierDetail(): String = try {
        Python.getInstance().getModule("fm_link")
            .callAttr("carrier_detail", applicationInfo.nativeLibraryDir).toString()
    } catch (e: Exception) {
        "level=? floor=? thr=?"
    }

    /** What the detector read at the instant we decided to key without waiting. */
    private fun noteClearKeyUp() {
        Log.i(TAG, "LBT key-up: channel clear, never deferred (${carrierDetail()})")
    }

    private fun waitForChannelClear(p: AudioPlan) {
        val deadline = System.currentTimeMillis() + p.channelBusyMaxWaitMs
        // Set once this call has seen the channel busy. A node that waited is
        // released the instant the burst it waited for ends, along with every
        // other node that waited for it, so it backs off for a random time and
        // LISTENS AGAIN before keying -- see fm_link.BACKOFF_MAX_S. A node that
        // never saw it busy transmits at once, exactly as before.
        var deferred = false
        while (System.currentTimeMillis() < deadline) {
            val age = System.currentTimeMillis() - lastRxSampledAtMs
            if (age > p.channelBusyMaxAgeMs) {
                Thread.sleep(CHANNEL_BUSY_POLL_MS)
                continue
            }
            if (lastChannelBusy) {
                // Once per wait, not once per poll: this loop spins every
                // CHANNEL_BUSY_POLL_MS and each detail read is a Python call.
                if (!deferred) Log.i(TAG, "LBT defer: channel busy (${carrierDetail()})")
                deferred = true
                Thread.sleep(CHANNEL_BUSY_POLL_MS)
                continue
            }
            if (!deferred) return noteClearKeyUp()
            // Idle, after having been busy. Wait out a random slice and look
            // again: whoever drew a shorter wait is on the air by then, and the
            // carrier detector hears it within ~0.3 s.
            val waitMs = (Math.random() * p.backoffMaxS * 1000).toLong()
            Log.i(TAG, "LBT clear after deferring; backing off ${waitMs}ms (${carrierDetail()})")
            Thread.sleep(waitMs)
            deferred = false
        }
        Log.w(TAG, "LBT gave up: channel still reads busy after ${p.channelBusyMaxWaitMs}ms, " +
                "transmitting anyway (${carrierDetail()})")
    }

    /** One physical burst, built by fm_link.py, keyed, and played via AudioTrack. */
    /**
     * Leave a gap after our OWN previous burst before starting the next.
     *
     * 2026-10-08, ten minutes of two-way traffic: this node's resend and beacon
     * timers coincided and it sent 2-3 bursts back to back (runs up to ~7 s). A
     * polite peer that waited the run out (its hold is 1.0 s, then a random
     * back-off) found our next burst already starting, so it never got a turn.
     * The gap is interBurstGapS plus a random 0..backoffMaxS, the same law the
     * back-off after a deferral uses; it applies only when the previous burst
     * ended less than that ago, so an idle node adds no delay at all.
     */
    private fun spaceFromLastBurst(p: AudioPlan) {
        val end = lastBurstEndMs
        if (end == 0L || p.interBurstGapS <= 0.0) return
        val needMs = ((p.interBurstGapS + Math.random() * p.backoffMaxS) * 1000).toLong()
        val sinceMs = android.os.SystemClock.elapsedRealtime() - end
        if (sinceMs < needMs) {
            Log.d(TAG, "spacing back-to-back bursts: waiting ${needMs - sinceMs} ms")
            try { Thread.sleep(needMs - sinceMs) } catch (e: InterruptedException) {
                Thread.currentThread().interrupt()
            }
        }
    }

    private fun transmitOneFrame(payload: ByteArray) {
        // TEST MODE (RadioLink.fmTxOff): receive only. Checked per burst, as the
        // very first thing, so it holds against every sender at once -- beacons,
        // messages and the blind resend schedules all arrive here. Logged at
        // WARN on every drop: a node in this state looks healthy (online,
        // receiving, its sent counters climbing) and reaches no one.
        if (RadioLink.fmTxOff(applicationContext)) {
            Log.w(TAG, "FM TX SUPPRESSED by test mode (${payload.size} B dropped): " +
                    "this node is NOT transmitting")
            return
        }
        // RE-ASKED PER BURST rather than read from the cached plan, so a
        // profile applied after this service started is the one the burst is
        // built against. See AudioPlan's own comment.
        val p = AudioPlan.fetch(applicationContext)
            ?: run {
                Log.e(TAG, "no audio plan -- dropping a frame rather than " +
                        "transmitting it at a guessed configuration")
                return
            }
        // VOX keying is a CHOICE, not a failure: the warning below was set when
        // the service started against the pre-profile CM108 default (the start
        // race AudioPlan's comment describes) and would otherwise outlive the
        // profile that replaced it, claiming "transmitting WITHOUT PTT, lead-in
        // sized for keyed transmission" on a link whose lead-in is VOX-sized.
        if (!p.keysPtt) keylessReason = null
        spaceFromLastBurst(p)
        val open = plan
        if (open != null && open.sampleRateHz != p.sampleRateHz) {
            // The recorder cannot change rate under itself, and a burst played
            // at a rate the recorder is not on means the two directions of
            // this link disagree -- which is indistinguishable from dead
            // hardware. Reopen instead of transmitting into that.
            handleLinkFailure(
                "sample rate changed from ${open.sampleRateHz} to " +
                    "${p.sampleRateHz} Hz; reopening the audio link")
            return
        }

        var keyed = false
        // HELD OUTSIDE THE try SO THE finally CAN RELEASE IT. An AudioTrack
        // abandoned on an exception path is a native handle the audio HAL does
        // not reclaim, and this service runs for the life of the process: a
        // run of failed bursts exhausts the available tracks and the link goes
        // mute with nothing but the original exceptions to say why. The
        // version this ports leaked one per failed burst.
        var track: AudioTrack? = null
        try {
            if (!Python.isStarted()) {
                Python.start(com.chaquo.python.android.AndroidPlatform(applicationContext))
            }
            // MODULATED BEFORE LISTENING, not after, and the order is the
            // point. The version this is a port of waited for the channel to
            // clear and THEN built the burst, which put the whole modulation
            // -- a Chaquopy round trip that builds seconds of audio, on a
            // phone, behind the GIL the receive loop is also using -- between
            // "the channel was clear" and the key-up that acts on it. A peer
            // starting a burst inside that window is a collision the check was
            // there to avoid. With the samples already in hand, the key-up
            // follows the reading by a poll interval.
            val fmLink = Python.getInstance().getModule("fm_link")
            val pcm = fmLink.callAttr("build_tx_burst", applicationInfo.nativeLibraryDir, payload)
                .toJava(ByteArray::class.java)

            val am = audioManager ?: return
            val usbOut = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull { it.isUsbAudioDevice() }
            val minBuf = AudioTrack.getMinBufferSize(
                p.sampleRateHz, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            track = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(p.sampleRateHz)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .build())
                .setBufferSizeInBytes(maxOf(minBuf, pcm.size))
                .setTransferMode(AudioTrack.MODE_STATIC)
                .build()
            if (usbOut != null) track.preferredDevice = usbOut
            track.setVolume(RadioLink.fmTxGain(applicationContext))
            am.setStreamVolume(
                AudioManager.STREAM_VOICE_CALL, am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL), 0)

            track.write(pcm, 0, pcm.size)

            // Everything above is preparation and none of it touches the air,
            // so the half-duplex gate goes up here: from this point the
            // receive loop must stop feeding the decoder, because what it is
            // about to capture is this phone's own burst.
            waitForChannelClear(p)
            isTransmitting.set(true)
            noteTxState(true)

            // KEY FIRST, THEN WAIT THE KEY-UP, THEN PLAY -- in that order,
            // and the wait is not padding. The carrier is not up for
            // ptt_keyup_s (0.055 s measured) after PTT is asserted, and
            // anything played before then is simply gone; tools/
            // fm_bench_audio puts 0.30 s of digital silence in front of every
            // burst it plays for exactly this reason, and the measurements the
            // operating point rests on were taken that way. Playing
            // immediately would instead eat 0.055 s of a 0.10 s lead-in tone,
            // leaving the receiver's AGC half the settling time the
            // configuration budgets. fm_link._ptt_keyup_s() reports 0 under
            // VOX, where the audio IS the keying and waiting would delay the
            // thing doing it.
            keyed = keyIfConfigured(p)
            val keyupMs = (p.pttKeyupS * 1000).toLong()
            if (keyed && keyupMs > 0) Thread.sleep(keyupMs)

            track.play()
            val durationMs = (pcm.size / 2).toLong() * 1000L / p.sampleRateHz
            // The tail is for the PLAYBACK to drain, not for the radio:
            // AudioTrack.play() returns immediately, and stop() on a
            // still-playing static buffer truncates the burst -- which costs
            // the last frame, and at one in-burst copy that is the whole
            // message. It does mean PTT is held ~0.3 s past the audio, which
            // is airtime the model does not charge; shortening it to reclaim
            // that would be trading a measured failure for an unmeasured
            // saving, since a burst cut short decodes as nothing at all.
            Thread.sleep(durationMs + 300)
            track.stop()
        } catch (e: Exception) {
            Log.e(TAG, "FM transmit failed", e)
            handleLinkFailure("transmit failed: ${e.message}")
        } finally {
            try { track?.release() } catch (e: Exception) { /* best effort */ }
            // UNKEYED IN finally, unconditionally: every path out of this
            // block has to drop PTT, including the exception path. A radio
            // left keyed does not merely lose this message -- it holds the
            // channel against every node on it until the cable is unplugged.
            if (keyed) {
                try { ptt?.unkey() } catch (e: Exception) { /* best effort */ }
            }
            isTransmitting.set(false)
            noteTxState(false)
            lastBurstEndMs = android.os.SystemClock.elapsedRealtime()
        }
    }

    /**
     * BENCH HOOK (2026-10-09): play pre-built raw audio out the radio, so a
     * waveform this app does not implement can be put on the air through the
     * SAME keying, key-up wait, AudioTrack setup and cable as a real burst.
     * Nothing here knows what the audio is; the modem under test stays on the
     * laptop, which is what lets two modems be compared from identical
     * captures of identical hardware.
     *
     * `files/fm_play_req` is a text file, one burst per line:
     *
     *     <file in files/> <sample rate Hz> [seconds to wait afterwards]
     *
     * of mono PCM16LE. It is deleted BEFORE anything plays, so a request that
     * fails cannot be replayed in a loop, and it ignores RadioLink.fmTxOff on
     * purpose: that switch exists to silence the app's own traffic while a
     * bench runs, and this IS the bench. Normal transmitOneFrame() is
     * untouched.
     */
    private fun playBenchRequests(req: java.io.File) {
        val lines = try { req.readLines() } catch (e: Exception) {
            Log.e(TAG, "bench play request unreadable", e)
            emptyList()
        }
        req.delete()
        val p = AudioPlan.fetch(applicationContext)
            ?: run { Log.e(TAG, "bench play: no audio plan"); return }
        for (line in lines) {
            val f = line.trim().split(Regex("\\s+"))
            if (f.size < 2) continue
            val file = java.io.File(filesDir, f[0])
            val rate = f[1].toIntOrNull()
            if (rate == null || !file.exists()) {
                Log.e(TAG, "bench play: bad request '$line'")
                continue
            }
            val gapMs = ((f.getOrNull(2)?.toDoubleOrNull() ?: 0.0) * 1000).toLong()
            playBenchBurst(file.readBytes(), rate, p, f[0])
            if (gapMs > 0) {
                try { Thread.sleep(gapMs) } catch (e: InterruptedException) { return }
            }
        }
        Log.i(TAG, "bench play: request finished")
    }

    private fun playBenchBurst(pcm: ByteArray, rateHz: Int, p: AudioPlan, name: String) {
        var keyed = false
        var track: AudioTrack? = null
        try {
            val am = audioManager ?: return
            val usbOut = am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).firstOrNull { it.isUsbAudioDevice() }
            val minBuf = AudioTrack.getMinBufferSize(
                rateHz, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT)
            track = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_VOICE_COMMUNICATION)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build())
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setSampleRate(rateHz)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .build())
                .setBufferSizeInBytes(maxOf(minBuf, pcm.size))
                .setTransferMode(AudioTrack.MODE_STATIC)
                .build()
            if (usbOut != null) track.preferredDevice = usbOut
            track.setVolume(RadioLink.fmTxGain(applicationContext))
            am.setStreamVolume(
                AudioManager.STREAM_VOICE_CALL, am.getStreamMaxVolume(AudioManager.STREAM_VOICE_CALL), 0)
            track.write(pcm, 0, pcm.size)

            isTransmitting.set(true)
            noteTxState(true)
            keyed = keyIfConfigured(p)
            val keyupMs = (p.pttKeyupS * 1000).toLong()
            if (keyed && keyupMs > 0) Thread.sleep(keyupMs)
            Log.i(TAG, "BENCH TX $name start=${System.currentTimeMillis()} " +
                    "${pcm.size / 2} samples @ $rateHz Hz keyed=$keyed")
            track.play()
            val durationMs = (pcm.size / 2).toLong() * 1000L / rateHz
            Thread.sleep(durationMs + 300)
            track.stop()
        } catch (e: Exception) {
            Log.e(TAG, "bench play failed", e)
        } finally {
            try { track?.release() } catch (e: Exception) { /* best effort */ }
            if (keyed) {
                try { ptt?.unkey() } catch (e: Exception) { /* best effort */ }
            }
            isTransmitting.set(false)
            noteTxState(false)
            lastBurstEndMs = android.os.SystemClock.elapsedRealtime()
        }
    }

    /**
     * Tells Python when this node's own transmitter is up, so a burst heard
     * around it is logged as "lost under our transmit" and not as a channel
     * loss. Best effort: a failure here costs a log line's accuracy and must
     * never cost the transmit its unkey, which is why it is its own function
     * with its own catch and sits outside the try that drops PTT.
     */
    private fun noteTxState(active: Boolean) {
        try {
            if (!Python.isStarted()) return
            Python.getInstance().getModule("fm_link")
                .callAttr("note_tx_state", applicationInfo.nativeLibraryDir, active)
        } catch (e: Exception) {
            Log.w(TAG, "note_tx_state failed", e)
        }
    }

    /** Radio (air) -> here -> socket -> Python, continuously. */
    private fun startRxLoop() {
        val p = plan ?: return
        val t = Thread({
            val chunk = ShortArray(p.chunkSamples)
            // DIAGNOSTIC, off unless an empty marker file exists: everything
            // handed to the decoder is also written raw (PCM16LE, mono, at the
            // plan's sample rate) so a capture can be pulled with `adb run-as`
            // and decoded on a desktop to see exactly what THIS phone's
            // AudioRecord heard. Capped at ~5 minutes so a forgotten marker
            // cannot fill the phone.
            //
            // The marker is followed LIVE (2026-10-09), not read once at link
            // open: creating it starts a fresh fm_rx_dump.raw and deleting it
            // closes the file, so a bench can capture one run at a time without
            // restarting the app to re-arm it.
            var dump: java.io.OutputStream? = null
            var dumped = 0L
            val dumpCap = p.sampleRateHz.toLong() * 2L * 300L
            val dumpMarker = java.io.File(filesDir, "fm_rx_dump_on")
            val benchPlayReq = java.io.File(filesDir, BENCH_PLAY_REQUEST)
            while (running) {
                val rec = record ?: break
                val n = try {
                    rec.read(chunk, 0, chunk.size)
                } catch (e: Exception) {
                    Log.e(TAG, "AudioRecord read failed", e)
                    handleLinkFailure("audio read failed: ${e.message}")
                    break
                }
                if (n <= 0) continue

                val wantDump = dumpMarker.exists()
                if (wantDump && dump == null) {
                    try {
                        dump = java.io.FileOutputStream(java.io.File(filesDir, "fm_rx_dump.raw"))
                        dumped = 0L
                        Log.i(TAG, "rx dump ON -> fm_rx_dump.raw (${p.sampleRateHz} Hz mono PCM16)")
                    } catch (e: Exception) {
                        Log.e(TAG, "rx dump could not open", e)
                    }
                } else if (!wantDump && dump != null) {
                    try { dump.close() } catch (e: Exception) { /* best effort */ }
                    dump = null
                    Log.i(TAG, "rx dump OFF (${dumped} bytes)")
                }

                // BENCH HOOK: see playBenchRequests(). Run on this thread, so
                // the receive loop is simply not reading while the burst plays,
                // which is what a half-duplex node does anyway.
                if (benchPlayReq.exists()) {
                    playBenchRequests(benchPlayReq)
                    continue
                }

                // Keep draining the hardware buffer even while transmitting
                // (otherwise it fills and read() blocks/overflows) -- just do
                // not hand this phone's own played-back burst to the decoder
                // as if it were a real received signal. This is also why
                // listen-before-transmit's occupancy reading below only
                // updates in this branch: reading it during our own TX would
                // be reading our own leaked-back audio, not the channel.
                if (isTransmitting.get()) continue

                val pcm = ByteArray(n * 2)
                for (i in 0 until n) {
                    pcm[i * 2] = (chunk[i].toInt() and 0xFF).toByte()
                    pcm[i * 2 + 1] = ((chunk[i].toInt() shr 8) and 0xFF).toByte()
                }

                if (dump != null && dumped < dumpCap) {
                    try { dump.write(pcm); dumped += pcm.size } catch (e: Exception) { dump = null }
                }

                try {
                    if (!Python.isStarted()) {
                        Python.start(com.chaquo.python.android.AndroidPlatform(applicationContext))
                    }
                    val fmLink = Python.getInstance().getModule("fm_link")
                    val decoded = fmLink.callAttr(
                        "feed_audio", applicationInfo.nativeLibraryDir, pcm
                    ).toJava(ByteArray::class.java)
                    // Already length-prefixed by fm_link.feed_audio(), so this
                    // forwards the bytes verbatim with no framing logic of its
                    // own -- the one place the two halves of that framing could
                    // drift apart is a place neither of them has.
                    if (decoded.isNotEmpty()) deliverFromLink(decoded)

                    // Listen-before-transmit's occupancy reading. Read AFTER
                    // feed_audio(), not computed from the raw chunk: this is
                    // codec2's own sync state, updated by feed_audio() as a
                    // side effect of decoding the chunk just fed to it, not a
                    // separate measurement of the chunk itself.
                    val busyNow = fmLink.callAttr(
                        "channel_busy", applicationInfo.nativeLibraryDir
                    ).toJava(Boolean::class.java)
                    lastChannelBusy = busyNow
                    lastRxSampledAtMs = System.currentTimeMillis()

                    // The detector's level, logged on every transition and every
                    // ~5 s, so the phone's own idle audio can be compared with
                    // the desktop capture the threshold came from (idle 0.577
                    // +/- 0.016, worst 0.632; burst 0.97+). Idle readings near
                    // 0.70 would mean a false "busy" the beacon pays for.
                    rxChunks++
                    if (busyNow != loggedBusy || rxChunks % CARRIER_LOG_EVERY_CHUNKS == 0L) {
                        // floor= and thr= are what the detector decided with,
                        // and mono= is elapsedRealtime: logcat's wall clock can
                        // be stepped by the network during a test, and an
                        // offline log has no other way to show it.
                        // "level=X busy=B floor=.. thr=.." keeps the field order the
                        // scorer's regex (results/*/score.py) already reads.
                        val d = carrierDetail()
                        Log.i(TAG, "carrier %s busy=%b %s mono=%d%s".format(
                            d.substringBefore(' '), busyNow, d.substringAfter(' '),
                            android.os.SystemClock.elapsedRealtime(),
                            if (busyNow != loggedBusy) " (changed)" else ""))
                        loggedBusy = busyNow
                    }
                } catch (e: Exception) {
                    // A decode-path exception is not a radio failure -- the
                    // audio hardware is fine, something in the modem call
                    // itself went wrong. Log and keep listening rather than
                    // tearing down the link over it. lastRxSampledAtMs is
                    // deliberately NOT updated here -- a failed read leaves
                    // the occupancy reading stale, which waitForChannelClear
                    // already treats as "unknown" rather than "idle".
                    Log.e(TAG, "feed_audio failed", e)
                }
            }
        }, "fm-audio-rx")
        t.isDaemon = true
        rxThread = t
        t.start()
    }
}
