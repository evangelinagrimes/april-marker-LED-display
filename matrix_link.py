"""
Serial Link to the ESP32 LED Matrix
=====================================
Owns the serial connection to the ESP32 and speaks its framed RGB protocol
(see led_matrix_esp/led_matrix_esp.ino). Deliberately has no printing and no
CLI concerns -- it raises MatrixLinkError for any I/O failure so callers
(the CLI and the GUI) get one consistent way to detect failure instead of
today's mix of print()-and-swallow for some errors and exception-propagation
for others.

--------------------------------------------------------------------------
Wire protocol
--------------------------------------------------------------------------
    Offset  Size  Field
    0       2     MAGIC     0xA5 0x5A
    2       1     TYPE      0x01 = RGB888 frame, 0x00 = ping
    3       N     PAYLOAD   N=768 for an RGB frame (row-major R,G,B), N=0 for a ping
    3+N     1     CHECKSUM  (TYPE + sum(PAYLOAD)) & 0xFF

A 2-byte magic (rather than the single sync byte the older grayscale
protocol used) matters here because an RGB payload can contain *any* byte
value -- a one-byte marker would be a real resync hazard, since a single
dropped byte could make the firmware's scanner latch onto the first
matching byte inside pixel data and render garbage. 0xA5 0x5A requires two
specific adjacent values to false-trigger; the checksum is the backstop
that catches it if that still happens.

The ESP32 replies with one line per frame: "OK: ..." on success (this is
also what a ping gets back, as its firmware-identification banner) or
"ERR: ..." if the checksum didn't match.
--------------------------------------------------------------------------
"""

import time

import serial
import serial.tools.list_ports

MAGIC = b"\xA5\x5A"
TYPE_PING = 0x00
TYPE_RGB = 0x01
PAYLOAD_BYTES = 768  # MATRIX_WIDTH * MATRIX_HEIGHT * 3, must match led_matrix_esp.ino

BAUD_RATE = 115200          # must match Serial.begin() in led_matrix_esp.ino
ACK_TIMEOUT_S = 0.5         # how long to wait for the ESP32's ack/ping reply
BOOT_SETTLE_S = 2.0         # opening the port resets most ESP32 boards; time to finish setup()


class MatrixLinkError(Exception):
    """Raised for any serial I/O failure: port busy/missing, disconnected
    mid-transfer, or the ESP32 explicitly reporting an error. Callers catch
    this one type regardless of the underlying cause."""


def list_ports():
    """Return [(device, description), ...] for every serial port the OS
    currently sees, e.g. [("COM3", "Silicon Labs CP210x USB to UART Bridge")]."""
    return [(p.device, p.description) for p in serial.tools.list_ports.comports()]


def build_frame(payload: bytes, type_byte: int = TYPE_RGB) -> bytes:
    """Wrap a payload in the MAGIC/TYPE/.../CHECKSUM framing described above."""
    if type_byte == TYPE_RGB and len(payload) != PAYLOAD_BYTES:
        raise ValueError(f"RGB frame payload must be exactly {PAYLOAD_BYTES} bytes, got {len(payload)}")
    checksum = (type_byte + sum(payload)) & 0xFF
    return MAGIC + bytes([type_byte]) + payload + bytes([checksum])


class MatrixLink:
    """Owns one serial connection to the ESP32. Not thread-safe -- must be
    used from exactly one thread at a time (see gui.py's worker thread)."""

    def __init__(self, port: str, baud: int = BAUD_RATE, timeout: float = ACK_TIMEOUT_S):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self):
        """Open the port with DTR/RTS already pinned low *before* the port
        opens, rather than opening first and pinning them after.

        pyserial only pushes a dtr/rts assignment to the hardware immediately
        if the port is already open (see serialutil.SerialBase's dtr/rts
        setters); setting them first and calling open() after defers that
        push into open()'s own line configuration, so the auto-reset circuit
        most ESP32 boards' USB-serial chips have never sees the transition
        that triggers a reset in the first place -- rather than seeing it and
        then having the lines pinned afterward, which is what naively opening
        via serial.Serial(port, baud) and reassigning dtr/rts after does.
        """
        try:
            ser = serial.serial_for_url(self.port, do_not_open=True)
            ser.baudrate = self.baud
            ser.timeout = self.timeout
            ser.dtr = False
            ser.rts = False
            ser.open()
        except (serial.SerialException, OSError) as e:
            raise MatrixLinkError(str(e)) from e
        self._ser = ser

    def close(self, final_payload: bytes = None):
        """Close the port. If `final_payload` is given and the link is
        still open, best-effort send it first -- errors are swallowed, same
        as every other non-fatal send failure this module already tolerates
        (e.g. ping()'s timeout): by the time you're closing, there's
        nothing more useful to do with a failed last send."""
        if final_payload is not None and self.is_open:
            try:
                self.send_rgb(final_payload)
            except MatrixLinkError:
                pass
        if self._ser is not None:
            try:
                self._ser.close()
            except (serial.SerialException, OSError):
                pass  # already going away; nothing meaningful to do about a close failure
            finally:
                self._ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _write_frame(self, frame: bytes) -> str:
        if not self.is_open:
            raise MatrixLinkError("serial port is not open")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(frame)
            self._ser.flush()
            response = self._ser.readline()
        except (serial.SerialException, OSError) as e:
            raise MatrixLinkError(str(e)) from e
        return response.decode("utf-8", errors="ignore").strip()

    def ping(self):
        """Send a ping frame and return the firmware's identification line,
        or None if it didn't respond within the timeout -- non-fatal, since
        the board may just still be mid-boot."""
        return self._write_frame(build_frame(b"", TYPE_PING)) or None

    def send_rgb(self, payload: bytes):
        """Send one RGB888 frame.

        Returns the ESP32's ack line ("OK: ...") on success, or None if it
        didn't respond within the timeout -- deliberately *not* treated as
        fatal here, since the frame very likely still displayed; the caller
        decides how much to make of a missing ack (see gui.py's error table).
        Raises MatrixLinkError if the write/read itself fails (port
        disconnected, etc.) or the ESP32 explicitly reports an error.
        """
        line = self._write_frame(build_frame(payload, TYPE_RGB))
        if line.upper().startswith("ERR"):
            raise MatrixLinkError(f"ESP32 rejected the frame: {line}")
        return line or None


def send_once(port: str, payload: bytes, baud: int = BAUD_RATE, settle: float = BOOT_SETTLE_S) -> str:
    """Convenience one-shot send for the CLI: open, give the board time to
    finish booting (opening the port may have reset it), send one frame,
    close. Raises MatrixLinkError on failure; returns the ack line (possibly
    empty if the ESP32 didn't respond in time)."""
    link = MatrixLink(port, baud=baud)
    link.open()
    try:
        time.sleep(settle)
        return link.send_rgb(payload) or ""
    finally:
        link.close()
