#!/usr/bin/env python3
"""PHM Monitor: BLDC Predictive Motor Failure Monitor (demo-day edition)
Clean fullscreen dashboard for the Raspberry Pi 5 touchscreen (800x480).
REAL DATA ONLY; every number is a live sensor value from the NUCLEO-F401RE.
"""

import sys
import os
import json
import time
import math
import argparse
import threading

try:
    import termios                     # Linux (the Pi); always present there
except ImportError:                    # allows headless UI testing on Windows
    termios = None

from PyQt5 import QtCore, QtGui, QtWidgets

# Run either as `python3 -m apps.demo.collector` or as a plain script path.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Calibration is shared with the bench app and the web view. Do not restate any
# of it here: tools/check_calibration.py fails the build if a copy reappears.
from bldc_phm.calibration import (            # noqa: E402
    ORIENTATION_BASELINE_MG,
    temp_c_from_mv,
    tilt_deg,
    to_bench_frame,
)
from bldc_phm.calibration import IDLE_OFFSET_A as _IDLE_OFFSET_A   # noqa: E402
from bldc_phm.calibration import MV_PER_AMP as _MV_PER_AMP         # noqa: E402
from bldc_phm.schema import FAULT_NAMES as _FAULT_CODE_NAMES       # noqa: E402


# -
# CONFIG
# -
# USB VCP first (demo-day hookup), GPIO UART as fallback
UART_PORTS = ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/serial0", "/dev/ttyAMA10"]
UART_BAUD = 115200

# Speed choices (campaign speeds; firmware full-scale is 2310 rpm)
SPEEDS = [300, 500, 900, 1300, 1700, 2100]

# Display labels derive from bldc_phm.schema so the demo, the web view and the
# dataset can never disagree about what a fault code is called.
FAULT_NAMES = {code: name.replace("_", " ").upper()
               for code, name in _FAULT_CODE_NAMES.items()}
FAULT_DETAIL = {
    0: "all channels inside the healthy envelope",
    1: "vibration + small orientation shift; mount is loose",
    2: "current + speed signature of mechanical drag on the rotor",
    3: "temperature rising while all other channels stay healthy",
    4: "large orientation change; motor position has shifted",
    15: "motor at rest; fault detector armed",
}
# banner gradient (top, bottom) per fault code: subtle depth, not flashy
FAULT_COLORS = {
    0: ("#2ea043", "#1f7a33"),   # green
    1: ("#d29922", "#a87717"),   # amber
    2: ("#e5484d", "#b62324"),   # red
    3: ("#e5484d", "#b62324"),
    4: ("#8957e5", "#6e44b8"),   # purple
    15: ("#363d47", "#262c34"),  # slate
}
BANNER_QSS = ("QFrame#banner { background: qlineargradient(x1:0, y1:0, x2:0, y2:1,"
              " stop:0 %s, stop:1 %s); border-radius: 14px; }")

# Current and accelerometer calibration come from bldc_phm.calibration, which
# reads bldc_phm/config/app_config.yaml. The demo mount reads 1 g as 1577 mg
# instead of the bench frame's 1050, and to_bench_frame() maps it back so the
# screen stays comparable to the recorded campaign.
MV_PER_AMP = _MV_PER_AMP
IDLE_OFFSET_A = _IDLE_OFFSET_A
G0 = ORIENTATION_BASELINE_MG

# Zero anchor to fall back on before the firmware has learned one this session.
ANCHOR_DEFAULT_MV = 1680.0

# 44-column CSV indices (firmware/main.c stream order)
COL_RPM, COL_I_MV, COL_VIB_RMS, COL_VIB_PK, COL_TEMP_MV = 1, 13, 16, 17, 18
COL_SEQ, COL_PWM, COL_GX, COL_GY, COL_GZ, COL_FAULT = 31, 32, 40, 41, 42, 43
N_COLS = 44

# termios.error is a plain Exception (NOT OSError); must be caught alongside
TERMIOS_ERROR = getattr(termios, "error", OSError) if termios else OSError

NO_VALUE = "n/a"          # shown wherever there is no reading yet

