package io.github.stress5094.customrnsbridge

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.SystemClock
import android.util.Log
import android.util.LruCache
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors

/**
 * Satellite imagery tiles for the Coverage map's hybrid view.
 *
 * Source: the USGS National Map orthoimagery (USDA NAIP aerial photography),
 * PUBLIC DOMAIN and keyless, which is why it was chosen over Esri's sharper
 * World Imagery, whose terms are not clear for an app like this. Measured
 * 2026-10-19 at the test area: the tile cache is populated to zoom 16 (~2 m per
 * pixel at 33 N) and 404s above it, so closer zooms stretch z16 -- soft at
 * the map's tightest zoom. US only; elsewhere every tile 404s and the map
 * shows streets alone.
 *
 * Tiles are kept on disk ([dir]) once seen, so imagery that has been looked
 * at is there offline, and in a memory LRU for drawing. Fetches run on a
 * small pool and call [onLoaded] (any thread) so the view can redraw.
 */
class ImageryTiles(private val dir: File, private val onLoaded: () -> Unit) {

    companion object {
        private const val TAG = "ImageryTiles"
        private const val URL_TEMPLATE =
            "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/%d/%d/%d"
        /** Deepest zoom the service actually serves (measured, see above). */
        const val MAX_ZOOM = 16
        const val ATTRIBUTION = "Imagery: USDA, USGS The National Map"
        private const val UA = "rtak-fm-coverage/1.0 (field planning)"
        // Panning queues tiles faster than they arrive; past this many in
        // flight, a tile is simply asked for again on a later frame.
        private const val MAX_IN_FLIGHT = 16
        private const val RETRY_AFTER_MS = 30_000L
    }

    private val mem = object : LruCache<String, Bitmap>(48 * 1024 * 1024) {
        override fun sizeOf(key: String, value: Bitmap) = value.byteCount
    }
    private val missing = ConcurrentHashMap.newKeySet<String>()     // 404: never ask again
    private val failedAt = ConcurrentHashMap<String, Long>()          // network error: retry later
    private val inFlight = ConcurrentHashMap.newKeySet<String>()
    private val pool = Executors.newFixedThreadPool(4)

    private fun key(z: Int, x: Int, y: Int) = "$z/$x/$y"

    /** The tile if it is ready; otherwise null, and it is fetched. */
    fun get(z: Int, x: Int, y: Int): Bitmap? {
        val k = key(z, x, y)
        mem.get(k)?.let { return it }
        if (k in missing || k in inFlight) return null
        val failed = failedAt[k]
        if (failed != null && SystemClock.elapsedRealtime() - failed < RETRY_AFTER_MS) return null
        if (inFlight.size >= MAX_IN_FLIGHT) return null
        inFlight.add(k)
        pool.execute { load(z, x, y, k) }
        return null
    }

    /** The tile only if already in memory -- for drawing a stretched parent
     *  under a child that has not arrived yet, without fetching parents. */
    fun peek(z: Int, x: Int, y: Int): Bitmap? = mem.get(key(z, x, y))

    private fun load(z: Int, x: Int, y: Int, k: String) {
        try {
            val file = File(dir, "$z/$x/$y.jpg")
            var bytes: ByteArray? = if (file.exists()) file.readBytes() else null
            if (bytes == null) {
                val conn = URL(String.format(URL_TEMPLATE, z, y, x)).openConnection() as HttpURLConnection
                conn.connectTimeout = 15_000
                conn.readTimeout = 20_000
                conn.setRequestProperty("User-Agent", UA)
                try {
                    when (conn.responseCode) {
                        200 -> bytes = conn.inputStream.use { it.readBytes() }
                        404 -> {
                            missing.add(k)
                            return
                        }
                        else -> {
                            failedAt[k] = SystemClock.elapsedRealtime()
                            return
                        }
                    }
                } finally {
                    conn.disconnect()
                }
                file.parentFile?.mkdirs()
                val tmp = File(file.path + ".part")
                tmp.writeBytes(bytes!!)
                tmp.renameTo(file)
            }
            val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes!!.size)
            if (bmp == null) {
                file.delete()                  // corrupt on disk: fetch again later
                failedAt[k] = SystemClock.elapsedRealtime()
                return
            }
            mem.put(k, bmp)
            onLoaded()
        } catch (e: Exception) {
            Log.w(TAG, "tile $k: ${e.javaClass.simpleName}: ${e.message}")
            failedAt[k] = SystemClock.elapsedRealtime()
        } finally {
            inFlight.remove(k)
        }
    }

    fun shutdown() {
        pool.shutdownNow()
    }
}
