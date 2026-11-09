package io.github.stress5094.customrnsbridge

import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import com.hoho.android.usbserial.driver.UsbSerialDriver
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialPort.ControlLine
import java.io.IOException
import java.util.EnumSet

class BulkOnlyCdcDriver(private val device: UsbDevice) : UsbSerialDriver {
    private val ports: List<UsbSerialPort>

    init {
        val dataIf = findDataInterface()
        val bulkIn = findEndpoint(dataIf, UsbConstants.USB_ENDPOINT_XFER_BULK, true)
        val bulkOut = findEndpoint(dataIf, UsbConstants.USB_ENDPOINT_XFER_BULK, false)
        ports = listOf(BulkOnlyCdcPort(this, device, dataIf, bulkIn, bulkOut))
    }

    override fun getDevice() = device
    override fun getPorts() = ports

    private fun findDataInterface(): UsbInterface {
        for (i in 0 until device.interfaceCount) {
            val intf = device.getInterface(i)
            if (intf.interfaceClass == UsbConstants.USB_CLASS_CDC_DATA) {
                return intf
            }
        }
        for (i in 0 until device.interfaceCount) {
            val intf = device.getInterface(i)
            if (intf.interfaceClass == UsbConstants.USB_CLASS_VENDOR_SPEC) {
                return intf
            }
        }
        throw IllegalArgumentException("No usable interface found")
    }

    private fun findEndpoint(intf: UsbInterface, type: Int, dirIn: Boolean): UsbEndpoint {
        for (i in 0 until intf.endpointCount) {
            val ep = intf.getEndpoint(i)
            if (ep.type == type && (ep.direction == if (dirIn) UsbConstants.USB_DIR_IN else UsbConstants.USB_DIR_OUT)) {
                return ep
            }
        }
        throw IllegalArgumentException("Endpoint not found")
    }