STATE_FILE = "/tmp/phm_state.json"


# tilt_deg, temp_c_from_mv and to_bench_frame are imported from
# bldc_phm.calibration above. cal_accel kept its old name at the call sites.
cal_accel = to_bench_frame


# -
# SERIAL LINK: reads telemetry, writes speed commands. stdlib termios only.
# -
class SerialLink(QtCore.QThread):
    packet = QtCore.pyqtSignal(object)
    portinfo = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._running = True
        self._fd = None
        self._wlock = threading.Lock()
        # rest-anchored current zero, the exact bench-app method: while the
        # motor is COMMANDED off and stopped, watch for 8 consecutive frames
        # whose sense-line mV is flat (spread <= 3 mV) and SNAP the anchor to
        # that window's mean. Re-snaps continuously at rest, so thermal drift
        # is tracked the same way the healthy campaign did it.
        self._anchor_mv = ANCHOR_DEFAULT_MV
        self._flat = []                # rolling window of rest-frame mV
        self._anchored = False         # no amps published until a real snap
        self.anchor_snap_t = 0.0       # monotonic time of the last snap

    # TX: speed command, same "S<rpm>\n" the bench PC always sent
    def send_rpm(self, rpm):
        with self._wlock:
            fd = self._fd
            if fd is None:
                return False
            try:
                os.write(fd, b"S%d\n" % int(rpm))
                return True
            except OSError:
                return False

    @staticmethod
    def _open(path):
        fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
            speed = getattr(termios, "B%d" % UART_BAUD, termios.B115200)
            cc = list(cc)
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 1      # 100 ms read timeout keeps the loop alive
            termios.tcsetattr(fd, termios.TCSANOW,
                              [0, 0, termios.CLOCAL | termios.CREAD | termios.CS8,
                               0, speed, speed, cc])
            os.set_blocking(fd, True)
        except Exception:
            try:
                os.close(fd)           # never leak the fd on a config failure
            except OSError:
                pass
            raise
        return fd

    def _drop_fd(self):
        with self._wlock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def run(self):
        buf = b""
        port_path = ""
        last_byte_t = time.monotonic()
        while self._running:
            if self._fd is None:
                for p in UART_PORTS:
                    if os.path.exists(p):
                        try:
                            self._fd = self._open(p)
                            port_path = p
                            last_byte_t = time.monotonic()
                            self.portinfo.emit(p)
                            buf = b""
                            break
                        except (OSError, TERMIOS_ERROR):
                            continue
                if self._fd is None:
                    self.portinfo.emit("")
                    time.sleep(1.0)
                    continue
            t_read = time.monotonic()
            try:
                chunk = os.read(self._fd, 512)
            except OSError:
                self._drop_fd()
                continue
            if not chunk:
                now = time.monotonic()
                # A hung-up CDC-ACM device (USB unplugged) returns EOF forever
                # instead of raising: detect it and re-enter the port scan.
                if not os.path.exists(port_path) or (now - last_byte_t) > 3.0:
                    self._drop_fd()
                    continue
                if now - t_read < 0.02:        # EOF spin guard: never burn a core
                    time.sleep(0.05)
                continue
            last_byte_t = time.monotonic()
            buf += chunk
            if len(buf) > 8192:            # runaway garbage guard
                buf = buf[-1024:]
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                d = self._parse(raw.decode("ascii", "ignore").strip())
                if d is not None:
                    self.packet.emit(d)
        with self._wlock:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def _parse(self, line):
        if line.startswith("PI,"):
            return self._parse_pi(line)
        return self._parse_csv(line)

    def _parse_csv(self, line):
        """Full 44-column firmware stream from the USB ST-LINK VCP."""
        parts = line.split(",")
        if len(parts) != N_COLS:
            return None
        try:
            v = [int(x) for x in parts]
        except ValueError:
            return None
        rpm, fault = v[COL_RPM], v[COL_FAULT]
        if not (0 <= fault <= 15 and 0 <= rpm <= 6000):
            return None
        # Flat-window anchor snap (the bench prezero method). Gate on
        # COMMANDED off (pwm==0) AND rpm==0: a stalled rotor has rpm 0 at
        # amp-class current and must never anchor (same gate as firmware).
        mv = float(v[COL_I_MV])
        if rpm == 0 and v[COL_PWM] == 0:
            self._flat.append(mv)
            if len(self._flat) > 8:
                self._flat.pop(0)
            if len(self._flat) == 8 and max(self._flat) - min(self._flat) <= 3.0:
                self._anchor_mv = sum(self._flat) / 8.0
                self._anchored = True
                self.anchor_snap_t = time.monotonic()
        else:
            self._flat.clear()
        amps = (max(0.0, IDLE_OFFSET_A + (mv - self._anchor_mv) / MV_PER_AMP)
                if self._anchored else None)
        cx, cy, cz, cvib, cpk = cal_accel(v[COL_GX], v[COL_GY], v[COL_GZ],
                                          v[COL_VIB_RMS], v[COL_VIB_PK])
        return {
            "seq": v[COL_SEQ], "fault_code": fault, "rpm": rpm,
            "current_a": amps, "i_mv": v[COL_I_MV],
            "temp_mv": v[COL_TEMP_MV], "temp_c": temp_c_from_mv(v[COL_TEMP_MV]),
            "vib_rms_mg": cvib, "vib_pk_mg": cpk,
            "grav_x_mg": cx, "grav_y_mg": cy, "grav_z_mg": cz,
            "tilt_deg": tilt_deg(cx, cy, cz),
            "raw_vib_mg": v[COL_VIB_RMS],
            "raw_g": [v[COL_GX], v[COL_GY], v[COL_GZ]],
        }

    @staticmethod
    def _parse_pi(line):
        """Compact GPIO-UART frame: PI,seq,fault,rpm,i_mA,temp_mv,vrms,vpk,gx,gy,gz"""
        parts = line.split(",")
        if len(parts) != 11:
            return None
        try:
            vals = [int(x) for x in parts[1:]]
        except ValueError:
            return None
        seq, fault, rpm, i_ma, temp_mv, vrms, vpk, gx, gy, gz = vals
        if not (0 <= fault <= 15 and 0 <= rpm <= 6000):
            return None
        cx, cy, cz, cvib, cpk = cal_accel(gx, gy, gz, vrms, vpk)
        return {
            "seq": seq, "fault_code": fault, "rpm": rpm,
            "current_a": max(0.0, i_ma / 1000.0),
            "temp_mv": temp_mv, "temp_c": temp_c_from_mv(temp_mv),
            "vib_rms_mg": cvib, "vib_pk_mg": cpk,
            "grav_x_mg": cx, "grav_y_mg": cy, "grav_z_mg": cz,
            "tilt_deg": tilt_deg(cx, cy, cz),
            "raw_vib_mg": vrms, "raw_g": [gx, gy, gz],
        }

    def stop(self):
        self._running = False


