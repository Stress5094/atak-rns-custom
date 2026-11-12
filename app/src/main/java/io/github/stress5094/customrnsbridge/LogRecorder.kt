package io.github.stress5094.customrnsbridge

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.util.Log
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Streams this app's own log to a file, so a test that happens away from a
 * computer can still be read afterwards.
 *
 * WHY THIS EXISTS. Android's logcat is a ring buffer -- 256 KiB on both bench
 * phones -- and on a busy device that is minutes, not hours. The 2026-08-14
 * range test lost its entire record to exactly that: the walk ran 17:38-18:09,
 * and by 18:12 the oldest surviving line from this app was 18:08:31. Half an
 * hour of the only data the test existed to produce, gone before anyone got
 * back to a laptop. What survived the rotation was luck -- the tx policy
 * counters are cumulative since process start, so the movement-policy result
 * could still be read off the CURRENT status line long after the individual
 * lines describing it had aged out. That is not a thing to rely on twice.
 *
 * WHY logcat RATHER THAN A LOGGER. The interesting lines come from two places:
 * Kotlin (android.util.Log) and Python (RNS.log, which Chaquopy surfaces as
 * python.stdout). A logging library would have to be plumbed into both, and
 * the Python side is a third-party library logging on its own terms. Reading
 * logcat back catches everything either of them emits, in one place, with no
 * call sites to change and nothing to keep in sync.
 *
 * It also needs no permission. READ_LOGS is a system permission and we do not
 * have it -- but an app reading logcat without it still sees its OWN entries,
 * because logd filters by uid. Verified on the OnePlus bench phone: the output
 * contained python.stdout and CotTakServerBridge lines and nothing else, which
 * is both what we want and, usefully, free of the device noise that caused the
 * rotation in the first place.
 *
 * Internal storage rather than getExternalFilesDir() on purpose. The external
 * path looks friendlier but is not reliably reachable: adb's access to
 * /sdcard/Android/data was restricted in Android 11 and varies by version, and
 * a `run-as` shell gets no mount view of it at all. filesDir is always
 * readable through `run-as` on a debug build, which is what this repo already
 * uses for the node id and the deaf-to marker. Retrieval is in the README.
 */
object LogRecorder {

    private const val TAG = "LogRecorder"

    // One file set per app start, so a test run is a self-contained artifact
    // rather than something to be sliced back out of a shared file by
    // timestamp. Costs a little disk; saves the step that gets fumbled at the
    // end of a long day.
    private const val FILE_PREFIX = "bridge-"
    private const val FILE_SUFFIX = ".log"

    // Rotation WITHIN a run, in KiB per file and files per set. 4 MiB x 4 is
    // ~16 MiB. Measured on the OnePlus bench phone at loglevel 6: ~35 lines a
    // minute with two idle nodes, so roughly 3.5 KiB/min, so days rather than
    // hours before the set wraps -- and traffic raises that without getting
    // close. The cap exists so a phone left running all week does not quietly
    // fill up, not because a test run comes near it.
    private const val ROTATE_KB = 4096
    private const val ROTATE_FILES = 4

    // Old runs kept before the earliest is deleted. Enough to cover a day of
    // repeated tests without anyone having to think about housekeeping.
    private const val KEEP_RUNS = 8

    // Absolute, because an app subprocess does not reliably inherit a PATH
    // containing /system/bin.
    private const val LOGCAT = "/system/bin/logcat"

    @Volatile
    private var process: Process? = null

    @Volatile
    private var currentFile: File? = null

