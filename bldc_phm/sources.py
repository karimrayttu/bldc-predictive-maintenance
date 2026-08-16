"""Data sources: where frames come from."""

import threading
import queue
import time

try:
    import serial  # pyserial
    import serial.tools.list_ports as list_ports
except ImportError:                      # pragma: no cover
    serial = None
    list_ports = None


def list_serial_ports():
    """Return [(device, description, is_stlink), ...] for the port picker."""
    if list_ports is None:
        return []
    out = []
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        desc = p.description or ""
        is_stlink = ("STLINK" in desc.upper() or "ST-LINK" in desc.upper()
                     or "0483" in hwid)        # ST VID
        out.append((p.device, desc, is_stlink))
    return out


def find_stlink_port():
    """Auto-detect the NUCLEO ST-LINK virtual COM port. Returns device or None."""
    for device, _desc, is_stlink in list_serial_ports():
        if is_stlink:
            return device
    return None


class _BaseSource:
    def __init__(self):
        self.q = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None

    def start(self):
        self._stop.clear()
        self.last_error = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def drain(self, max_items=2000):
        """Pull up to max_items raw lines from the queue (non-blocking)."""
        out = []
        for _ in range(max_items):
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                break
        return out

    def send(self, text):
        """Send a command to the device (overridden by sources that support it)."""
        return False

    def _run(self):
        raise NotImplementedError


class SerialSource(_BaseSource):
    """    Reads newline-delimited CSV from the ST-LINK virtual COM port, with auto-reconnect:"""
    def __init__(self, port="COM3", baud=115200):
        super().__init__()
        self.port = port
        self.baud = baud
        self.connected = False
        self._ser = None

    def send(self, text):
        """Write a command line to the VCP (e.g. 'S1500\\n'). Needs RX-enabled firmware."""
        ser = self._ser
        if ser is not None and self.connected:
            try:
                ser.write(text.encode("ascii"))
                return True
            except Exception:
                return False
        return False

    def _run(self):
        if serial is None:
            self.last_error = "pyserial not installed"
            return
        while not self._stop.is_set():
            try:
                ser = serial.Serial(self.port, self.baud, timeout=1.0)
            except Exception as exc:    # busy (CubeMonitor) or absent -> retry, don't die
                self.connected = False
                self.last_error = f"{self.port}: {exc}"
                self._stop.wait(1.0)
                continue
            self._ser = ser
            self.connected = True
            self.last_error = None
            buf = bytearray()
            try:
                ser.reset_input_buffer()    # start clean (drop any mid-line bytes)
            except Exception:
                pass
            try:
                while not self._stop.is_set():
                    # Framing: read whatever's available, split on '\n', keep the
                    # partial remainder. Avoids the partial/glued lines that readline()
                    # produces under load (which the validator would reject).
                    try:
                        n = ser.in_waiting
                        chunk = ser.read(n if n else 1)
                    except Exception as exc:
                        self.last_error = str(exc)
                        break           # drop out to reconnect
                    if not chunk:
                        continue
                    buf.extend(chunk)
                    if len(buf) > 100000:   # runaway guard if no newline ever arrives
                        del buf[:-2000]
                    while True:
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = bytes(buf[:nl]).decode("ascii", errors="ignore")
                        del buf[:nl + 1]
                        if line.strip():
                            try:
                                self.q.put_nowait(line)
                            except queue.Full:
                                pass    # drop if GUI is behind; never block the reader
            finally:
                self.connected = False
                self._ser = None
                try:
                    ser.close()
                except Exception:
                    pass