# -
# MAIN WINDOW. 800x480 touch layout: banner, tiles, speed buttons. No plots.
# -
QSS = """
* { font-family: 'DejaVu Sans', 'Liberation Sans', sans-serif; }
QWidget#root { background: #0b0f14; }

QLabel#title   { color: #e6edf3; font-size: 15px; font-weight: bold; letter-spacing: 1px; }
QLabel#link    { color: #f85149; font-size: 13px; font-weight: bold; }
QLabel#link[live="true"] { color: #3fb950; }

QFrame#banner  { background: #30363d; border-radius: 14px; }
QLabel#fault   { color: white; font-size: 46px; font-weight: bold; letter-spacing: 2px; }
QLabel#detail  { color: rgba(255,255,255,0.85); font-size: 14px; }

QFrame.tile    { background: #151b23; border: 1px solid #232b36; border-radius: 12px; }
QLabel.cap     { color: #8b98a9; font-size: 11px; font-weight: bold; letter-spacing: 1px; }
QLabel.val     { color: #e6edf3; font-size: 30px; font-weight: bold; }
QLabel.unit    { color: #8b98a9; font-size: 13px; font-weight: bold; }
QLabel#orient  { color: #e6edf3; font-size: 15px; font-weight: bold;
                 font-family: 'DejaVu Sans Mono', monospace; }

QPushButton.spd {
    background: #21262d; color: #e6edf3; font-size: 17px; font-weight: bold;
    border: 1px solid #30363d; border-radius: 10px;
}
QPushButton.spd:checked { background: #1f6feb; border-color: #1f6feb; color: white; }
QPushButton.spd:pressed { background: #388bfd; }

QPushButton#stop {
    background: #da3633; color: white; font-size: 18px; font-weight: bold;
    border: none; border-radius: 10px;
}
QPushButton#stop:pressed { background: #f85149; }

QPushButton#cal {
    background: #21262d; color: #58a6ff; font-size: 15px; font-weight: bold;
    border: 1px solid #30363d; border-radius: 10px;
}
QPushButton#cal:pressed { background: #1f6feb; color: white; }

QPushButton#refresh {
    background: #21262d; color: #8b98a9; font-size: 22px; font-weight: bold;
    border: 1px solid #30363d; border-radius: 10px;
}
QPushButton#refresh:pressed { background: #30363d; }

QLabel#subtitle { color: #6e7b8f; font-size: 11px; }

QPushButton#quit {
    background: transparent; color: #8b98a9; font-size: 13px; border: none;
}
"""