    /**
     * Safe to call more than once -- MainActivity.onCreate() runs again on
     * every configuration change, and a second recorder writing the same file
     * would interleave two copies of every line into it.
     */
    @Synchronized
    fun start(context: Context) {
        if (process?.isAlive == true) return

        try {
            val dir = File(context.filesDir, "logs")
            if (!dir.exists() && !dir.mkdirs()) {
                Log.e(TAG, "could not create $dir; logs stay in the ring buffer only")
                return
            }

            prune(dir)

            val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
            val file = File(dir, "$FILE_PREFIX$stamp$FILE_SUFFIX")

            // -v year because a log read days later needs an unambiguous date,
            // and threadtime is the format every other tool here prints.
            //
            // No -d: logcat dumps whatever is already buffered and then keeps
            // following. That is deliberate, and worth a line -- it means the
            // file opens with the backlog leading up to this moment, so the
            // startup sequence (radio parameters, negotiated bitrate) lands in
            // the file even though it was logged before the recorder existed.
            process = ProcessBuilder(
                LOGCAT,
                "-v", "threadtime",
                "-v", "year",
                "-f", file.absolutePath,
                "-r", ROTATE_KB.toString(),
                "-n", ROTATE_FILES.toString()
            ).redirectErrorStream(true).start()

            currentFile = file
            // Goes through logcat, so it lands in the file it names.
            Log.i(TAG, "recording this session's log to ${file.absolutePath}")
        } catch (e: Exception) {
            // Never fatal. A bridge that runs without a log file is worth
            // more than one that refuses to start because it has none.
            Log.e(TAG, "could not start the log recorder", e)
            process = null
            currentFile = null
        }
    }

    /** Whether logcat is currently being streamed to a file. */
    fun isRecording(): Boolean = process?.isAlive == true

    /**
     * Stops recording. The file stays where it is and can still be exported --
     * stopping is for ending a test cleanly, not for discarding it.
     */
    @Synchronized
    fun stop() {
        process?.destroy()
        process = null
        Log.i(TAG, "log recording stopped")
    }

    /**
     * Stop recording and delete every log and range-test file. Used by the
     * auto-erase feature, so a phone that wipes its settings also leaves no
     * on-device record of what happened.
     */
    @Synchronized
    fun erase(context: Context) {
        stop()
        currentFile = null
        try {
            File(context.filesDir, "logs").listFiles()?.forEach { it.delete() }
        } catch (e: Exception) {
            Log.w(TAG, "could not erase the logs", e)
        }
        Log.i(TAG, "logs erased")
    }

    /** The file this run is writing to, or null if recording never started. */
    fun currentLogFile(): File? = currentFile

    /** Total bytes across the current run's file set, for the status screen. */
    fun currentSizeBytes(): Long {
        val file = currentFile ?: return 0L
        val dir = file.parentFile ?: return 0L
        // The rotated siblings are file.1, file.2 ... alongside the live one.
        return (dir.listFiles() ?: emptyArray())
            .filter { it.name.startsWith(file.name) }
            .sumOf { it.length() }
    }

    /**
     * Writes the whole of the current run -- every rotated piece plus the live
     * file, in order -- into the phone's Downloads folder as one .txt.
     *
     * This is the half that makes the feature usable away from a desk. The log
     * itself lives in app-private storage, which is only reachable over adb,
     * and the phone that most needs its log is the one that spent the test out
     * of adb range. Downloads is readable by the Files app and by any share
     * target, so the log can come off the phone using only the phone.
     *
     * Stitching the rotated pieces together here rather than leaving that to
     * whoever reads it: `logcat -r` leaves a run as `x.log.3, x.log.2, x.log.1,
     * x.log` with the LIVE file newest and the numbering running backwards
     * into the past, which is not an ordering anyone should have to rediscover
     * at the point of trying to read a test.
     *
     * Returns a human-readable destination, or null if nothing could be
     * written. Best called after stop(): logcat buffers its writes, so
     * exporting mid-run can miss the last few seconds.
     */
    fun export(context: Context): String? {
        try {
            val live = currentFile ?: newestRun(context) ?: return null
            val pieces = runFilesInOrder(live).filter { it.exists() }
            if (pieces.isEmpty()) return null

            val name = live.name.removeSuffix(FILE_SUFFIX) + ".txt"
            val where = copyToDownloads(context, name, "text/plain", pieces) ?: return null
            Log.i(TAG, "exported ${pieces.size} log file(s) to $where")
            return where
        } catch (e: Exception) {
            Log.e(TAG, "could not export the log", e)
            return null
        }
    }

