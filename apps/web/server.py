#!/usr/bin/env python3
"""BLDC PHM bench: Raspberry Pi 5 demo receiver + live dashboard server."""
import io
import json
import os
import sys
try:
    import termios                     # Linux (the Pi), always present there
except ImportError:                    # lets the module import for tests on Windows
    termios = None
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Run either as `python3 -m apps.web.server` or as a plain script path.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Shared with the bench and demo apps. Never restate the physics here:
# tools/check_calibration.py fails the build if a local copy reappears.
from bldc_phm import calibration as cal                       # noqa: E402
from bldc_phm.schema import FAULT_NAMES as FAULT_CODE_NAMES   # noqa: E402

# ------------------------------------------------------------------ config
PORT_CANDIDATES = ["/dev/serial0", "/dev/ttyAMA10", "/dev/ttyAMA0", "/dev/ttyS0"]
BAUD = 115200
HTTP_PORT = 8080
HERE = os.path.dirname(os.path.abspath(__file__))

# Display labels derive from bldc_phm.schema, same as the demo app.
FAULT_NAMES = {code: name.replace("_", " ").upper()
               for code, name in FAULT_CODE_NAMES.items()}
FAULT_KIND = {  # ok / warn / fault -> drives the UI colour
    0: "ok", 15: "idle",
    1: "fault", 2: "fault", 3: "fault", 4: "fault",
}

def temp_c_from_mv(mv):
    """Motor temperature, rounded for display. Physics lives in bldc_phm."""
    celsius = cal.temp_c_from_mv(mv)
    return None if celsius is None else round(celsius, 1)


# ------------------------------------------------------------------ shared state
class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = {
            "connected": False,          # a frame arrived recently
            "seq": None, "fault_code": None, "fault_name": "--",
            "fault_kind": "idle",
            "rpm": None, "current_a": None, "current_ma": None,
            "temp_c": None, "temp_mv": None,
            "vib_rms_mg": None, "vib_pk_mg": None,
            "gx": None, "gy": None, "gz": None,
            "tilt_deg": None,
            "last_frame_ts": 0.0, "age_s": None,
            "frames_total": 0, "port": None,
        }

    def update(self, **kw):
        with self.lock:
            self.data.update(kw)

    def snapshot(self):
        with self.lock:
            d = dict(self.data)
        ts = d.get("last_frame_ts") or 0.0
        age = (time.time() - ts) if ts else None
        d["age_s"] = round(age, 2) if age is not None else None
        d["connected"] = bool(age is not None and age < 2.0)
        return d


STATE = State()

# Healthy orientation baseline, measured on the bench (milli-g).
G0 = cal.ORIENTATION_BASELINE_MG


def tilt_from_gravity(gx, gy, gz):
    """Angle off the healthy baseline, rounded for display."""
    degrees = cal.tilt_deg(gx, gy, gz)
    return None if degrees is None else round(degrees, 1)


# ------------------------------------------------------------------ UART reader
def open_port(path, baud):
    """Open a serial port with raw termios config, no pyserial needed."""
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
    speed = getattr(termios, "B%d" % baud, termios.B115200)
    # raw mode
    iflag = 0
    oflag = 0
    lflag = 0
    cflag = termios.CLOCAL | termios.CREAD | termios.CS8
    cc = list(cc)
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag,
                                            speed, speed, cc])
    # switch to blocking reads now that it is configured
    os.set_blocking(fd, True)
    return fd


def find_and_open():
    for p in PORT_CANDIDATES:
        if os.path.exists(p):
            try:
                fd = open_port(p, BAUD)
                return fd, p
            except Exception as exc:
                sys.stderr.write("open %s failed: %s\n" % (p, exc))
    return None, None


def parse_frame(line):
    # PI,seq,fault,rpm,i_mA,temp_mv,vrms,vpk,gx,gy,gz
    if not line.startswith("PI,"):
        return None
    parts = line.strip().split(",")
    if len(parts) != 11:
        return None
    try:
        seq = int(parts[1]); fault = int(parts[2]); rpm = int(parts[3])
        i_ma = int(parts[4]); temp_mv = int(parts[5])
        vrms = int(parts[6]); vpk = int(parts[7])
        gx = int(parts[8]); gy = int(parts[9]); gz = int(parts[10])
    except ValueError:
        return None
    return dict(seq=seq, fault=fault, rpm=rpm, i_ma=i_ma, temp_mv=temp_mv,
                vrms=vrms, vpk=vpk, gx=gx, gy=gy, gz=gz)


def reader_loop():
    buf = b""
    fd = None
    port = None
    while True:
        if fd is None:
            fd, port = find_and_open()
            if fd is None:
                STATE.update(port=None)
                time.sleep(1.0)
                continue
            STATE.update(port=port)
            buf = b""
        try:
            chunk = os.read(fd, 256)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            fd = None
            time.sleep(0.5)
            continue
        if not chunk:
            time.sleep(0.005)
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            try:
                line = raw.decode("ascii", "ignore").strip()
            except Exception:
                continue
            fr = parse_frame(line)
            if not fr:
                continue
            i_a = fr["i_ma"] / 1000.0
            tc = temp_c_from_mv(fr["temp_mv"])
            tilt = tilt_from_gravity(fr["gx"], fr["gy"], fr["gz"])
            fault = fr["fault"]
            STATE.update(
                seq=fr["seq"], fault_code=fault,
                fault_name=FAULT_NAMES.get(fault, "?"),
                fault_kind=FAULT_KIND.get(fault, "warn"),
                rpm=fr["rpm"], current_a=round(i_a, 3), current_ma=fr["i_ma"],
                temp_c=tc, temp_mv=fr["temp_mv"],
                vib_rms_mg=fr["vrms"], vib_pk_mg=fr["vpk"],
                gx=fr["gx"], gy=fr["gy"], gz=fr["gz"], tilt_deg=tilt,
                last_frame_ts=time.time(),
                frames_total=STATE.data["frames_total"] + 1,
            )


# ------------------------------------------------------------------ HTTP server
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            html = io.open(os.path.join(HERE, "dashboard.html"),
                           encoding="utf-8").read()
            self._send(200, html)
        elif self.path.startswith("/api/state"):
            self._send(200, json.dumps(STATE.snapshot()),
                       "application/json")
        elif self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(STATE.snapshot())
                    self.wfile.write(("data: %s\n\n" % payload).encode())
                    self.wfile.flush()
                    time.sleep(0.1)
            except Exception:
                return
        else:
            self._send(404, "not found")


def main():
    t = threading.Thread(target=reader_loop, daemon=True)
    t.start()
    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    sys.stderr.write("bench dashboard on http://localhost:%d\n" % HTTP_PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