class Dashboard(QtWidgets.QWidget):
    def __init__(self, source):
        super().__init__()
        self.setObjectName("root")
        self.source = source
        self.port = ""
        self._last_rx = 0.0
        self._frames = 0
        self._sel_rpm = 0
        self._last_pkt = None
        self._state_t = 0.0
        # park re-roll blip (bench-app behavior): shortly after the link comes
        # up with the motor at rest, pulse the rotor so it re-parks fresh and
        # the current zero anchors on a clean, settled rest level
        self._blip = "pending"          # pending -> done
        self._link_up_t = None
        self._was_connected = False
        # pre-start calibration sequence (every start from rest)
        self._cal_seq = 0               # token: bumping it cancels the chain
        self._cal_msg = None

        self._build_ui()
        self.source.packet.connect(self.on_packet)
        self.source.portinfo.connect(self.on_port)

        self.tick = QtCore.QTimer(self)
        self.tick.timeout.connect(self.on_tick)
        self.tick.start(200)

    # UI
    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setContentsMargins(14, 8, 14, 12)
        root.setSpacing(8)

        # header: title + subtitle left, link status + quit right
        head = QtWidgets.QHBoxLayout()
        tcol = QtWidgets.QVBoxLayout()
        tcol.setSpacing(0)
        title = QtWidgets.QLabel("BLDC PREDICTIVE MAINTENANCE")
        title.setObjectName("title")
        sub = QtWidgets.QLabel("on-device ML fault detection · NUCLEO-F401RE → Raspberry Pi 5")
        sub.setObjectName("subtitle")
        tcol.addWidget(title)
        tcol.addWidget(sub)
        head.addLayout(tcol)
        head.addStretch(1)
        self.lbl_link = QtWidgets.QLabel("○ WAITING FOR NUCLEO USB")
        self.lbl_link.setObjectName("link")
        head.addWidget(self.lbl_link)
        btn_quit = QtWidgets.QPushButton("✕")
        btn_quit.setObjectName("quit")
        btn_quit.setFixedSize(30, 24)
        btn_quit.clicked.connect(self.close)
        head.addWidget(btn_quit)
        root.addLayout(head)

        # fault banner
        self.banner = QtWidgets.QFrame()
        self.banner.setObjectName("banner")
        self.banner.setFixedHeight(118)
        bl = QtWidgets.QVBoxLayout(self.banner)
        bl.setContentsMargins(16, 8, 16, 10)
        bl.setSpacing(0)
        self.lbl_fault = QtWidgets.QLabel(NO_VALUE)
        self.lbl_fault.setObjectName("fault")
        self.lbl_fault.setAlignment(QtCore.Qt.AlignCenter)
        self.lbl_detail = QtWidgets.QLabel("waiting for telemetry")
        self.lbl_detail.setObjectName("detail")
        self.lbl_detail.setAlignment(QtCore.Qt.AlignCenter)
        bl.addWidget(self.lbl_fault, stretch=1)
        bl.addWidget(self.lbl_detail)
        root.addWidget(self.banner)

        # sensor tiles: SPEED / CURRENT / TEMP / VIBRATION
        tiles = QtWidgets.QHBoxLayout()
        tiles.setSpacing(8)
        self.v_rpm, _ = self._tile(tiles, "SPEED", "rpm")
        self.v_cur, self.u_cur = self._tile(tiles, "BUS CURRENT", "A")
        self.v_tmp, _ = self._tile(tiles, "MOTOR TEMP", "°F")
        self.v_vib, _ = self._tile(tiles, "VIBRATION RMS", "mg")
        root.addLayout(tiles, stretch=1)

        # orientation strip
        strip = QtWidgets.QFrame()
        strip.setProperty("class", "tile")
        strip.setFixedHeight(52)
        sl = QtWidgets.QHBoxLayout(strip)
        sl.setContentsMargins(14, 4, 14, 4)
        cap = QtWidgets.QLabel("ORIENTATION")
        cap.setProperty("class", "cap")
        sl.addWidget(cap)
        self.lbl_orient = QtWidgets.QLabel("x: y: z: ·    tilt, °")
        self.lbl_orient.setObjectName("orient")
        sl.addStretch(1)
        sl.addWidget(self.lbl_orient)
        sl.addStretch(1)
        root.addWidget(strip)

        # speed buttons + stop
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(8)
        self.speed_btns = {}
        for sp in SPEEDS:
            b = QtWidgets.QPushButton(str(sp))
            b.setProperty("class", "spd")
            b.setCheckable(True)
            b.setFixedHeight(54)
            b.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                            QtWidgets.QSizePolicy.Fixed)
            b.clicked.connect(lambda _, s=sp: self.set_speed(s))
            self.speed_btns[sp] = b
            row.addWidget(b)
        root.addLayout(row)

        brow = QtWidgets.QHBoxLayout()
        brow.setSpacing(8)
        self.btn_stop = QtWidgets.QPushButton("■   STOP MOTOR")
        self.btn_stop.setObjectName("stop")
        self.btn_stop.setFixedHeight(56)
        self.btn_stop.clicked.connect(lambda: self.set_speed(0))
        brow.addWidget(self.btn_stop, stretch=2)
        self.btn_cal = QtWidgets.QPushButton("◎  CALIBRATE")
        self.btn_cal.setObjectName("cal")
        self.btn_cal.setFixedHeight(56)
        self.btn_cal.clicked.connect(self.run_calibration)
        brow.addWidget(self.btn_cal, stretch=1)
        btn_refresh = QtWidgets.QPushButton("⟳")
        btn_refresh.setObjectName("refresh")
        btn_refresh.setFixedSize(70, 56)
        btn_refresh.clicked.connect(self.refresh_app)
        brow.addWidget(btn_refresh)
        root.addLayout(brow)

    def _tile(self, layout, caption, unit):
        f = QtWidgets.QFrame()
        f.setProperty("class", "tile")
        v = QtWidgets.QVBoxLayout(f)
        v.setContentsMargins(14, 10, 14, 10)
        v.setSpacing(2)
        cap = QtWidgets.QLabel(caption)
        cap.setProperty("class", "cap")
        v.addWidget(cap)
        v.addStretch(1)
        val = QtWidgets.QLabel(NO_VALUE)
        val.setProperty("class", "val")
        v.addWidget(val)
        u = QtWidgets.QLabel(unit)
        u.setProperty("class", "unit")
        v.addWidget(u)
        layout.addWidget(f, stretch=1)
        return val, u

    # handlers
    def on_port(self, port):
        self.port = port

    def run_calibration(self):
        """One-tap full calibration: stop, park re-roll blip, flat-window
        zero snap, exactly the healthy-campaign prezero cycle."""
        self._cal_seq += 1
        seq = self._cal_seq
        self._sel_rpm = 0
        for b in self.speed_btns.values():
            b.setChecked(False)
        self._cal_msg = "calibrating; stopping motor"
        self.source.send_rpm(0)

        def _clear():
            if seq == self._cal_seq:
                self._cal_msg = None

        def _blip():
            if seq != self._cal_seq:
                return
            self._cal_msg = "calibrating; park re-roll blip"
            self.source.send_rpm(300)

        def _zero():
            if seq != self._cal_seq:
                return
            self._cal_msg = "calibrating; flat-window zero"
            self.source.send_rpm(0)
            t0 = time.monotonic()

            def _poll():
                if seq != self._cal_seq:
                    return
                if self.source.anchor_snap_t > t0 + 0.3:
                    self._cal_msg = "calibrated; zero locked ✓"
                    QtCore.QTimer.singleShot(3000, _clear)
                elif time.monotonic() - t0 > 8.0:
                    self._cal_msg = "calibration timed out; is the motor free?"
                    QtCore.QTimer.singleShot(4000, _clear)
                else:
                    QtCore.QTimer.singleShot(200, _poll)

            QtCore.QTimer.singleShot(600, _poll)

        QtCore.QTimer.singleShot(1500, _blip)
        QtCore.QTimer.singleShot(2300, _zero)

    def refresh_app(self):
        """Clean restart: stop the motor, hand off to a fresh instance."""
        try:
            self.source.send_rpm(0)
        except Exception:
            pass
        launcher = os.path.expanduser("~/launch_phm.sh")
        if os.path.exists(launcher):
            QtCore.QProcess.startDetached("bash", [launcher])
        QtCore.QTimer.singleShot(300, self.close)

    def set_speed(self, rpm):
        self._cal_seq += 1              # invalidate any in-flight sequence
        seq = self._cal_seq
        self._sel_rpm = rpm
        for s, b in self.speed_btns.items():
            b.setChecked(s == rpm)
        if rpm == 0:
            self._cal_msg = None
            self.source.send_rpm(0)
            return
        if (self._last_pkt or {}).get("rpm", 0) > 50:
            # already spinning: change speed directly, no calibration cycle
            self.source.send_rpm(rpm)
            return
        # PRE-START CALIBRATION runs the exact healthy-campaign cycle: park
        # re-roll blip, command 0, WAIT for the 8-frame flat-window anchor
        # snap on the freshly parked rotor, then spin up to the selection.
        self._cal_msg = "calibrating; park blip"
        self.source.send_rpm(300)

        def _step_go():
            if seq != self._cal_seq:
                return
            self._cal_msg = None
            self.source.send_rpm(rpm)

        def _step_zero():
            if seq != self._cal_seq:
                return
            self._cal_msg = "calibrating; waiting for flat-window zero"
            self.source.send_rpm(0)
            t0 = time.monotonic()

            def _poll():
                if seq != self._cal_seq:
                    return
                # fresh snap after the post-blip park -> anchored, go.
                # 6 s safety timeout so a noisy window can't wedge the start.
                if (self.source.anchor_snap_t > t0 + 0.3
                        or time.monotonic() - t0 > 6.0):
                    _step_go()
                else:
                    QtCore.QTimer.singleShot(200, _poll)

            QtCore.QTimer.singleShot(600, _poll)

        QtCore.QTimer.singleShot(800, _step_zero)

    def on_packet(self, pkt):
        self._last_rx = time.monotonic()
        self._frames += 1
        self._last_pkt = pkt

        fc = pkt["fault_code"]
        self.lbl_fault.setText(FAULT_NAMES.get(fc, "?"))
        self.lbl_detail.setText(self._cal_msg or FAULT_DETAIL.get(fc, ""))
        self.banner.setStyleSheet(
            BANNER_QSS % FAULT_COLORS.get(fc, FAULT_COLORS[15]))

        self.v_rpm.setText(str(pkt["rpm"]))
        self.v_cur.setText(NO_VALUE if pkt["current_a"] is None
                           else f"{pkt['current_a']:.3f}")
        # bench convention: show the raw INA240 sense-line mV beside the amps
        self.u_cur.setText("A" if pkt.get("i_mv") is None
                           else f"A   ·   {pkt['i_mv']} mV")
        self.v_tmp.setText(NO_VALUE if pkt["temp_c"] is None
                           else f"{pkt['temp_c'] * 9.0 / 5.0 + 32.0:.1f}")
        self.v_vib.setText(str(pkt["vib_rms_mg"]))
        self.lbl_orient.setText(
            f"x {pkt['grav_x_mg']:+5d}    y {pkt['grav_y_mg']:+5d}    "
            f"z {pkt['grav_z_mg']:+5d}    ·    tilt {pkt['tilt_deg']:.1f} °")

    # timer
    def on_tick(self):
        now = time.monotonic()
        connected = (now - self._last_rx) < 1.5
        if connected and not self._was_connected:
            self._link_up_t = now       # fresh link: arm the park blip
            self._blip = "pending"
        self._was_connected = connected
        if connected:
            self.lbl_link.setText(f"● LIVE: {os.path.basename(self.port) or 'serial'}")
            # park re-roll blip: only once per link, only from a verified
            # standstill with nothing selected: never against a running motor
            if (self._blip == "pending" and self._link_up_t is not None
                    and now - self._link_up_t >= 2.0):
                pkt = self._last_pkt or {}
                self._blip = "done"
                if pkt.get("rpm", 1) == 0 and self._sel_rpm == 0:
                    self.source.send_rpm(300)
                    QtCore.QTimer.singleShot(
                        800, lambda: (self._sel_rpm == 0
                                      and self.source.send_rpm(0)))
        else:
            self.lbl_link.setText(
                "○ NO TELEMETRY: CHECK NUCLEO USB" if self.port
                else "○ WAITING FOR NUCLEO USB")
            self.lbl_fault.setText(NO_VALUE)
            self.lbl_detail.setText("waiting for telemetry")
            self.banner.setStyleSheet(BANNER_QSS % FAULT_COLORS[15])
            for w in (self.v_rpm, self.v_cur, self.v_tmp, self.v_vib):
                w.setText(NO_VALUE)
            self.lbl_orient.setText("x: y: z: ·    tilt: °")
            # clear any pending speed selection: after a replug the Nucleo
            # boots with the motor OFF, and the UI must never suggest
            # (or resend) a speed the motor is not actually running
            if self._sel_rpm:
                self._sel_rpm = 0
                for b in self.speed_btns.values():
                    b.setChecked(False)
            self._link_up_t = None      # blip re-arms on the next link-up
        self.lbl_link.setProperty("live", "true" if connected else "false")
        self.lbl_link.style().unpolish(self.lbl_link)
        self.lbl_link.style().polish(self.lbl_link)

        # publish state once a second (atomic write) for headless verification
        now = time.monotonic()
        if now - self._state_t >= 1.0:
            self._state_t = now
            state = {"connected": connected, "port": self.port,
                     "frames": self._frames, "sel_rpm": self._sel_rpm}
            if self._last_pkt:
                state.update(self._last_pkt)
                state["fault_name"] = FAULT_NAMES.get(
                    self._last_pkt.get("fault_code"), "?")
                if state.get("temp_c") is not None:
                    state["temp_f"] = round(state["temp_c"] * 9.0 / 5.0 + 32.0, 1)
                state["anchored"] = self.source._anchored
                state["anchor_mv"] = round(self.source._anchor_mv, 1)
            try:
                with open(STATE_FILE + ".tmp", "w") as f:
                    json.dump(state, f)
                os.replace(STATE_FILE + ".tmp", STATE_FILE)
            except OSError:
                pass

    def closeEvent(self, e):
        try:
            self.source.send_rpm(0)     # never leave the motor running
            self.source.stop()
            # reader worst-case latency is the 1.0 s port-scan sleep: wait
            # longer than that so the QThread is never destroyed while running
            self.source.wait(2000)
        except Exception:
            pass
        e.accept()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fullscreen", action="store_true", help="kiosk fullscreen")
    args = ap.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)

    rx = SerialLink()
    win = Dashboard(source=rx)
    rx.start()

    if args.fullscreen:
        win.showFullScreen()
    else:
        win.resize(800, 480)
        win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