    /**
     * Copies the newest range test CSV (rangetest-<date>-<time>.csv, written by
     * rns_bridge.start_range_test into the same logs dir) to Downloads.
     *
     * The CSV is app-private like the log, so before this it could only come
     * off the phone over adb -- and the walking phone is the one whose CSV
     * holds the trustworthy distances (its own GPS), and the one that ends the
     * walk out of adb range. The 2026-11-08 walk ended with the Pixel away
     * from the desk and its CSV unreachable; this is the fix.
     *
     * Only the newest: the one just stopped is the one wanted, and older runs
     * are still on the phone for adb. Exporting mid-test is fine -- every row
     * is flushed as it is written. Returns the destination, or null if there
     * is no CSV or it could not be written.
     */
    fun exportRangeTest(context: Context): String? {
        try {
            val csv = (File(context.filesDir, "logs").listFiles() ?: return null)
                .filter { it.name.startsWith("rangetest-") && it.name.endsWith(".csv") }
                // The timestamp in the name sorts chronologically as text.
                .maxByOrNull { it.name } ?: return null
            val where = copyToDownloads(context, csv.name, "text/csv", listOf(csv))
                ?: return null
            Log.i(TAG, "exported range test ${csv.name} to $where")
            return where
        } catch (e: Exception) {
            Log.e(TAG, "could not export the range test", e)
            return null
        }
    }

    /**
     * Concatenates [pieces] into Downloads/[name]; returns where it went.
     *
     * MediaStore needs no permission and puts the file somewhere the user can
     * actually see. It arrived in API 29; below that, fall back to the app's
     * external files dir, which also needs no permission but is less
     * discoverable. Both bench phones are well past 29 -- this branch exists
     * because minSdk is 26.
     */
    private fun copyToDownloads(
        context: Context, name: String, mime: String, pieces: List<File>
    ): String? {
        val out = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.MediaColumns.DISPLAY_NAME, name)
                put(MediaStore.MediaColumns.MIME_TYPE, mime)
                put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS)
            }
            val uri = context.contentResolver
                .insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: return null
            context.contentResolver.openOutputStream(uri) to "Downloads/$name"
        } else {
            val dir = File(context.getExternalFilesDir(null), "exports")
            dir.mkdirs()
            val file = File(dir, name)
            file.outputStream() to file.absolutePath
        }
        val (stream, where) = out
        (stream ?: return null).use { sink ->
            pieces.forEach { piece -> piece.inputStream().use { it.copyTo(sink) } }
        }
        return where
    }

    /** Oldest rotated piece first, live file last. See export(). */
    private fun runFilesInOrder(live: File): List<File> {
        val dir = live.parentFile ?: return listOf(live)
        val rotated = (dir.listFiles() ?: emptyArray())
            .filter { it.name.startsWith(live.name + ".") }
            .mapNotNull { file ->
                file.name.substringAfterLast('.').toIntOrNull()?.let { it to file }
            }
            .sortedByDescending { it.first }
            .map { it.second }
        return rotated + live
    }

    /** The most recent run on disk, for exporting after a restart. */
    private fun newestRun(context: Context): File? {
        val files = File(context.filesDir, "logs").listFiles() ?: return null
        return files
            .filter { it.name.startsWith(FILE_PREFIX) && it.name.endsWith(FILE_SUFFIX) }
            // The timestamp in the name sorts chronologically as text.
            .maxByOrNull { it.name }
    }

    /**
     * Drops the oldest runs. Grouped by the timestamp in the name rather than
     * by mtime, because a rotated sibling is touched long after the run that
     * created it began and sorting on mtime would interleave two runs.
     */
    private fun prune(dir: File) {
        try {
            val runs = (dir.listFiles() ?: return)
                .filter { it.name.startsWith(FILE_PREFIX) }
                .groupBy { it.name.substringBefore(FILE_SUFFIX) }

            runs.keys.sortedDescending()
                .drop(KEEP_RUNS - 1)   // -1: the run about to be created
                .forEach { stale -> runs[stale]?.forEach { it.delete() } }
        } catch (e: Exception) {
            Log.w(TAG, "could not prune old logs", e)
        }
    }
}