    private class BulkOnlyCdcPort(
        private val driver: BulkOnlyCdcDriver,
        private val device: UsbDevice,
        private val dataInterface: UsbInterface,
        private val bulkIn: UsbEndpoint,
        private val bulkOut: UsbEndpoint
    ) : UsbSerialPort {
        private var connection: UsbDeviceConnection? = null

        override fun getDriver() = driver
        override fun getDevice() = device
        override fun getPortNumber() = 0
        override fun getWriteEndpoint() = bulkOut
        override fun getReadEndpoint() = bulkIn
        override fun getSerial() = ""

        override fun open(connection: UsbDeviceConnection) {
            this.connection = connection
            if (!connection.claimInterface(dataInterface, true)) {
                throw IOException("Failed to claim interface")
            }
        }

        override fun close() {
            try {
                connection?.releaseInterface(dataInterface)
            } catch (_: Exception) {}
            try {
                connection?.close()
            } catch (_: Exception) {}
            connection = null
        }

        override fun read(dst: ByteArray, timeoutMs: Int): Int {
            val conn = connection ?: throw IOException("Not open")
            return conn.bulkTransfer(bulkIn, dst, dst.size, timeoutMs)
                ?: throw IOException("Bulk read failed")
        }

        override fun read(dst: ByteArray, offset: Int, timeoutMs: Int): Int {
            val buf = ByteArray(dst.size - offset)
            val n = read(buf, timeoutMs)
            if (n > 0) System.arraycopy(buf, 0, dst, offset, n)
            return n
        }

        /**
         * bulkTransfer() returns how many bytes it ACTUALLY wrote, and is
         * allowed to write fewer than asked. Checking only for `< 0` (outright
         * failure) means a short write silently discards the remainder --
         * truncating the KISS frame mid-flight, with no exception and no log,
         * so the packet just vanishes. usb-serial-for-android's own
         * CommonUsbSerialPort loops for exactly this reason; this driver has
         * to as well, since it replaces that class wholesale to work around
         * the ESP32-S3's missing CDC control endpoint.
         *
         * Milestone 1 never exposed this: anything over 481 bytes raised
         * IOError before reaching the USB layer, so every write that got here
         * was small enough to complete in one transfer. Milestone 2 sends up
         * to the full 464-byte payload (~483 with the RNS header, more once
         * KISS-escaped), which is where short writes become plausible.
         *
         * Throwing on an unfinishable write matters as much as the loop:
         * RNodeInterface.process_outgoing already raises IOError when the
         * serial write comes up short, and RNS's reconnect machinery recovers
         * from that. A silent truncation is what it cannot recover from.
         */
        override fun write(data: ByteArray, timeoutMs: Int) {
            val conn = connection ?: throw IOException("Not open")
            val deadline = System.currentTimeMillis() + timeoutMs
            var offset = 0
            while (offset < data.size) {
                val remaining = (deadline - System.currentTimeMillis()).toInt()
                if (remaining <= 0) {
                    throw IOException("Bulk write timed out at $offset/${data.size} bytes")
                }
                // bulkTransfer always starts at index 0 of the buffer it is
                // given, so a partial write has to be re-offered as a slice.
                val chunk = if (offset == 0) data else data.copyOfRange(offset, data.size)
                val written = conn.bulkTransfer(bulkOut, chunk, chunk.size, remaining)
                if (written < 0) {
                    throw IOException("Bulk write failed at $offset/${data.size} bytes")
                }
                offset += written
            }
        }

        override fun write(data: ByteArray, offset: Int, timeoutMs: Int) {
            if (offset == 0) {
                write(data, timeoutMs)
            } else {
                val buf = data.copyOfRange(offset, data.size)
                write(buf, timeoutMs)
            }
        }

        override fun setParameters(baudRate: Int, dataBits: Int, stopBits: Int, parity: Int) {
            // ESP32-S3 USB Serial/JTAG has fixed 115200 8N1; no CDC control endpoint,
            // so we silently ignore these.
        }

        override fun getCD() = false
        override fun getCTS() = false
        override fun getDSR() = false
        override fun getDTR() = false
        override fun setDTR(value: Boolean) {}
        override fun getRI() = false
        override fun getRTS() = false
        override fun setRTS(value: Boolean) {}
        override fun getControlLines(): EnumSet<UsbSerialPort.ControlLine> = EnumSet.noneOf(UsbSerialPort.ControlLine::class.java)
        override fun getSupportedControlLines(): EnumSet<UsbSerialPort.ControlLine> = EnumSet.noneOf(UsbSerialPort.ControlLine::class.java)
        override fun purgeHwBuffers(purgeWriteBuffers: Boolean, purgeReadBuffers: Boolean) {}
        override fun setBreak(value: Boolean) {}
        override fun isOpen() = connection != null
    }

    companion object {
        /** Espressif's USB vendor id: the ESP32-S3's built-in USB Serial/JTAG, the
         * one device this driver exists for. */
        const val ESPRESSIF_VID = 0x303A

        /**
         * ONLY THE DEVICE THIS WAS WRITTEN FOR. This used to answer "yes" to any
         * device with a CDC data interface, which is nearly every USB serial
         * board there is -- including the nRF52 boards (RAK4631) that Meshtastic
         * runs on. This driver cannot raise DTR, and those boards send nothing
         * until the host does: measured with the Meshtastic handshake on a
         * RAK4631, DTR high -> 46 protocol frames back, DTR low -> zero bytes.
         * Driven by this class, such a board is silent no matter what the app
         * does, which on a phone looks exactly like a radio that is not there.
         * Everything else goes through the standard prober, which can set DTR.
         */
        fun probe(device: UsbDevice): Boolean {
            if (device.vendorId != ESPRESSIF_VID) return false
            for (i in 0 until device.interfaceCount) {
                val intf = device.getInterface(i)
                if (intf.interfaceClass == UsbConstants.USB_CLASS_CDC_DATA) return true
            }
            return false
        }
    }
}
