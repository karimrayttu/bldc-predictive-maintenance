"""BLDC PHM Data Acquisition: main window."""

import os
import io
import json
import sys
import csv
import time
import collections
import datetime
import yaml
import numpy as np
from PyQt5 import QtWidgets, QtCore, QtGui

from bldc_phm.sources import (SerialSource,
                              list_serial_ports, find_stlink_port)
from bldc_phm.validator import FrameStats, parse_and_validate
from apps.bench.ui.design import NO_VALUE
from bldc_phm.schema import FAULT_NAMES, decode_status
from bldc_phm.session import SessionManager
from bldc_phm.drift import SeverityTracker
from bldc_phm.taxonomy import Taxonomy
from bldc_phm.stm32tools import STM32Tools
from bldc_phm import coverage as cov
from bldc_phm import firmwaregen as fwg
from bldc_phm.livelog import LiveLog

# Fixture modes: (subroot folder, fixture_state, valid_for_classifier, dataset_use)
FIXTURE_MODES = {
    "commissioning_unmounted": ("commissioning_unmounted", "commissioning_unmounted", "false", "commissioning_only"),
    "mounted":                 ("mounted_baseline", "mounted", "true", "baseline"),
}

BUF = 1500
# 2026-07-30 (operator): 2-minute steady record per official run. At 10 Hz that is
# ~1200 frames per run: steady-state feature distributions (current, ripple,
# vibration, temp) converge in the first ~200 frames, so 1200 gives 6x margin while
# cutting the 150-run campaign from ~26 h of bench time to ~7 h. Kept equal to
# app.core.coverage.HOLD_S so the Coverage page's workload estimate is the truth.
FULL_RUN_HOLD_S = 120
SEV_COLOR = {0: "#2e7d32", 1: "#f9a825", 2: "#ef6c00", 3: "#c62828"}
SEV_TEXT = {0: "S0  HEALTHY", 1: "S1  INCIPIENT", 2: "S2  DEVELOPING", 3: "S3  SEVERE"}
HEALTH_TEXT = {0: "HEALTHY", 1: "INCIPIENT", 2: "DEVELOPING", 3: "SEVERE"}
# punchier chip fills (good white-text contrast) for status pills
CHIP_COLOR = {0: "#1f9d57", 1: "#d98a00", 2: "#e8590c", 3: "#e0362f"}
CHIP_NEUTRAL = "#26344a"


def _chip_css(bg):
    return (f"background:{bg}; color:#ffffff; font-size:18px; font-weight:bold; "
            "border:0; border-radius:13px; padding:6px 18px;")


class _BgWorker(QtCore.QThread):
    """Runs a callable off the UI thread and emits its result (keeps the app snappy)."""
    done = QtCore.pyqtSignal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent); self._fn = fn

    def run(self):
        try:
            res = self._fn()
        except Exception as exc:
            res = ("__error__", str(exc))
        self.done.emit(res)
QUALITY_MIN = 90        # a finished run below this frame-integrity % is auto-discarded


class WheelGuard(QtCore.QObject):
    """Ignore mouse-wheel over combos/spinboxes unless they're focused, so scrolling
    the settings panel never accidentally changes a value."""
    def eventFilter(self, obj, ev):
        if ev.type() == QtCore.QEvent.Wheel and not obj.hasFocus():
            ev.ignore()
            return True
        return super().eventFilter(obj, ev)
# Channels checked by default (available now from the Hall stream).
DEFAULT_CHANNELS = {"hall_rpm", "ripple"}


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.setWindowTitle("BLDC Motor Bench: Predictive Maintenance")
        # Fit the window to the screen that is actually available and centre it, rather
        # than always asking for 1380x880 wherever Qt happens to put it. On this laptop
        # that left the window hanging off the left edge with the speed gauge, health
        # banner and sensor strip clipped out of view. Clamped to 96% of the available
        # work area (which already excludes the taskbar), so it can never open larger
        # than the screen it is on.
        _avail = QtWidgets.QApplication.primaryScreen().availableGeometry()
        _w = min(1380, int(_avail.width() * 0.96))
        _h = min(880, int(_avail.height() * 0.96))
        self.resize(_w, _h)
        self.move(_avail.x() + (_avail.width() - _w) // 2,
                  _avail.y() + (_avail.height() - _h) // 2)
        self.setMinimumSize(900, 600)
        logo = self._logo_path()
        if logo:
            self.setWindowIcon(QtGui.QIcon(logo))

        self.tax = Taxonomy(os.path.join(config["app_dir"], "config", "taxonomy.yaml"))
        self.fixture_mode = config.get("default_fixture", "commissioning_unmounted")
        if self.fixture_mode not in FIXTURE_MODES:
            self.fixture_mode = "commissioning_unmounted"
        self.session = SessionManager(
            config["data_dir"],
            meta_fields=["motor_model", "fixture_state", "valid_for_classifier", "dataset_use",
                         "data_source", "authenticity", "run_kind"]
                        + self.tax.field_keys() + ["cal_verdict"],
            subroot=FIXTURE_MODES[self.fixture_mode][0])
        self.tools = STM32Tools(config.get("tools"), config.get("firmware_elf"))
        self.live = LiveLog(config["data_dir"])      # observability sink for review
        self._wheelguard = WheelGuard(self)          # stop wheel from changing values on scroll
        self.flash_proc = None
        self.source = None
        self._run_len = 30
        self.stats = FrameStats()
        self.sev = SeverityTracker()
        self.armed = False
        self.fw = {}                      # field key -> (kind, widget, extra)
        self.current_cfg = config.get("current_sensor", {}) or {}
        self.temp_cfg = config.get("temp_sensor", {}) or {}

        self.buf = {k: collections.deque(maxlen=BUF)
                    for k in ("rpm", "ripple_permil", "hall", "illegal", "edges", "t_ms",
                              "period_std_permil", "mech1x_permil", "elec_permil", "coast_ms",
                              "accel_rms_mg", "accel_pk_mg", "temp_mv")}
        self.last_frame = None

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_monitor_tab(), "Monitor")
        self.tabs.addTab(self._build_status_tab(), "Connection")
        self.tabs.addTab(self._build_plan_tab(), "Coverage")
        self.tabs.addTab(self._build_hookups_tab(), "Wiring")
        self.setCentralWidget(self.tabs)
        self._mon_tick = 0
        # refresh the relevant tab immediately when the user switches to it
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.status = self.statusBar()
        self.status.showMessage("Idle. Data folder: " + self.config["data_dir"])
        self._refresh_label_preview()
        self._check_hookups()
        self._update_runnext_label()

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)        # ~30 fps display for snappy live values
        self._reconnect_timer = QtCore.QTimer(self)
        self._reconnect_timer.timeout.connect(self._autoconnect_retry)
        self._reconnect_timer.start(5000)   # keep attaching to the board on its own

    # =====================================================  MONITOR TAB
    def _logo_path(self):
        """Resolve the app logo if present (drop app_logo.png/.ico in apps/bench/ui/assets/)."""
        base = os.path.join(self.config["ui_dir"], "assets")
        for name in ("app_logo.ico", "app_logo.png"):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
        return None

    def _build_monitor_tab(self):
        w = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(w)

        # Lightweight live-value display (replaces the live graphs), big number cards.
        disp = QtWidgets.QGridLayout(); disp.setSpacing(10)

        def card(title, color, big=False):
            fr = QtWidgets.QFrame()
            fr.setStyleSheet("QFrame{background:#131a24; border:1px solid #1e2836; border-radius:14px;}QFrame:hover{border-color:#27374b;}")
            cl = QtWidgets.QVBoxLayout(fr); cl.setContentsMargins(18, 12, 18, 16)
            t = QtWidgets.QLabel(title.upper()); t.setStyleSheet("color:#7d8da3; font-size:11px; font-weight:700; letter-spacing:1.1px; border:0;")
            val = QtWidgets.QLabel(NO_VALUE)
            val.setStyleSheet(f"color:{color}; font-size:{80 if big else 31}px; font-weight:{600 if big else 700}; letter-spacing:{-2 if big else -0.3}px; border:0;")
            cl.addWidget(t); cl.addWidget(val)
            return fr, val

        def chip_card(title):
            """A card whose value is a rounded status PILL (e.g. HEALTHY / WARNING)."""
            fr = QtWidgets.QFrame()
            fr.setStyleSheet("QFrame{background:#131a24; border:1px solid #1e2836; border-radius:14px;}QFrame:hover{border-color:#27374b;}")
            cl = QtWidgets.QVBoxLayout(fr); cl.setContentsMargins(18, 12, 18, 16)
            t = QtWidgets.QLabel(title.upper()); t.setStyleSheet("color:#7d8da3; font-size:11px; font-weight:700; letter-spacing:1.1px; border:0;")
            row = QtWidgets.QHBoxLayout(); row.setContentsMargins(0, 4, 0, 0)
            chip = QtWidgets.QLabel(NO_VALUE); chip.setStyleSheet(_chip_css(CHIP_NEUTRAL))
            chip.setAlignment(QtCore.Qt.AlignCenter)
            row.addWidget(chip); row.addStretch(1)
            cl.addWidget(t); cl.addLayout(row)
            return fr, chip

        # BIG live speed (prominent, fast refresh) spanning the top
        cR, self.val_rpm = card("LIVE SPEED   (rpm)", "#42a5f5", big=True)
        disp.addWidget(cR, 0, 0, 1, 2)
        ch, self.val_health = chip_card("System health"); disp.addWidget(ch, 1, 0, 1, 2)
        self.val_health.setText("STANDBY")
        c2, self.val_ripple = card("Speed ripple (permil)", "#26c6da")
        c3, self.val_hall = card("Hall state (1-6; 0/7 illegal)", "#ab47bc")
        c4, self.val_illegal = card("Illegal-Hall rise", "#ef5350")
        c5, self.val_coast = card("Coast-down (ms)", "#ffb74d")
        c6, self.val_rate = card("Frame rate (fps)", "#9ccc65")
        c7, self.val_good = chip_card("Frame integrity")
        c8, self.val_base = card("Ripple baseline (mu ± 3σ)", "#b0bec5")
        c9, self.val_current = card("DC BUS CURRENT · total (incl. driver idle)", "#f48fb1")
        self.val_current.setToolTip(
            "Total 24 V supply current, the same quantity a meter in series with "
            "the feed reads.\n"
            "Current calibration is presently INVALID. The card shows the live,\n"
            "untouched PA0 ADC-window mean, sample count, and factory-VREF voltage.\n"
            "After a fresh series-ammeter campaign passes, total current will use\n"
            "the raw PA0/VREFINT affine model with no auto-zero or idle add-back.\n"
            "Calibration tool: py -3 tools\\calibrate_current_raw.py --help"
        )
        c10, self.val_vib = card("VIBRATION · KX134 · rms / pk (mg)", "#ffd54f")
        self.val_vib.setToolTip(
            "KX134 tri-axial accelerometer (bit-bang I2C, D14/D15, addr 0x1F). "
            "AC vibration magnitude: RMS and peak in milli-g per 32-sample block. "
            "0 / 0 means the sensor was not detected at boot."
        )
        c12, self.val_sensors = card("SENSOR HEALTH · self-verified", "#81c784")
        self.val_sensors.setToolTip(
            "Each channel is proven against a physical invariant, in firmware:\n"
            "  ADC    VREFINT bandgap in band (ADC core + VDDA sane)\n"
            "  CURR   INA240 inside its linear band (not railed = open/short)\n"
            "  TEMP   NTC divider between the rails (not open/shorted)\n"
            "  ACCEL  KX134 answering AND reading ~1 g of gravity\n"
            "  HALL   legal code now, and all six codes seen (all 3 lines alive)\n"
            "A channel that fails shows FAULT and publishes NO value, so a broken "
            "wire can never look like a plausible reading."
        )
        c11, self.val_temp = card("MOTOR TEMP · NTC · PA1/A1", "#4dd0e1")
        self.val_temp.setToolTip(
            "10k NTC (beta 3550) with 10k series from 3.3 V on PA1. "
            "Firmware streams the divider mV; the app applies the beta equation "
            "(temp_sensor block in app_config.yaml)."
        )
        for i, c in enumerate((c2, c3, c4, c5, c6, c7, c8, c9, c10, c11, c12)):
            disp.addWidget(c, 2 + i // 2, i % 2)
        disp.setRowStretch(9, 1)
        root.addLayout(disp, 3)

        # right control panel (scrollable)
        panel = QtWidgets.QVBoxLayout()

        # branded header: logo badge + app name (logo optional -> apps/bench/ui/assets/app_logo.png)
        hdr = QtWidgets.QHBoxLayout(); hdr.setSpacing(11)
        logo = self._logo_path()
        if logo:
            lg = QtWidgets.QLabel()
            lg.setPixmap(QtGui.QPixmap(logo).scaledToHeight(46, QtCore.Qt.SmoothTransformation))
            lg.setStyleSheet("border:0;"); hdr.addWidget(lg)
        tt = QtWidgets.QVBoxLayout(); tt.setSpacing(0)
        ttl = QtWidgets.QLabel("BLDC Motor Bench")
        ttl.setStyleSheet("border:0; font-size:19px; font-weight:bold; color:#eaf1fa;")
        sub = QtWidgets.QLabel("BLDC Predictive Maintenance")
        sub.setStyleSheet("border:0; font-size:10px; color:#7d8ca3;")
        tt.addWidget(ttl); tt.addWidget(sub)
        hdr.addLayout(tt, 1)
        hdrw = QtWidgets.QWidget(); hdrw.setLayout(hdr); panel.addWidget(hdrw)

        # STOP ALL: motor OFF + abort any run/sweep (with confirm + discard)
        self.btn_stop = QtWidgets.QPushButton("■  STOP ALL")
        self.btn_stop.setStyleSheet("background:#e0362f; color:white; font-weight:bold; "
                                    "font-size:15px; padding:11px; border:0; border-radius:12px;")
        self.btn_stop.clicked.connect(self._stop_all)
        panel.addWidget(self.btn_stop)

        # Fixture as a segmented toggle (Unmounted | Mounted), no dropdown, no banner
        fxrow = QtWidgets.QHBoxLayout(); fxrow.setSpacing(0); fxrow.setContentsMargins(0, 0, 0, 0)
        self.btn_unmounted = QtWidgets.QPushButton("Unmounted"); self.btn_unmounted.setCheckable(True)
        self.btn_mounted = QtWidgets.QPushButton("Mounted"); self.btn_mounted.setCheckable(True)
        self.btn_unmounted.clicked.connect(lambda: self._set_fixture("commissioning_unmounted"))
        self.btn_mounted.clicked.connect(lambda: self._set_fixture("mounted"))
        fxrow.addWidget(self.btn_unmounted); fxrow.addWidget(self.btn_mounted)
        fxw = QtWidgets.QWidget(); fxw.setLayout(fxrow); panel.addWidget(fxw)
        self.lbl_fixture = QtWidgets.QLabel(""); self.lbl_fixture.setStyleSheet("border:0; font-size:11px;")
        self.lbl_fixture.setWordWrap(True); panel.addWidget(self.lbl_fixture)

        # Hardware: auto-detected ST-LINK, no simulator, no manual port/baud
        gb = QtWidgets.QGroupBox("Hardware")
        gh = QtWidgets.QVBoxLayout(gb)
        self.lbl_hw = QtWidgets.QLabel("Scanning…"); self.lbl_hw.setStyleSheet("border:0;")
        hrow = QtWidgets.QHBoxLayout()
        self.btn_conn = QtWidgets.QPushButton("Connect"); self.btn_conn.clicked.connect(self._toggle_connect)
        self.btn_rescan = QtWidgets.QPushButton("Rescan"); self.btn_rescan.setFixedWidth(78)
        self.btn_rescan.clicked.connect(self._rescan_ports)
        hrow.addWidget(self.btn_conn, 1); hrow.addWidget(self.btn_rescan)
        gh.addWidget(self.lbl_hw); gh.addLayout(hrow)
        panel.addWidget(gb)

        # ⚡ one-click Build + Flash + Run (firmware -> flash -> connect & run)
        self.btn_bfr = QtWidgets.QPushButton("⚡  Build · Flash · Run")
        self.btn_bfr.setStyleSheet("background:#2f7bf6; color:white; font-weight:bold; "
                                   "padding:11px; border:0; border-radius:12px;")
        self.btn_bfr.clicked.connect(self._build_flash_run)
        panel.addWidget(self.btn_bfr)

        # 🎯 start-of-day calibration check (verify readings before collecting data)
        self.btn_cal = QtWidgets.QPushButton("🎯  Calibration check  (new day)")
        self.btn_cal.setStyleSheet("background:#7a5cff; color:white; font-weight:bold; "
                                   "padding:10px; border:0; border-radius:12px;")
        self.btn_cal.setToolTip("Quick verified spin-up that checks rpm tracking, ripple, Hall health, "
                                "frame rate and integrity; run it each new day so you don't record false data.")
        self.btn_cal.clicked.connect(self._calibration_mode)
        panel.addWidget(self.btn_cal)

        # ===== PRIMARY action: run the next still-needed official test (step-through) =====
        self.btn_rec = QtWidgets.QPushButton("▶  Run next test")
        self.btn_rec.setStyleSheet("background:#23a55a; color:white; font-weight:bold; font-size:15px; "
                                   "padding:13px; border:0; border-radius:12px;")
        self.btn_rec.setToolTip("Runs the next test still needed for the current dataset "
                                "(spin → 2-min record → coast → save). Click again after each for the next one. "
                                "All tests are defined in the Coverage tab.")
        self.btn_rec.clicked.connect(self._run_next_remaining)
        panel.addWidget(self.btn_rec)

        # sleek run timer (elapsed / total + remaining): visible only during a run
        self.run_banner = QtWidgets.QFrame()
        self.run_banner.setStyleSheet("QFrame{background:#13243c; border:1px solid #295088; border-radius:11px;}")
        rb = QtWidgets.QVBoxLayout(self.run_banner); rb.setContentsMargins(13, 9, 13, 11); rb.setSpacing(6)
        self.run_clock = QtWidgets.QLabel(NO_VALUE)
        self.run_clock.setStyleSheet("color:#7fd0ff; font-weight:bold; font-size:14px; border:0;")
        self.run_prog = QtWidgets.QProgressBar(); self.run_prog.setRange(0, 100)
        self.run_prog.setTextVisible(False); self.run_prog.setFixedHeight(8)
        rb.addWidget(self.run_clock); rb.addWidget(self.run_prog)
        panel.addWidget(self.run_banner); self.run_banner.setVisible(False)
        self._rescan_ports()

        m = self.config.get("motor", {})
        lbl_motor = QtWidgets.QLabel(f"Motor: {m.get('model', '42BL41.010 Rev.B')}  ·  "
                                    f"{m.get('rated_rpm', 4000)} rpm  ·  {m.get('peak_current_A', 6.0)} A peak")
        lbl_motor.setWordWrap(True); lbl_motor.setStyleSheet("border:0; color:#8fdca0; font-size:11px;")
        panel.addWidget(lbl_motor)

        # ===== stacked sections (plain group boxes: NOT a QToolBox; its tab labels clipped) =====
        def _grp(page, title):
            gb = QtWidgets.QGroupBox(title)
            gl = QtWidgets.QVBoxLayout(gb); gl.setContentsMargins(8, 6, 8, 8); gl.addWidget(page)
            return gb

        # Speed page (quick live speed change + live R&D 'Test' probe)
        sp_page = QtWidgets.QWidget(); spv = QtWidgets.QVBoxLayout(sp_page)
        self._presets = [int(x) for x in (self.tax.list("speed_presets") or
                                          [0, 500, 800, 1100, 1400, 1700, 2000, 2400])]
        pr = QtWidgets.QGridLayout(); pr.setSpacing(4)
        for i, s in enumerate(self._presets):
            b = QtWidgets.QPushButton("OFF" if s == 0 else str(s)); b.setMinimumWidth(54)
            b.clicked.connect(lambda _c=False, r=s: self._set_speed_live(r)); pr.addWidget(b, i // 4, i % 4)
        spv.addLayout(pr)
        cr = QtWidgets.QHBoxLayout()
        self.btn_cycle = QtWidgets.QPushButton("Cycle next ▶"); self.btn_cycle.clicked.connect(self._cycle_speed)
        b_dn = QtWidgets.QPushButton("−100"); b_dn.clicked.connect(lambda: self._nudge_speed(-100))
        b_up = QtWidgets.QPushButton("+100"); b_up.clicked.connect(lambda: self._nudge_speed(100))
        cr.addWidget(self.btn_cycle, 1); cr.addWidget(b_dn); cr.addWidget(b_up); spv.addLayout(cr)
        cr2 = QtWidgets.QHBoxLayout()
        self.sb_live = QtWidgets.QSpinBox(); self.sb_live.setRange(0, fwg.RPM_CEILING)
        self.sb_live.setSingleStep(50); self.sb_live.setSuffix(" rpm"); self.sb_live.setValue(800)
        self._guard_wheel(self.sb_live)
        b_set = QtWidgets.QPushButton("Set"); b_set.clicked.connect(lambda: self._set_speed_live(self.sb_live.value()))
        cr2.addWidget(QtWidgets.QLabel("Custom")); cr2.addWidget(self.sb_live, 1); cr2.addWidget(b_set); spv.addLayout(cr2)
        self.lbl_cmd = QtWidgets.QLabel("Commanded: n/a"); self.lbl_cmd.setStyleSheet("color:#9ccc65; font-weight:bold; border:0;")
        spv.addWidget(self.lbl_cmd)
        self.btn_test = QtWidgets.QPushButton("🔬  Test  (live coast probe; not saved)")
        self.btn_test.setToolTip("R&D only: spins to the Custom speed, cuts power, and shows the "
                                 "coast-down time for debugging. Nothing is recorded.")
        self.btn_test.clicked.connect(self._rd_test)
        spv.addWidget(self.btn_test)
        self.lbl_test = QtWidgets.QLabel("Test result: n/a")
        self.lbl_test.setStyleSheet("color:#26c6da; font-weight:bold; border:0;")
        spv.addWidget(self.lbl_test); spv.addStretch(1)
        panel.addWidget(_grp(sp_page, "Speed & live test (R&D)"))

        # Run-metadata field widgets are built HIDDEN (the Test-setup UI is gone: every test
        # is defined in the Coverage page). Defaults feed the label/meta; condition/severity/
        # speed are set per-test by the Coverage runner.
        self._sev_widget = self._sev_label = None
        for fld in self.tax.fields():
            self._make_field_widget(fld)
        if "condition" in self.fw:
            self.fw["condition"][1].currentTextChanged.connect(self._update_severity_visibility)
        self._wire_dynamic_fields()

        # STM32 tools section
        tl_page = QtWidgets.QWidget(); tlv = QtWidgets.QGridLayout(tl_page)
        self.btn_mon = QtWidgets.QPushButton("CubeMonitor"); self.btn_mon.clicked.connect(lambda: self._open_tool("cubemonitor"))
        self.btn_ide = QtWidgets.QPushButton("CubeIDE"); self.btn_ide.clicked.connect(lambda: self._open_tool("cubeide"))
        self.btn_prog = QtWidgets.QPushButton("CubeProgrammer"); self.btn_prog.clicked.connect(lambda: self._open_tool("cubeprogrammer"))
        self.btn_flash = QtWidgets.QPushButton("⚡ Flash"); self.btn_flash.clicked.connect(self._flash)
        self.btn_speeds = QtWidgets.QPushButton("Edit firmware speeds…"); self.btn_speeds.clicked.connect(self._firmware_speeds)
        self.btn_base = QtWidgets.QPushButton("Set health baseline"); self.btn_base.clicked.connect(self._arm_baseline)
        tlv.addWidget(self.btn_mon, 0, 0); tlv.addWidget(self.btn_ide, 0, 1)
        tlv.addWidget(self.btn_prog, 1, 0); tlv.addWidget(self.btn_flash, 1, 1)
        tlv.addWidget(self.btn_speeds, 2, 0, 1, 2); tlv.addWidget(self.btn_base, 3, 0, 1, 2)
        panel.addWidget(_grp(tl_page, "STM32 tools"))

        btn_data = QtWidgets.QPushButton("📁  Open data folder")
        btn_data.clicked.connect(lambda: os.startfile(self.config["data_dir"]))
        panel.addWidget(btn_data)

        self.lbl_read = QtWidgets.QLabel(""); self.lbl_read.setWordWrap(True)
        self.lbl_read.setStyleSheet("font-family:Consolas; font-size:11px; color:#8aa0b4;")
        panel.addWidget(self.lbl_read); panel.addStretch(1)
        self._update_tool_buttons(); self._apply_fixture_ui(); self._update_severity_visibility()

        scroll = QtWidgets.QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        holder = QtWidgets.QWidget(); holder.setLayout(panel); scroll.setWidget(holder)
        scroll.setMinimumWidth(370); scroll.setMaximumWidth(440)
        root.addWidget(scroll, 0)
        return w

    def _wrap(self, layout):
        w = QtWidgets.QWidget(); layout.setContentsMargins(0, 0, 0, 0); w.setLayout(layout); return w

    def _write_live_status(self):
        f = self.last_frame or {}
        connected = bool(self.source) and (not isinstance(self.source, SerialSource) or self.source.connected)
        sweep = getattr(self, "_sweep_running", False)
        self.live.status({
            "connected": connected,
            "port": getattr(self, "_detected_port", None),
            "fixture_mode": self.fixture_mode,
            "current_calibration_valid": bool(self.current_cfg.get("calibration_valid", False)),
            "rpm": f.get("rpm"), "ripple": f.get("ripple_permil"), "hall": f.get("hall"),
            "current_a": f.get("i_dc_a"),
            "current_sensor_mv": f.get("i_adc_factory_mv"),
            "current_adc_raw_sum": f.get("i_adc_raw_sum"),
            "current_adc_raw_count": f.get("i_adc_raw_count"),
            "current_adc_raw_min": f.get("i_adc_raw_min"),
            "current_adc_raw_max": f.get("i_adc_raw_max"),
            "current_adc_raw_sumsq": f.get("i_adc_raw_sumsq"),
            "current_adc_raw_mean": f.get("i_adc_raw_mean"),
            "current_adc_ratio": f.get("i_adc_ratio"),
            "current_adc_factory_mv": f.get("i_adc_factory_mv"),
            "vref_adc_raw_sum": f.get("vref_adc_raw_sum"),
            "vref_adc_raw_count": f.get("vref_adc_raw_count"),
            "vref_adc_raw_sumsq": f.get("vref_adc_raw_sumsq"),
            "vref_adc_raw_mean": f.get("vref_adc_raw_mean"),
            "vref_factory_cal_raw": f.get("vref_factory_cal_raw"),
            "frame_seq": f.get("frame_seq"),
            "pwm_ticks": f.get("pwm_ticks"),
            "current_adc_window_start_us": f.get("i_adc_window_start_us"),
            "current_adc_window_end_us": f.get("i_adc_window_end_us"),
            "current_adc_fail_count": f.get("i_adc_fail_count"),
            "vref_window_start_us": f.get("vref_window_start_us"),
            "vref_window_end_us": f.get("vref_window_end_us"),
            "current_peak_a": f.get("i_dc_peak_a"),
            # Anchor provenance: which hall sector the zero was learned in (must be
            # 3/5/6 = one-sink), and where the rotor is parked right now. Makes the
            # meter's two-level idle (0.053 vs 0.063 A by sector) auditable.
            "current_anchor_hall": f.get("i_dc_anchor_hall"),
            "current_park_hall": f.get("i_dc_park_hall"),
            "current_zero_mv": f.get("i_dc_zero_mv"),
            "accel_rms_mg": f.get("accel_rms_mg"), "accel_pk_mg": f.get("accel_pk_mg"),
            "accel_x_mg": f.get("accel_x_mg"), "accel_y_mg": f.get("accel_y_mg"),
            "accel_z_mg": f.get("accel_z_mg"),
            # on-device predictive fault detector (decision tree in firmware)
            "fault_code": f.get("fault_code"),
            "fault_name": (FAULT_NAMES.get(f.get("fault_code"), "unknown")
                           if f.get("fault_code") is not None else None),
            "temp_c": f.get("temp_motor_c"), "temp_f": f.get("temp_motor_f"),
            "temp_mv": f.get("temp_mv"),
            "step": f.get("step"),
            "adc_vref_raw": f.get("adc_vref_raw"),
            "adc_pc0_mv": f.get("adc_pc0_mv"),
            "frame_integrity_pct": round(self.stats.good_pct, 1) if self.stats.total else None,
            "fps": getattr(self, "_fps_text", None),
            "health": HEALTH_TEXT.get(self.sev.severity) if self.armed else "not armed",
            # A full CYCLE (_cd) is the thing that actually records official runs;
            # _run_active alone made batch runs look idle from the backend all night.
            "run_active": bool(getattr(self, "_run_active", False)
                               or getattr(self, "_cd", None)),
            "cycle_phase": (getattr(self, "_cd", None) or {}).get("phase"),
            "batch_cond": getattr(self, "_batch_cond", None),
            "run_remaining_s": getattr(self, "_run_remaining", None) if getattr(self, "_run_active", False) else None,
            "sweep_active": sweep,
            "sweep_progress": (f"{self._sweep['done']}/{self._sweep['total']}"
                               if sweep and hasattr(self, "_sweep") else None),
            "last_error": getattr(self.source, "last_error", None) if self.source else None,
            "sensor_status": f.get("sensor_status"),
            "frame_age_s": (round(time.monotonic() - self._last_frame_t, 2)
                            if getattr(self, "_last_frame_t", None) else None),
        })

    def _augment_current(self, frame):
        """Use this frame's untouched PA0/VREFINT aggregates for current."""
        if not self.current_cfg.get("enabled", True):
            return

        frame["i_dc_a"] = None
        frame["i_dc_a_signed"] = None
        frame["i_dc_peak_a"] = None
        frame["i_dc_min_a"] = None
        frame["i_dc_zero_mv"] = None
        frame["i_dc_uncalibrated"] = not bool(
            self.current_cfg.get("calibration_valid", False))
        try:
            i_sum = int(frame["i_adc_raw_sum"])
            i_count = int(frame["i_adc_raw_count"])
            vref_sum = int(frame["vref_adc_raw_sum"])
            vref_count = int(frame["vref_adc_raw_count"])
            factory_cal = int(frame["vref_factory_cal_raw"])
        except (KeyError, TypeError, ValueError):
            frame["i_dc_raw_missing"] = True
            return
        if i_count <= 0 or vref_count <= 0 or vref_sum <= 0 or factory_cal <= 0:
            frame["i_dc_raw_missing"] = True
            return

        pa0_mean = i_sum / i_count
        vref_mean = vref_sum / vref_count
        ratio = pa0_mean / vref_mean
        factory_mv = ratio * 3300.0 * factory_cal / 4095.0
        frame["i_adc_raw_mean"] = pa0_mean
        frame["vref_adc_raw_mean"] = vref_mean
        frame["i_adc_ratio"] = ratio
        frame["i_adc_factory_mv"] = factory_mv
        frame["i_dc_raw_missing"] = False

        if not self.current_cfg.get("calibration_valid", False):
            return
        if self.current_cfg.get("calibration_model") != "raw_pa0_vref_affine_v1":
            frame["i_dc_uncalibrated"] = True
            return
        try:
            slope = float(self.current_cfg["raw_ratio_slope_a_per_ratio"])
            intercept = float(self.current_cfg["raw_ratio_intercept_a"])
        except (KeyError, TypeError, ValueError):
            frame["i_dc_uncalibrated"] = True
            return

        # LIVE INTERCEPT RE-ANCHOR
        # Measured 2026-07-30: the raw PA0/VREF ratio's rest level wanders ~5 mV
        # equivalent (~10 mA) over minutes, and this path deliberately has no
        # auto-zero: so ten minutes after a perfect fit the idle read +9.3 mA off.
        # The SLOPE is stable (it is set by the shunt, the amplifier, and the ADC);
        # only the baseline walks. So keep the fitted slope and re-learn the
        # INTERCEPT whenever the motor is commanded off and settled, anchored to the
        # operator's series-meter idle (idle_offset_a): at true idle the app must
        # read exactly that. Guards mirror the proven auto-zero: commanded OFF only
        # (a locked rotor must never re-anchor), 3 s dwell past the coast-down, and
        # 8 consecutive flat ratios so a settling value cannot be latched.
        # Until the first anchor completes, publish NOTHING, an unanchored
        # baseline was how the app once opened showing 1.2 A of pure seed error.
        idle_a = float(self.current_cfg.get("idle_offset_a") or 0.0)
        # AMPS COME FROM THE FILTERED CHANNEL, NOT THE RAW SUM. Compared live
        # 2026-07-30: at the same commanded 2100 rpm the raw-sum ratio delta was
        # 0.0100 in one run and 0.0059 the next (the raw mean aliases the driver's
        # ~20 kHz chopper, so its apparent slope is duty-dependent, its "483 mV/A"
        # was an artifact). The firmware's oversampled+median+EMA i_dc_mv gave
        # 7.0 / 5.98 mV deltas across two independent sessions, and its fitted
        # 263.5 mV/A sits 5% from the parts-derived 250. The raw aggregates stay
        # streamed and logged for diagnostics; they are not the measurement.
        mvpa = float(self.current_cfg.get("mv_per_amp") or 0.0)
        mv_f = float(frame.get("i_dc_mv", 0))
        if abs(mvpa) < 1e-9 or not mv_f:
            frame["i_dc_uncalibrated"] = True
            return
        stopped = (frame.get("rpm", 1) == 0
                   and getattr(self, "_cmd_off", False)
                   and (time.monotonic() - getattr(self, "_cmd_off_since", 0.0)) > 3.0)
        if not hasattr(self, "_anchor_hist"):
            self._anchor_hist = collections.deque(maxlen=8)
        # SECTOR-NORMALIZED ANCHOR (2026-07-30, operator-approved)
        # The motor's Hall sensors are powered whenever the driver has 24 V, and
        # their open-collector outputs sink pull-up current through the sensed path.
        # Parked in hall sector 1/2/4 TWO sensors sink; in 3/5/6 only ONE does. The
        # difference was measured blind over 9 stops (EN released, output stage
        # OFF): every 1/2/4 park read 0.063-0.064 A on the series meter and A0
        # +3 mV; every 3/5/6 park read 0.053 A. Which sector the rotor lands in is
        # chance, so an anchor taken raw in a 1/2/4 park would push every reading
        # of the following run ~10 mA low.
        #
        # FIX: anchor in ANY sector, and NORMALIZE to the one-sink baseline
        # by subtracting the measured sector offset when parked in 1/2/4. No
        # waiting for a lucky park, no held/stale anchor, and the zero always
        # refers to the same background current. The offset lives in the config
        # (hall_sector_offset_mv) with its measurement provenance; re-measure it
        # if the driver or the Hall wiring ever changes.
        _park_hall = frame.get("hall", 0)
        self._park_hall = _park_hall
        _sector_off = (float(self.current_cfg.get("hall_sector_offset_mv", 3.0) or 0.0)
                       if _park_hall in (1, 2, 4) else 0.0)
        if stopped:
            self._anchor_hist.append(mv_f - _sector_off)
        else:
            self._anchor_hist.clear()
        # 8 consecutive flat frames (<=3 mV spread) so a coast-down or a settling
        # baseline cannot be latched; the anchor is the median of the flat window.
        # The window holds sector-NORMALIZED values, so a park that straddles a
        # sector boundary (rotor teetering between codes) still converges.
        if (len(self._anchor_hist) == self._anchor_hist.maxlen
                and (max(self._anchor_hist) - min(self._anchor_hist)) <= 3.0):
            self._anchor_mv = sorted(self._anchor_hist)[len(self._anchor_hist) // 2]
            self._anchor_ok = True
            self._anchor_hall = _park_hall
        frame["i_dc_anchor_hall"] = getattr(self, "_anchor_hall", None)
        frame["i_dc_park_hall"] = _park_hall if stopped else None
        if not getattr(self, "_anchor_ok", False):
            # No 0 A reference yet -> there is no measurement, only seed error.
            # (The app once opened showing 1.2 A because of exactly this window.)
            frame["i_dc_zeroing"] = True
            frame["i_dc_ratio_pending"] = len(self._anchor_hist)
            return
        frame["i_dc_zero_mv"] = round(self._anchor_mv, 1)

        amps = idle_a + (mv_f - self._anchor_mv) / mvpa
        frame["i_dc_a"] = round(amps, 4)
        frame["i_dc_a_signed"] = round(amps, 4)
        frame["i_dc_uncalibrated"] = False
        if "i_dc_peak_mv" in frame and "i_dc_min_mv" in frame:
            ends = (idle_a + (float(frame["i_dc_peak_mv"]) - self._anchor_mv) / mvpa,
                    idle_a + (float(frame["i_dc_min_mv"]) - self._anchor_mv) / mvpa)
            frame["i_dc_peak_a"] = round(max(ends), 4)
            frame["i_dc_min_a"] = round(min(ends), 4)

        # Extrema use the untouched ADC samples from this same frame. Sorting the
        # converted endpoints also supports a physically reversed sense polarity.
        try:
            lo_ratio = float(frame["i_adc_raw_min"]) / vref_mean
            hi_ratio = float(frame["i_adc_raw_max"]) / vref_mean
            endpoints = (slope * lo_ratio + intercept,
                         slope * hi_ratio + intercept)
            frame["i_dc_min_a"] = min(endpoints)
            frame["i_dc_peak_a"] = max(endpoints)
        except (KeyError, TypeError, ValueError):
            pass

    def _augment_temp(self, frame):
        """Convert the NTC divider mV on PA1 into degrees C (beta equation)."""
        if not self.temp_cfg.get("enabled", True) or "temp_mv" not in frame:
            return
        # Firmware self-check: an open or shorted NTC pins the divider to a rail.
        # Report no temperature at all rather than a number the beta equation
        # will happily produce from a rail voltage.
        if "sensor_status" in frame and not decode_status(frame["sensor_status"])["temp"]:
            frame["temp_motor_c"] = None
            frame["temp_motor_f"] = None
            return
        try:
            import math
            mv = float(frame["temp_mv"])
            vin = float(self.temp_cfg.get("vin_v", 3.3)) * 1000.0        # mV
            series = float(self.temp_cfg.get("series_ohm", 10000.0))
            r0 = float(self.temp_cfg.get("r0_ohm", 10000.0))
            beta = float(self.temp_cfg.get("beta", 3550.0))
            t0 = float(self.temp_cfg.get("t0_k", 298.15))
            if mv < 30.0 or mv > vin - 30.0:      # open / shorted probe
                frame["temp_motor_c"] = None
                return
            # circuit: VIN -> series R -> node(PA1) -> NTC -> GND
            r_ntc = series * (mv / (vin - mv))
            temp_k = 1.0 / ((1.0 / t0) + math.log(r_ntc / r0) / beta)
            temp_c = temp_k - 273.15
            frame["temp_motor_c"] = round(temp_c, 2)
            frame["temp_motor_f"] = round(temp_c * 9.0 / 5.0 + 32.0, 1)
        except (TypeError, ValueError, ZeroDivisionError):
            frame["temp_motor_c"] = None
            frame["temp_motor_f"] = None

    def _guard_wheel(self, widget):
        widget.setFocusPolicy(QtCore.Qt.StrongFocus)     # wheel won't focus it
        widget.installEventFilter(self._wheelguard)
        return widget

    def _shrink_combo(self, c):
        """Stop combos from forcing the panel wide (the side-scroll cause)."""
        c.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(8)
        c.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        c.view().setMinimumWidth(220)      # but the dropdown LIST stays readable
        self._guard_wheel(c)

    # field widget factory
    def _make_field_widget(self, fld):
        key, t = fld["key"], fld.get("type", "text")
        if t == "choice":
            c = QtWidgets.QComboBox()               # non-editable: pick from list only
            self._shrink_combo(c)
            opts = [str(o) for o in self.tax.list(fld.get("options", ""))]
            c.addItems(opts)
            dv = fld.get("default")
            if dv is not None:
                i = c.findText(str(dv))
                if i >= 0:
                    c.setCurrentIndex(i)
            self.fw[key] = ("choice", c, fld.get("options", ""))
            return c
        if t == "speed_step":
            c = QtWidgets.QComboBox(); self._shrink_combo(c)
            for s in self.tax.speed_steps():
                c.addItem(f"step {s['step']}: {s['rpm']} rpm", s)
            self.fw[key] = ("speed_step", c, None)
            return c
        if t == "multichoice":
            cont = QtWidgets.QWidget(); g = QtWidgets.QVBoxLayout(cont)
            g.setContentsMargins(0, 0, 0, 0); g.setSpacing(1); checks = []
            for opt in (str(o) for o in self.tax.list(fld.get("options", ""))):
                cb = QtWidgets.QCheckBox(opt); cb.setChecked(opt in DEFAULT_CHANNELS)
                g.addWidget(cb); checks.append(cb)
            self.fw[key] = ("multichoice", cont, checks)
            return cont
        if t == "rpm":
            sb = QtWidgets.QSpinBox()
            sb.setRange(int(fld.get("min", 0)), int(fld.get("max", fwg.RPM_CEILING)))
            sb.setSingleStep(int(fld.get("step", 10)))   # unit shown in the field label, not the box
            try:
                sb.setValue(int(fld.get("default", 500)))
            except (TypeError, ValueError):
                sb.setValue(500)
            self._guard_wheel(sb)
            self.fw[key] = ("rpm", sb, None)
            return sb
        # text / number
        le = QtWidgets.QLineEdit(str(fld.get("default", "")))
        if t == "number":
            le.setPlaceholderText("number")
        self.fw[key] = (t, le, None)
        return le

    def _wire_dynamic_fields(self):
        for key in ("condition", "orientation", "load", "severity", "speed_rpm_nominal"):
            if key in self.fw:
                kind, wdg, _ = self.fw[key]
                if kind == "choice":
                    wdg.currentTextChanged.connect(self._refresh_label_preview)
                elif kind == "rpm":
                    wdg.valueChanged.connect(self._refresh_label_preview)
                else:
                    wdg.textChanged.connect(self._refresh_label_preview)

    # =====================================================  COVERAGE / REQUIREMENTS TAB
    def _build_plan_tab(self):
        w = QtWidgets.QWidget(); v = QtWidgets.QVBoxLayout(w)
        guide = QtWidgets.QLabel(
            "Data-collection requirements; how many runs each category still needs.  Each cell is "
            "have / target (green = complete · amber = partial · grey = none).  "
            "Click any cell to run that test; click the Category/Left column to run the next one needed.")
        guide.setWordWrap(True); guide.setStyleSheet("color:#9fb0bf;"); v.addWidget(guide)

        # dataset + target controls
        ctl = QtWidgets.QHBoxLayout()
        self.cmb_dataset = QtWidgets.QComboBox()
        self.cmb_dataset.addItem("Mounted (baseline)", "mounted_baseline")
        self.cmb_dataset.addItem("Commissioning (unmounted)", "commissioning_unmounted")
        self.cmb_dataset.setCurrentIndex(1 if self.fixture_mode == "commissioning_unmounted" else 0)
        self.cmb_dataset.activated.connect(self._refresh_matrix)
        plan = self.tax.data.get("coverage_plan") or cov.DEFAULT_PLAN
        self.sb_target = QtWidgets.QSpinBox(); self.sb_target.setRange(1, 30)
        self.sb_target.setValue(int(plan.get("runs_per_cell", 3)))
        self.sb_target.setSuffix("  runs/cell"); self._guard_wheel(self.sb_target)
        self.sb_target.valueChanged.connect(self._refresh_matrix)
        ctl.addWidget(QtWidgets.QLabel("Dataset")); ctl.addWidget(self.cmb_dataset)
        ctl.addSpacing(14)
        self.sb_target.setToolTip("Runs required per (category × rpm). Changing it updates the grid.")
        ctl.addWidget(QtWidgets.QLabel("Runs each")); ctl.addWidget(self.sb_target)
        ctl.addStretch(1)
        ctl.addWidget(QtWidgets.QLabel("Tests run from this page; click a cell, or use "
                                       "“▶ Run next test” on the Monitor tab."))
        v.addLayout(ctl)

        # per-category batch runners (2026-07-30, operator-requested)
        # One click records EVERY remaining (rpm x run) for that category back to
        # back: settle -> 2-min record -> coast -> save, then straight into the next
        # cell. Set the fault fixture up once, press its button, walk away.
        # STOP ALL (or any abort) ends the batch; counts refresh after every run.
        bat = QtWidgets.QHBoxLayout()
        bat.addWidget(QtWidgets.QLabel("Run a whole category:"))
        self.batch_btns = {}
        for _cond in (self.tax.data.get("conditions")
                      or cov.DEFAULT_PLAN["conditions"]):
            b = QtWidgets.QPushButton(_cond)
            b.setToolTip(f"Run every remaining {_cond} test back-to-back "
                         "(2-min record each). STOP ALL cancels.")
            b.clicked.connect(lambda _c=False, c=_cond: self._start_batch(c))
            self.batch_btns[_cond] = b
            bat.addWidget(b)
        bat.addStretch(1)
        v.addLayout(bat)

        # editable plan: add a custom rpm column, add a category, or remove an rpm
        ed = QtWidgets.QHBoxLayout()
        ed.addWidget(QtWidgets.QLabel("Edit plan:  rpm"))
        self.sb_addrpm = QtWidgets.QSpinBox(); self.sb_addrpm.setRange(1, int(fwg.RPM_CEILING))
        self.sb_addrpm.setSingleStep(50); self.sb_addrpm.setSuffix(" rpm"); self.sb_addrpm.setValue(1200)
        self._guard_wheel(self.sb_addrpm)
        ed.addWidget(self.sb_addrpm)
        b_addrpm = QtWidgets.QPushButton("＋ add"); b_addrpm.clicked.connect(self._plan_add_rpm)
        b_delrpm = QtWidgets.QPushButton("－ remove"); b_delrpm.clicked.connect(self._plan_remove_rpm)
        ed.addWidget(b_addrpm); ed.addWidget(b_delrpm)
        ed.addSpacing(14); ed.addWidget(QtWidgets.QLabel("category"))
        self.cmb_addcat = QtWidgets.QComboBox(); self.cmb_addcat.setEditable(True)
        self.cmb_addcat.setInsertPolicy(QtWidgets.QComboBox.NoInsert)
        self.cmb_addcat.addItems(self.tax.list("conditions"))
        self.cmb_addcat.setToolTip("Pick an existing condition or TYPE a brand-new category name, then ＋add.")
        self.cmb_addcat.setMinimumWidth(150)
        ed.addWidget(self.cmb_addcat)
        b_addcat = QtWidgets.QPushButton("＋ add"); b_addcat.clicked.connect(self._plan_add_category)
        b_delcat = QtWidgets.QPushButton("－ remove"); b_delcat.clicked.connect(self._plan_remove_category)
        ed.addWidget(b_addcat); ed.addWidget(b_delcat); ed.addStretch(1)
        v.addLayout(ed)

        # overall progress
        pr = QtWidgets.QHBoxLayout()
        self.cov_bar = QtWidgets.QProgressBar(); self.cov_bar.setRange(0, 100); self.cov_bar.setFixedHeight(22)
        self.lbl_cov = QtWidgets.QLabel("")
        self.lbl_cov.setStyleSheet("color:#dbe4ef; font-weight:600;")
        pr.addWidget(self.cov_bar, 1); pr.addWidget(self.lbl_cov)
        v.addLayout(pr)

        # requirements grid (condition x speed)
        self.tbl = QtWidgets.QTableWidget()
        self.tbl.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.tbl.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self.tbl.cellClicked.connect(self._coverage_cell_clicked)
        v.addWidget(self.tbl, 3)

        # to-do list (what's left, most-needed first): double-click an item to run it
        tl = QtWidgets.QLabel("Still to collect; most needed first  (double-click to run):")
        tl.setStyleSheet("color:#8696ad; font-weight:600;"); v.addWidget(tl)
        self.todo = QtWidgets.QListWidget(); v.addWidget(self.todo, 2)
        self.todo.itemDoubleClicked.connect(self._run_todo_item)

        btns = QtWidgets.QHBoxLayout()
        for lab, fn in (("📁 Open data folder", lambda: os.startfile(self.config["data_dir"])),
                        ("📊 Rebuild & open workbooks", self._rebuild_workbooks),
                        ("✔ Verify data authenticity", self._verify_data),
                        ("Edit plan", lambda: os.startfile(
                            os.path.join(self.config["app_dir"], "config", "taxonomy.yaml"))),
                        ("Reload plan", self._reload_taxonomy)):
            b = QtWidgets.QPushButton(lab); b.clicked.connect(fn); btns.addWidget(b)
        btns.addStretch(1); v.addLayout(btns)
        QtCore.QTimer.singleShot(200, self._refresh_matrix)
        return w

    def _refresh_matrix(self):
        """Render the data-collection requirements tracker (have vs target per category)."""
        if not hasattr(self, "tbl"):
            return
        mode = self.cmb_dataset.currentData() if hasattr(self, "cmb_dataset") else self.fixture_mode
        counts = cov.count_official(self.config["data_dir"], mode)
        plan = dict(self.tax.data.get("coverage_plan") or cov.DEFAULT_PLAN)
        plan["runs_per_cell"] = self.sb_target.value()
        rows, overall, todo = cov.build_requirements(counts, plan)
        # Column set = UNION of every speed any condition tests. Cells are placed BY
        # SPEED, not sequentially: with reduced per-condition plans, writing cells
        # left-to-right put loose_mount's 500/1300/2100 under the 300/500/700
        # headers and left "2100" looking untested (operator caught it 2026-07-30).
        speeds = sorted({c["speed"] for r in rows for c in r["cells"]})

        green = QtGui.QColor("#1f9d57"); amber = QtGui.QColor("#b3791a")
        grey = QtGui.QColor("#1a2436"); white = QtGui.QColor("#ffffff"); dim = QtGui.QColor("#6f8096")

        self.tbl.clear()
        self.tbl.setColumnCount(len(speeds) + 2)
        self.tbl.setHorizontalHeaderLabels(["Category"] + [f"{s} rpm" for s in speeds] + ["Left"])
        self.tbl.setRowCount(len(rows))
        for i, row in enumerate(rows):
            cond = row["condition"]; sev = "none" if cond == "healthy" else "mild"
            cat = QtWidgets.QTableWidgetItem("  " + cond)
            cat.setForeground(white)
            cat.setData(QtCore.Qt.UserRole, (cond, None, sev, None))      # None speed = next-needed
            cat.setToolTip(f"Click to run the next needed speed for {cond}")
            self.tbl.setItem(i, 0, cat)
            by_speed = {c["speed"]: c for c in row["cells"]}
            for j, col_sp in enumerate(speeds):
                cell = by_speed.get(col_sp)
                if cell is None:
                    it = QtWidgets.QTableWidgetItem(NO_VALUE)
                    it.setTextAlignment(QtCore.Qt.AlignCenter)
                    it.setForeground(dim)
                    it.setToolTip(f"{cond} is not planned at {col_sp} rpm")
                    self.tbl.setItem(i, j + 1, it)
                    continue
                have, tgt, sp = cell["have"], cell["target"], cell["speed"]
                it = QtWidgets.QTableWidgetItem(f"{have}/{tgt}")
                it.setTextAlignment(QtCore.Qt.AlignCenter)
                if have >= tgt:
                    it.setBackground(green); it.setForeground(white)
                elif have > 0:
                    it.setBackground(amber); it.setForeground(white)
                else:
                    it.setBackground(grey); it.setForeground(dim)
                it.setData(QtCore.Qt.UserRole, (cond, sp, sev, cell["remaining"]))
                it.setToolTip(f"Click to run {cond} @ {sp} rpm  (full-cycle test)")
                self.tbl.setItem(i, j + 1, it)
            left = QtWidgets.QTableWidgetItem(str(row["remaining"]))
            left.setTextAlignment(QtCore.Qt.AlignCenter)
            left.setForeground(white if row["remaining"] else QtGui.QColor("#1f9d57"))
            left.setData(QtCore.Qt.UserRole, (cond, None, sev, None))
            left.setToolTip(f"Click to run the next needed speed for {cond}")
            self.tbl.setItem(i, len(speeds) + 1, left)
        self.tbl.resizeRowsToContents()

        self.cov_bar.setValue(overall["pct"])
        extra = overall["total_recorded"] - overall["have"]
        extra_txt = f"   ·   {extra} extra run(s) at off-plan speeds" if extra > 0 else ""
        self.lbl_cov.setText(
            f"{overall['have']} / {overall['target']} required runs  ·  "
            f"{overall['cells_done']}/{overall['cells_total']} categories complete  ·  "
            f"{overall['remaining']} left{extra_txt}")

        self.todo.clear()
        if not todo:
            self.todo.addItem("✓  All requirements met for this dataset.")
        else:
            for cond, sp, rem in todo:
                it = QtWidgets.QListWidgetItem(f"•  {cond}  @ {sp} rpm:  {rem} run(s) needed")
                sev = "none" if cond == "healthy" else "mild"
                it.setData(QtCore.Qt.UserRole, (cond, sp, sev))
                self.todo.addItem(it)

    def _reload_taxonomy(self):
        self.tax.load(); self.status.showMessage("Taxonomy reloaded (restart to rebuild form).")

    def _fixture_workbook_path(self, subroot):
        name = "UNMOUNTED.xlsx" if subroot == "commissioning_unmounted" else "MOUNTED.xlsx"
        return os.path.join(self.config["data_dir"], name)

    def _rebuild_fixture(self, subroot):
        """Rebuild ONE fixture workbook from its official sessions (called after each run)."""
        from bldc_phm import consolidate as _consol
        try:
            _consol.consolidate_fixture(self.config["data_dir"], subroot,
                                        self._fixture_workbook_path(subroot))
        except FileNotFoundError:
            pass
        except Exception as e:
            self.live.event(f"workbook rebuild failed: {e}")

    def _rebuild_workbooks(self):
        """Rebuild both UNMOUNTED.xlsx and MOUNTED.xlsx from official runs, then open them."""
        from bldc_phm import consolidate as _consol
        built = []
        for sub in ("commissioning_unmounted", "mounted_baseline"):
            try:
                rep = _consol.consolidate_fixture(self.config["data_dir"], sub,
                                                  self._fixture_workbook_path(sub))
                built.append((sub, rep))
            except FileNotFoundError:
                pass
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Rebuild failed", f"{sub}: {e}")
        if not built:
            QtWidgets.QMessageBox.information(self, "Workbooks", "No official runs yet to consolidate.")
            return
        msg = "\n".join(f"{'UNMOUNTED' if s == 'commissioning_unmounted' else 'MOUNTED'}.xlsx: "
                        f"{r['runs']} runs, {r['conditions']} categories" for s, r in built)
        self.status.showMessage("Workbooks rebuilt.")
        QtWidgets.QMessageBox.information(self, "Workbooks rebuilt", msg)
        for s, _ in built:
            try:
                os.startfile(self._fixture_workbook_path(s))
            except Exception:
                pass

    def _verify_data(self):
        """Audit every saved OFFICIAL run for authenticity (real sensor data, not
        fabricated/stuck/simulated). Reports any SIMULATED/SUSPECT runs."""
        from bldc_phm import consolidate as _consol
        rep = _consol.verify_dataset(self.config["data_dir"], rd=False)
        if not rep:
            QtWidgets.QMessageBox.information(self, "Verify data", "No official runs to verify yet."); return
        ok = [r for r in rep if r[1] == "VERIFIED"]
        bad = [r for r in rep if r[1] != "VERIFIED"]
        lines = [f"{v:9s}  {name}" + (f"   ({', '.join(iss)})" if iss else "")
                 for name, v, iss in (bad[:40])]
        head = (f"{len(ok)}/{len(rep)} official runs VERIFIED as real sensor data."
                + ("\n\nFlagged (not genuine / suspect):\n" + "\n".join(lines) if bad
                   else "\n\nAll runs verified; no fabricated/stuck/simulated data."))
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Data authenticity audit")
        box.setIcon(QtWidgets.QMessageBox.Information if not bad else QtWidgets.QMessageBox.Warning)
        box.setText("✓  All official data verified" if not bad else f"⚠  {len(bad)} run(s) need review")
        box.setInformativeText(head); box.exec_()
        self.status.showMessage(f"Verify: {len(ok)}/{len(rep)} official runs authentic"
                                + (f", {len(bad)} flagged." if bad else "."))

    def _export_combined(self):
        """Merge both modes' combined_data.csv into one master CSV (rows are self-describing
        via fixture_state/condition/etc., so a plain concatenation is analysis-ready)."""
        root = self.config["data_dir"]
        out = os.path.join(root, "COMBINED_ALL_DATA.csv")
        srcs = [os.path.join(root, m, "combined_data.csv")
                for m in ("mounted_baseline", "commissioning_unmounted")]
        srcs = [s for s in srcs if os.path.exists(s)]
        if not srcs:
            QtWidgets.QMessageBox.information(self, "Export", "No data yet to combine."); return
        total = 0
        with open(out, "w", newline="") as dst:
            wrote_header = False
            for s in srcs:
                lines = open(s).read().splitlines()
                if not lines:
                    continue
                if not wrote_header:
                    dst.write(lines[0] + "\n"); wrote_header = True
                for ln in lines[1:]:
                    dst.write(ln + "\n"); total += 1
        self.status.showMessage(f"Exported {total} rows -> COMBINED_ALL_DATA.csv")
        QtWidgets.QMessageBox.information(self, "Export combined",
            f"Merged {len(srcs)} dataset(s), {total} rows ->\n{out}\n\n"
            "Filter by fixture_state / condition / speed_setpoint_rpm columns in Excel.")

    # =====================================================  STATUS / TOOLS MONITOR TAB
    def _build_status_tab(self):
        w = QtWidgets.QWidget(); v = QtWidgets.QVBoxLayout(w)
        v.addWidget(QtWidgets.QLabel("<b>Live connection & tool monitor</b>. "
                                     "Verifies every link is connected and reading."))

        # setup verification (run on a new machine to confirm everything's installed)
        gb_setup = QtWidgets.QGroupBox("Setup verification")
        sl = QtWidgets.QVBoxLayout(gb_setup)
        sl.addWidget(QtWidgets.QLabel("Confirm this machine can run, build, flash and record, "
                                      "and install anything missing."))
        sr = QtWidgets.QHBoxLayout()
        b_ver = QtWidgets.QPushButton("🔧  Verify setup"); b_ver.clicked.connect(self._verify_setup)
        b_ver.setStyleSheet("background:#2f7bf6; color:white; font-weight:bold; padding:8px; border:0; border-radius:10px;")
        b_redet = QtWidgets.QPushButton("↻  Re-detect tools"); b_redet.clicked.connect(self._redetect_tools)
        b_redet.setToolTip("Re-scan for STM32 tools; run after installing CubeIDE to confirm it's detected.")
        b_pip = QtWidgets.QPushButton("⬇  Install / repair packages"); b_pip.clicked.connect(self._install_packages)
        b_st = QtWidgets.QPushButton("🌐  Get STM32 tools"); b_st.clicked.connect(self._open_st_downloads)
        b_guide = QtWidgets.QPushButton("❓  Walkthrough"); b_guide.clicked.connect(self._show_guide)
        b_guide.setStyleSheet("background:#7a5cff; color:white; font-weight:bold; padding:8px; border:0; border-radius:10px;")
        for b in (b_ver, b_redet, b_pip, b_st, b_guide):
            sr.addWidget(b)
        sl.addLayout(sr); v.addWidget(gb_setup)

        gb_link = QtWidgets.QGroupBox("Data link")
        fl = QtWidgets.QFormLayout(gb_link)
        self.mon_source = QtWidgets.QLabel(NO_VALUE); self.mon_conn = QtWidgets.QLabel(NO_VALUE)
        self.mon_rate = QtWidgets.QLabel(NO_VALUE); self.mon_good = QtWidgets.QLabel(NO_VALUE)
        self.mon_last = QtWidgets.QLabel(NO_VALUE); self.mon_stlink = QtWidgets.QLabel(NO_VALUE)
        fl.addRow("Source", self.mon_source); fl.addRow("Connected", self.mon_conn)
        fl.addRow("ST-LINK port", self.mon_stlink); fl.addRow("Frame rate", self.mon_rate)
        fl.addRow("Frame integrity", self.mon_good); fl.addRow("Last reading", self.mon_last)
        v.addWidget(gb_link)

        gb_tools = QtWidgets.QGroupBox("STM32 toolchain")
        tl = QtWidgets.QFormLayout(gb_tools)
        self.mon_tools = {}
        for key, lab in (("cubemonitor", "CubeMonitor"), ("cubeide", "CubeIDE"),
                         ("cubeprogrammer", "CubeProgrammer"), ("programmer_cli", "Programmer CLI"),
                         ("gcc", "arm-none-eabi-gcc")):
            lbl = QtWidgets.QLabel(NO_VALUE); lbl.setWordWrap(True); self.mon_tools[key] = lbl
            tl.addRow(lab, lbl)
        v.addWidget(gb_tools)

        gb_ch = QtWidgets.QGroupBox("Channels")
        cl = QtWidgets.QVBoxLayout(gb_ch)
        self.mon_chtable = QtWidgets.QTableWidget(); self.mon_chtable.setColumnCount(3)
        self.mon_chtable.setHorizontalHeaderLabels(["channel", "status", "reading"])
        cl.addWidget(self.mon_chtable); v.addWidget(gb_ch, 1)

        self._populate_tool_status()
        self._build_channel_rows()
        return w

    # sleek modern dialogs (reused by verify / calibration / guide)
    def _busy_dialog(self, text):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowFlags(QtCore.Qt.Dialog | QtCore.Qt.FramelessWindowHint)
        dlg.setStyleSheet("QDialog{background:#10192b; border:1px solid #2f7bf6; border-radius:14px;}")
        lay = QtWidgets.QVBoxLayout(dlg); lay.setContentsMargins(28, 24, 28, 24); lay.setSpacing(12)
        lbl = QtWidgets.QLabel(text); lbl.setStyleSheet("color:#e8eef6; font-size:14px; font-weight:bold; border:0;")
        bar = QtWidgets.QProgressBar(); bar.setRange(0, 0); bar.setTextVisible(False); bar.setFixedHeight(8)
        lay.addWidget(lbl); lay.addWidget(bar)
        dlg.setModal(True); dlg.show(); QtWidgets.QApplication.processEvents()
        return dlg

    def _report_dialog(self, title, ok, headline, sections, note=""):
        """Modern result dialog. sections = [(section_name, [(name, ok, detail), ...]), ...]."""
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle(title)
        dlg.setStyleSheet("QDialog{background:#0f1727;}")
        v = QtWidgets.QVBoxLayout(dlg); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(0)
        hd = QtWidgets.QFrame(); hd.setStyleSheet(f"background:{'#143524' if ok else '#3a2c14'}; border:0;")
        hv = QtWidgets.QVBoxLayout(hd); hv.setContentsMargins(24, 18, 24, 18)
        t = QtWidgets.QLabel(("✓  " if ok else "⚠  ") + headline)
        t.setStyleSheet(f"color:{'#5fe09a' if ok else '#ffce6b'}; font-size:17px; font-weight:bold; border:0;")
        t.setWordWrap(True); hv.addWidget(t); v.addWidget(hd)
        sa = QtWidgets.QScrollArea(); sa.setWidgetResizable(True); sa.setStyleSheet("border:0; background:#0f1727;")
        body = QtWidgets.QWidget(); bl = QtWidgets.QVBoxLayout(body)
        bl.setContentsMargins(24, 16, 24, 16); bl.setSpacing(10)
        for sec, rows in sections:
            s = QtWidgets.QLabel(sec.upper()); s.setStyleSheet("color:#6f8094; font-weight:700; font-size:10px; border:0;")
            bl.addWidget(s)
            for name, rok, det in rows:
                rw = QtWidgets.QHBoxLayout()
                dot = QtWidgets.QLabel("●"); dot.setStyleSheet(f"color:{'#1f9d57' if rok else '#e0653a'}; font-size:13px; border:0;")
                nm = QtWidgets.QLabel(str(name)); nm.setStyleSheet("color:#dbe4ef; border:0;")
                dt = QtWidgets.QLabel(str(det)); dt.setStyleSheet("color:#8696ad; border:0;"); dt.setAlignment(QtCore.Qt.AlignRight)
                rw.addWidget(dot); rw.addWidget(nm); rw.addStretch(1); rw.addWidget(dt)
                bl.addLayout(rw)
        if note:
            n = QtWidgets.QLabel(note); n.setWordWrap(True); n.setStyleSheet("color:#9fb0c4; border:0; padding-top:6px;")
            bl.addWidget(n)
        bl.addStretch(1); sa.setWidget(body); v.addWidget(sa, 1)
        ft = QtWidgets.QHBoxLayout(); ft.setContentsMargins(24, 12, 24, 16); ft.addStretch(1)
        b = QtWidgets.QPushButton("Close"); b.clicked.connect(dlg.accept)
        b.setStyleSheet("background:#2f7bf6; color:white; font-weight:bold; padding:8px 20px; border:0; border-radius:9px;")
        ft.addWidget(b); v.addLayout(ft)
        dlg.resize(560, 580); dlg.exec_()

    def _verify_setup(self):
        """Run the full setup check OFF the UI thread (snappy), then show a sleek report."""
        from bldc_phm import setup_check as sc
        busy = self._busy_dialog("Verifying setup…  (Python · packages · tools · ports)")

        def work():
            tools = STM32Tools(self.config.get("tools"), self.config.get("firmware_elf"))
            return ("ok", sc.run_all(self.config, tools), tools)

        def done(res):
            busy.close()
            if not (isinstance(res, tuple) and res and res[0] == "ok"):
                self.status.showMessage("Verify failed."); return
            _tag, rep, tools = res
            self.tools = tools; self._populate_tool_status(); self._update_tool_buttons()
            miss = sc.missing_packages()
            flash_ok = bool(tools.paths.get("gcc") and tools.paths.get("programmer_cli"))
            ok = (not miss)
            headline = (("Core app READY" if not miss else f"Missing packages: {', '.join(miss)}")
                        + (" · build/flash READY" if flash_ok else " · install STM32CubeIDE for build/flash"))
            self._report_dialog("Setup verification", ok, headline, list(rep.items()),
                                note="Recording also needs the NUCLEO plugged in (ST-LINK) and the firmware flashed.")
        self._vworker = _BgWorker(work, self)
        self._vworker.done.connect(done)
        self._vworker.start()

    def _redetect_tools(self):
        """Re-scan for STM32 tools off-thread so a freshly installed CubeIDE shows up live."""
        busy = self._busy_dialog("Detecting STM32 tools…")

        def work():
            return STM32Tools(self.config.get("tools"), self.config.get("firmware_elf"))

        def done(tools):
            busy.close()
            if not isinstance(tools, STM32Tools):
                self.status.showMessage("Re-detect failed."); return
            before = {k for k, v in self.tools.paths.items() if v}
            self.tools = tools
            self._populate_tool_status(); self._update_tool_buttons()
            new = {k for k, v in tools.paths.items() if v} - before
            self.status.showMessage("✓ Newly detected: " + ", ".join(sorted(new)) if new
                                    else "Tools re-detected; no new installs found.")
        self._tworker = _BgWorker(work, self)
        self._tworker.done.connect(done)
        self._tworker.start()

    def _show_guide(self):
        steps = [
            ("1 · Install", "Run INSTALL.bat once; it auto-installs Python + packages. For flashing, install STM32CubeIDE."),
            ("2 · Connect", "Plug in the NUCLEO (ST-LINK). Connection tab → 'Verify setup' should be green."),
            ("3 · Flash", "Monitor → ⚡ Build · Flash · Run loads the firmware (once per board)."),
            ("4 · Calibrate", "Monitor → 🎯 Calibration check each new day; confirms readings are real."),
            ("5 · Plan", "Coverage tab → set the rpms & categories you want (add custom rpm / create a category)."),
            ("6 · Collect", "Monitor → ▶ Run next test. Click again after each one (2-min full-cycle, auto-saved), or use the per-category batch buttons on Coverage."),
            ("7 · Results", "Coverage → 📊 Rebuild & open workbooks → UNMOUNTED.xlsx / MOUNTED.xlsx."),
        ]
        self._report_dialog("How to use the BLDC Motor Bench", True, "Bench-test workflow: 7 steps",
                            [("Walkthrough", [(t, True, d) for t, d in steps])],
                            note="R&D probing: Speed → 🔬 Test (live, never saved).  STOP ALL aborts anything safely.")

    def _install_packages(self):
        req = os.path.join(self.config["root_dir"], "requirements.txt")
        self._run_proc_chain([(sys.executable, ["-m", "pip", "install", "-r", req],
                               "Install Python packages")],
                             "Install / repair Python packages",
                             preamble="Installing from requirements.txt … (restart the app after)")

    def _open_st_downloads(self):
        from bldc_phm import setup_check as sc
        url = list(sc.ST_DOWNLOADS.values())[0]      # CubeIDE = gcc + Programmer + ST-LINK driver
        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))
        self.status.showMessage("Opened the STM32CubeIDE download page "
                                "(installs arm-none-eabi-gcc + Programmer + ST-LINK driver).")

    def _populate_tool_status(self):
        for key, lbl in self.mon_tools.items():
            p = self.tools.paths.get(key)
            lbl.setText(("✓ " + p) if p else "✗ not found")
            lbl.setStyleSheet("color:#8fdca0;" if p else "color:#ef6c00;")

    def _build_channel_rows(self):
        """Build the channel table once (rows + status colour). Cheap reading-state
        updates happen in _update_channel_reading."""
        hk = self._load_hookups()
        t = self.mon_chtable; t.setRowCount(len(hk)); self._mon_ch_keys = []
        for i, (ch, d) in enumerate(hk.items()):
            st = d.get("status", "")
            it0 = QtWidgets.QTableWidgetItem(ch); it1 = QtWidgets.QTableWidgetItem(st)
            it1.setBackground(QtCore.Qt.darkGreen if st == "live" else QtCore.Qt.darkYellow)
            it2 = QtWidgets.QTableWidgetItem("")
            for it in (it0, it1, it2):
                it.setTextAlignment(QtCore.Qt.AlignCenter)
            t.setItem(i, 0, it0); t.setItem(i, 1, it1); t.setItem(i, 2, it2)
            self._mon_ch_keys.append((ch, st))
        t.resizeColumnsToContents()
        self._update_channel_reading()

    def _update_channel_reading(self):
        live_keys = set(c for c in self._read_field("channels_enabled").split("|") if c)
        flowing = bool(self.source) and self.last_frame is not None
        t = self.mon_chtable
        for i, (ch, st) in enumerate(getattr(self, "_mon_ch_keys", [])):
            reading = (st == "live" and ch in live_keys and flowing)
            txt = "reading" if reading else ("idle" if st == "live" else NO_VALUE)
            it = t.item(i, 2)
            if it is not None and it.text() != txt:      # only touch the DOM on change
                it.setText(txt)

    def _update_monitor(self):
        # Only when the Status tab is actually visible -> no port scan / table work otherwise.
        if not hasattr(self, "mon_source") or self.tabs.currentIndex() != 1:
            return
        is_ser = isinstance(self.source, SerialSource)
        self.mon_source.setText("Serial COM (ST-LINK)" if self.source else NO_VALUE)
        connected = (self.source is not None and (not is_ser or self.source.connected))
        self.mon_conn.setText("● yes" if connected else "○ no")
        self.mon_conn.setStyleSheet("color:#8fdca0;" if connected else "color:#ef6c00;")
        # ST-LINK: if connected we already know the port; else rescan only every ~4 s (slow call)
        self._mon_calls = getattr(self, "_mon_calls", 0) + 1
        if is_ser and connected:
            self.mon_stlink.setText(getattr(self, "_detected_port", None) or NO_VALUE)
        else:
            if self._mon_calls % 8 == 1:
                self._stlink_cache = find_stlink_port()
            self.mon_stlink.setText(getattr(self, "_stlink_cache", None) or "not detected")
        self.mon_rate.setText(self._frame_rate_text() + " frames/s")
        self.mon_good.setText(f"{self.stats.good_pct:0.1f}%  ({self.stats.good}/{self.stats.total})"
                              if self.stats.total else NO_VALUE)
        if self.last_frame:
            f = self.last_frame
            self.mon_last.setText(f"rpm {f['rpm']}  ripple {f['ripple_permil']}  hall {f['hall']}")
        self._update_channel_reading()

    def _frame_rate_text(self):
        """Frames/s, recomputed at most ~2.5x/s and cached (shared by Monitor + Status)."""
        now = time.monotonic(); total = self.stats.total
        # BUG (fixed 2026-07-30): _fps_t was only ever assigned INSIDE the dt>=0.4
        # branch, while the getattr default above fell back to `now`. So dt was 0 on
        # every call, the branch never ran, _fps_t was never set, and the card read a
        # permanent "0.0" no matter how fast frames were arriving. Seed the window on
        # the first call so there is a real reference point to measure against.
        if not hasattr(self, "_fps_t"):
            self._fps_t = now
            self._fps_n = total
            return NO_VALUE
        dt = now - self._fps_t
        if dt >= 0.4:
            self._fps_text = f"{(total - self._fps_n) / dt:0.1f}"
            self._fps_t = now; self._fps_n = total
        return getattr(self, "_fps_text", NO_VALUE)

    def _on_tab_changed(self, idx):
        if idx == 0:                      # Monitor -> refresh the "Run next test" label
            self._update_runnext_label()
        elif idx == 1:                    # Status/Tools -> rebuild rows + refresh now
            self._build_channel_rows(); self._update_monitor()
        elif idx == 2:                    # Coverage -> refresh the requirements grid
            self._refresh_matrix()

    # =====================================================  HOOKUPS TAB
    def _load_hookups(self):
        # cached in memory; invalidated on Save (avoids a disk read every monitor cycle)
        if getattr(self, "_hk_cache", None) is not None:
            return self._hk_cache
        path = os.path.join(self.config["app_dir"], "config", "hookups.yaml")
        if not os.path.exists(path):
            self._hk_cache = {}
        else:
            with open(path, encoding="utf-8") as f:
                self._hk_cache = (yaml.safe_load(f) or {}).get("channels", {})
        return self._hk_cache

    def _build_hookups_tab(self):
        w = QtWidgets.QWidget(); v = QtWidgets.QVBoxLayout(w)
        head = QtWidgets.QLabel(
            "Editable pin map for every channel. If your wiring changes, edit the cell here "
            "and click Save; the app reads pins from this file, so a pin change never makes it "
            "hard-fail. green = live, yellow = planned (confirm planned pins in CubeMX). "
            "Detail + safety notes in docs/WIRING_FUTURE.md.")
        head.setWordWrap(True); head.setStyleSheet("color:#9fb0bf;"); v.addWidget(head)

        self.hk_cols = ["channel", "status", "signal", "nucleo_pin", "bus", "connector",
                        "sensor", "conditioning", "note"]
        hk = self._load_hookups()
        t = QtWidgets.QTableWidget(); self.hk_table = t
        t.setColumnCount(len(self.hk_cols))
        t.setHorizontalHeaderLabels([c.replace("_", " ") for c in self.hk_cols])
        t.setRowCount(len(hk)); t.setWordWrap(True)
        t.itemChanged.connect(self._hk_item_changed)
        for i, (ch, d) in enumerate(hk.items()):
            vals = [ch] + [str(d.get(c, "")) for c in self.hk_cols[1:]]
            for j, val in enumerate(vals):
                it = QtWidgets.QTableWidgetItem(val)
                if j == 0:                       # channel key is read-only
                    it.setFlags(it.flags() & ~QtCore.Qt.ItemIsEditable)
                if j == 1:
                    it.setBackground(QtCore.Qt.darkGreen if val == "live" else QtCore.Qt.darkYellow)
                t.setItem(i, j, it)
        t.resizeColumnsToContents(); t.horizontalHeader().setStretchLastSection(True)
        v.addWidget(t, 1)

        row = QtWidgets.QHBoxLayout()
        bsave = QtWidgets.QPushButton("Save pin map"); bsave.setStyleSheet("font-weight:bold;")
        bsave.clicked.connect(self._save_hookups)
        bedit = QtWidgets.QPushButton("Open hookups.yaml"); bedit.clicked.connect(
            lambda: os.startfile(os.path.join(self.config["app_dir"], "config", "hookups.yaml")))
        row.addWidget(bsave); row.addWidget(bedit); row.addStretch(1); v.addLayout(row)
        return w

    def _hk_item_changed(self, item):
        # keep the status cell color in sync when edited
        if item.column() == 1:
            item.setBackground(QtCore.Qt.darkGreen if item.text() == "live" else QtCore.Qt.darkYellow)

    def _save_hookups(self):
        t = self.hk_table
        channels = {}
        for i in range(t.rowCount()):
            ch_item = t.item(i, 0)
            ch = ch_item.text().strip() if ch_item else ""
            if not ch:
                continue
            d = {}
            for j, col in enumerate(self.hk_cols[1:], start=1):
                cell = t.item(i, j)
                d[col] = cell.text() if cell else ""
            channels[ch] = d
        path = os.path.join(self.config["app_dir"], "config", "hookups.yaml")
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"channels": channels}, f, sort_keys=False, allow_unicode=True, width=100)
        self._hk_cache = None             # invalidate cache so the monitor picks up changes
        self.status.showMessage(f"Pin map saved ({len(channels)} channels) -> hookups.yaml")
        self._check_hookups()

    def _check_hookups(self):
        """Warn (in the status bar) if a taxonomy channel has no hookup entry."""
        hk = set(self._load_hookups().keys())
        declared = set(str(c) for c in self.tax.list("channels"))
        missing = declared - hk
        if missing:
            self.status.showMessage("WARNING: channels with no hookup entry: "
                                    + ", ".join(sorted(missing)))

    # =====================================================  field read/write
    def _read_field(self, key):
        if key not in self.fw:
            return ""
        kind, w, extra = self.fw[key]
        if kind == "choice":
            return w.currentText()
        if kind == "rpm":
            return str(w.value())
        if kind == "speed_step":
            d = w.currentData(); return str(d["step"]) if d else ""
        if kind == "multichoice":
            return "|".join(cb.text() for cb in extra if cb.isChecked())
        return w.text()

    def _set_field(self, key, val):
        if key not in self.fw:
            return
        kind, w, extra = self.fw[key]
        if kind == "choice":
            i = w.findText(str(val))
            if i < 0:                       # template value not in list -> add it (combo is non-editable)
                w.addItem(str(val)); i = w.findText(str(val))
            w.setCurrentIndex(i)
        elif kind == "rpm":
            try:
                w.setValue(int(float(val)))
            except (TypeError, ValueError):
                pass
        elif kind == "speed_step":
            for idx in range(w.count()):
                if str(w.itemData(idx)["rpm"]) == str(val) or str(w.itemData(idx)["step"]) == str(val):
                    w.setCurrentIndex(idx); break
        elif kind == "multichoice":
            want = set(str(val).split("|"))
            for cb in extra:
                cb.setChecked(cb.text() in want)
        else:
            w.setText(str(val))

    def _apply_template(self):
        name = self.cmb_template.currentText()
        tpl = self.tax.test_templates().get(name, {})
        for k, v in tpl.items():
            self._set_field(k, v)
        self._refresh_label_preview()
        if tpl:
            self.status.showMessage(f"Applied template: {name}")

    def _label_fields(self):
        # only the fields the label template actually uses (keeps the label clean + connected)
        return {
            "condition": self._read_field("condition"),
            "severity": self._read_field("severity"),
            "orientation": self._read_field("orientation"),
            "load": self._read_field("load"),
            "rpm": self._read_field("speed_rpm_nominal"),
        }

    def _refresh_label_preview(self):
        if hasattr(self, "lbl_label"):     # the label widget was removed with the Test-setup UI
            self.lbl_label.setText(self.tax.make_label(self._label_fields()))

    def _update_severity_visibility(self):
        """Severity is for faults only; hide it (and force 'none') when condition=healthy."""
        healthy = self._read_field("condition") == "healthy"
        if self._sev_widget is not None:
            self._sev_widget.setVisible(not healthy)
        if self._sev_label is not None:
            self._sev_label.setVisible(not healthy)
        if healthy:
            self._set_field("severity", "none")

    def _persist_new_combo_values(self):
        for key, (kind, w, extra) in self.fw.items():
            if kind == "choice" and extra:
                self.tax.add_value(extra, w.currentText())

    def _collect_meta(self):
        meta = {f["key"]: self._read_field(f["key"]) for f in self.tax.fields()}
        meta["label"] = self.tax.make_label(self._label_fields())
        meta["motor_model"] = self.config.get("motor", {}).get("model", "42BL41.010 Rev.B")
        _sub, fstate, valid, use = FIXTURE_MODES[self.fixture_mode]
        meta["fixture_state"] = fstate
        meta["valid_for_classifier"] = valid
        meta["dataset_use"] = use
        # SimulatorSource no longer exists; it was deleted so that no code path can
        # produce synthetic data. The only source is the physical board.
        meta["source"] = "serial"
        return meta

    # =====================================================  fixture toggle (buttons)
    def _set_fixture(self, mode):
        if (self.session.active or getattr(self, "_run_active", False)
                or getattr(self, "_sweep_running", False) or getattr(self, "_cd", None)
                or getattr(self, "_cal", None) or getattr(self, "_batch_cond", None)):
            QtWidgets.QMessageBox.information(self, "Busy",
                "Stop the current run (STOP ALL) before switching fixture.")
            self._apply_fixture_ui(); return
        self.fixture_mode = mode
        self.session.set_subroot(FIXTURE_MODES[mode][0])
        self._apply_fixture_ui()
        self._refresh_matrix()
        self.live.event(f"fixture -> {mode}")
        self.status.showMessage(f"Fixture: {mode}  ->  data/{FIXTURE_MODES[mode][0]}/")

    def _apply_fixture_ui(self):
        sub = FIXTURE_MODES[self.fixture_mode][0]
        un = self.fixture_mode == "commissioning_unmounted"
        self.btn_unmounted.setChecked(un); self.btn_mounted.setChecked(not un)
        if un:
            self.lbl_fixture.setText("Unmounted; commissioning only (not for classifier)")
            self.lbl_fixture.setStyleSheet("border:0; color:#d4a017; font-size:11px;")
        else:
            self.lbl_fixture.setText("Mounted; baseline data (valid for classifier)")
            self.lbl_fixture.setStyleSheet("border:0; color:#8fdca0; font-size:11px;")
        self._update_runnext_label()

    # =====================================================  source / record
    def _set_sev_label(self, sev):
        if hasattr(self, "val_health"):
            self.val_health.setText(HEALTH_TEXT[sev])
            self.val_health.setStyleSheet(_chip_css(CHIP_COLOR[sev]))

    def _rescan_ports(self):
        """Auto-detect the NUCLEO ST-LINK (no manual port picking)."""
        port = find_stlink_port()
        self._detected_port = port
        if self.source is not None:
            return                                  # don't disturb an active connection label
        if port:
            self.lbl_hw.setText(f"●  ST-LINK detected on {port}")
            self.lbl_hw.setStyleSheet("border:0; color:#8fdca0;")
            self.btn_conn.setEnabled(True)
        else:
            others = [d for d, _, _ in list_serial_ports()]
            extra = f"  (other ports: {', '.join(others)})" if others else ""
            self.lbl_hw.setText("○  No ST-LINK found; plug in the NUCLEO (USB), then Rescan" + extra)
            self.lbl_hw.setStyleSheet("border:0; color:#ef6c00;")
            self.btn_conn.setEnabled(False)

    def _update_tool_buttons(self):
        for key, btn in (("cubemonitor", self.btn_mon), ("cubeide", self.btn_ide),
                         ("cubeprogrammer", self.btn_prog)):
            ok = self.tools.available(key)
            btn.setEnabled(ok)
            btn.setToolTip(self.tools.paths.get(key) or f"{key} not found (set path in app_config.yaml)")
        cli, info = self.tools.flash_command()
        self.btn_flash.setEnabled(bool(cli))
        self.btn_flash.setToolTip("Flash motor.elf over ST-LINK (port=SWD mode=UR -rst)"
                                  if cli else str(info))

    def _open_tool(self, key):
        # ST-LINK is one USB device -> free the serial port before a tool that uses it.
        if isinstance(self.source, SerialSource):
            self._disconnect()
            self.status.showMessage(f"Released serial port for {key} (ST-LINK handoff).")
        ok, msg = self.tools.open(key)
        self.status.showMessage(msg)
        if not ok:
            QtWidgets.QMessageBox.warning(self, "STM32 tool", msg)

    def _flash(self):
        cli, args = self.tools.flash_command()
        if cli is None:
            QtWidgets.QMessageBox.warning(self, "Flash", str(args)); return
        if QtWidgets.QMessageBox.question(
                self, "Flash firmware",
                "Flash this firmware to the NUCLEO-F401RE over ST-LINK?\n\n"
                f"{args[-2]}\n\nThe app will release the serial port; the motor will reset."
                ) != QtWidgets.QMessageBox.Yes:
            return
        if isinstance(self.source, SerialSource):      # free the ST-LINK for SWD
            self._disconnect()

        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Flashing over ST-LINK…")
        lay = QtWidgets.QVBoxLayout(dlg)
        self._flash_log = QtWidgets.QPlainTextEdit(); self._flash_log.setReadOnly(True)
        self._flash_log.setMinimumSize(580, 360)
        self._flash_log.setStyleSheet("font-family:Consolas; font-size:11px;")
        lay.addWidget(self._flash_log); dlg.show(); self._flash_dlg = dlg
        self.btn_flash.setEnabled(False)

        self.flash_proc = QtCore.QProcess(self)
        self.flash_proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.flash_proc.readyReadStandardOutput.connect(self._flash_read)
        self.flash_proc.finished.connect(self._flash_done)
        self._flash_log.appendPlainText(f"$ {cli} {' '.join(args)}\n")
        self.flash_proc.start(cli, args)

    def _flash_read(self):
        txt = bytes(self.flash_proc.readAllStandardOutput()).decode("ascii", "ignore")
        if txt.strip():
            self._flash_log.appendPlainText(txt.rstrip())

    def _flash_done(self, code, _status):
        ok = (code == 0)
        self._flash_log.appendPlainText("\n=== " + ("SUCCESS" if ok else f"FAILED (exit {code})") + " ===")
        self.status.showMessage("Flash " + ("OK; reconnect serial to stream." if ok else "FAILED"))
        self._update_tool_buttons()

    # =====================================================  firmware speeds -> build -> flash
    def _firmware_speeds(self):
        fw_dir = self.config.get("firmware_dir", "")
        gcc = self.tools.paths.get("gcc")
        cli = self.tools.paths.get("programmer_cli")
        if not (fw_dir and os.path.isdir(fw_dir)):
            QtWidgets.QMessageBox.warning(self, "Firmware", f"firmware_dir not found:\n{fw_dir}"); return
        # code-update works with no toolchain; build/flash buttons disable if tools missing

        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Set firmware speeds → build → flash")
        v = QtWidgets.QVBoxLayout(dlg)
        v.addWidget(QtWidgets.QLabel(
            f"Up to 5 speed steps the USER button cycles (6th = OFF). Any rpm 0-{fwg.RPM_CEILING}. "
            "TIM3_CH1 PWM duty (1000 ticks = 100%) is shown live."))
        spins, pwm_lbls = [], []
        grid = QtWidgets.QGridLayout()
        defaults = [500, 800, 1100, 1400, 1700]
        for i in range(5):
            sb = QtWidgets.QSpinBox(); sb.setRange(0, fwg.RPM_CEILING); sb.setSingleStep(10)
            sb.setSuffix(" rpm"); sb.setValue(defaults[i])
            dl = QtWidgets.QLabel()
            def mk(sbx, dlx):
                return lambda: dlx.setText(f"PWM {fwg.rpm_to_pwm(sbx.value())}/1000")
            upd = mk(sb, dl); sb.valueChanged.connect(upd); upd()
            grid.addWidget(QtWidgets.QLabel(f"Step {i}"), i, 0); grid.addWidget(sb, i, 1); grid.addWidget(dl, i, 2)
            spins.append(sb); pwm_lbls.append(dl)
        v.addLayout(grid)
        note = QtWidgets.QLabel(
            "“Update code” rewrites firmware/speeds_gen.h only (real C source, no flash). "
            "“+ build” compiles motor.elf with the bundled gcc. “+ flash” also programs the "
            "board over ST-LINK (releases the serial port first).")
        note.setWordWrap(True); note.setStyleSheet("color:#9fb0bf;"); v.addWidget(note)

        result = {"mode": None}
        rb = QtWidgets.QHBoxLayout()
        b_code = QtWidgets.QPushButton("Update code only")
        b_build = QtWidgets.QPushButton("Update + build")
        b_flash = QtWidgets.QPushButton("Update + build + flash")
        b_cancel = QtWidgets.QPushButton("Cancel")
        for b in (b_code, b_build, b_flash, b_cancel):
            rb.addWidget(b)
        v.addLayout(rb)
        if not gcc:
            b_build.setEnabled(False); b_flash.setEnabled(False)
            b_build.setToolTip("arm-none-eabi-gcc not found"); b_flash.setToolTip("gcc not found")
        if not cli:
            b_flash.setEnabled(False); b_flash.setToolTip("STM32_Programmer_CLI not found")

        def choose(m):
            result["mode"] = m; dlg.accept()
        b_code.clicked.connect(lambda: choose("code"))
        b_build.clicked.connect(lambda: choose("build"))
        b_flash.clicked.connect(lambda: choose("flash"))
        b_cancel.clicked.connect(dlg.reject)

        if dlg.exec_() != QtWidgets.QDialog.Accepted or not result["mode"]:
            return
        self._apply_speeds(fw_dir, gcc, cli, [s.value() for s in spins], result["mode"])

    def _apply_speeds(self, fw_dir, gcc, cli, rpms, mode):
        header, codes = fwg.write_speed_table(fw_dir, rpms)   # update the code (real source)
        if mode == "code":
            ans = QtWidgets.QMessageBox.question(
                self, "Firmware code updated",
                f"Updated firmware source (no build/flash):\n{header}\n\nPWM ticks: {codes}\n\n"
                "Open the file to view the change?")
            if ans == QtWidgets.QMessageBox.Yes:
                os.startfile(header)
            self.status.showMessage(f"Firmware code updated: speeds {rpms} -> PWM {codes[:5]}")
            return
        if isinstance(self.source, SerialSource):
            self._disconnect()                                # free the ST-LINK for SWD
        _g, build_args, elf = fwg.build_command(gcc, fw_dir)
        steps = [(gcc, build_args, "BUILD (arm-none-eabi-gcc)")]
        if mode == "flash":
            steps.append((cli, ["-c", "port=SWD", "mode=UR", "-w", elf, "-rst"],
                          "FLASH (STM32_Programmer_CLI)"))
        self._run_proc_chain(steps, f"Speeds {rpms} ({mode})",
                             preamble=f"Wrote {os.path.basename(header)}: PWM ticks {codes}")

    # =====================================================  live speed + auto sweep
    def _cmd_speed(self, rpm):
        """THE single place the motor speed is commanded; every send routes here."""
        rpm = max(0, int(rpm))
        if not self.source:
            return False
        ok = self.source.send(f"S{rpm}\n")
        if not ok:
            # The write never reached the board (port down / reconnecting). Claiming
            # commanded-off here would let the zero re-anchor against a motor that is
            # actually still spinning under its last real command.
            self.status.showMessage("Speed command NOT sent - serial link down, retrying...")
            self.live.event(f"cmd S{rpm} FAILED (link down)")
            return False
        if hasattr(self, "lbl_cmd"):
            self.lbl_cmd.setText("Commanded: " + ("OFF" if rpm == 0 else f"{rpm} rpm"))
        self._cmd_off = (rpm == 0)
        self._cmd_off_since = time.monotonic()
        return ok

    def _set_speed_live(self, rpm):
        """Send a speed live (one click). Updates the speed field + the commanded readout."""
        # An official cycle OWNS the motor: a preset/custom speed click mid-recording
        # changed the speed under a labeled run (observed live: a rotor_drag@300 run
        # spinning at 513 because 500 was clicked during it). The cycle engine
        # commands speed via _cmd_speed directly, so this guard only blocks humans.
        cd = getattr(self, "_cd", None)
        if cd and cd.get("kind") == "full":
            self.status.showMessage("Recording in progress - STOP ALL first to change speed.")
            return
        rpm = max(0, int(rpm))
        if "speed_rpm_nominal" in self.fw and rpm > 0:
            self._set_field("speed_rpm_nominal", rpm)
        if hasattr(self, "sb_live"):
            self.sb_live.blockSignals(True)
            self.sb_live.setValue(min(rpm, self.sb_live.maximum()))
            self.sb_live.blockSignals(False)
        if not self.source:
            self.status.showMessage("Connect a source first."); return
        self._cmd_speed(rpm)
        self.status.showMessage(
            f"Speed → {'OFF' if rpm == 0 else str(rpm) + ' rpm'}  "
            "(needs the RX firmware flashed once; else use the board button)")

    def _nudge_speed(self, delta):
        self._set_speed_live(self.sb_live.value() + delta)

    def _cycle_speed(self):
        presets = getattr(self, "_presets", [0, 500, 800, 1100, 1400, 1700])
        self._cycle_idx = (getattr(self, "_cycle_idx", -1) + 1) % len(presets)
        self._set_speed_live(presets[self._cycle_idx])

    # =====================================================  R&D routing + authenticity
    def _session_to(self, rd):
        """Point the session at the R&D folder (data/rd) for debug runs, or back at the
        official fixture folder. Manual runs are R&D; Coverage runs are official."""
        want = "rd" if rd else FIXTURE_MODES[self.fixture_mode][0]
        if not self.session.active and self.session.subroot != want:
            self.session.set_subroot(want)

    def _assess_authenticity(self):
        """Confirm the just-recorded data is REAL sensor output, not fabricated/stuck/simulated.
        Returns (verdict, checks). verdict: VERIFIED | SIMULATED | SUSPECT."""
        serial = isinstance(self.source, SerialSource)
        rpm = np.fromiter(self.buf["rpm"], float)
        rip = np.fromiter(self.buf["ripple_permil"], float)
        hall = np.fromiter(self.buf["hall"], float)
        t = np.fromiter(self.buf["t_ms"], float) / 1000.0
        e = np.fromiter(self.buf["edges"], float)
        checks = []
        checks.append(("Real sensor source (not simulator)", serial,
                       "serial / ST-LINK" if serial else "SIMULATOR"))
        nd = len(set(int(h) for h in hall)) if len(hall) else 0
        checks.append(("Hall states cycling (not stuck)", nd >= 4, f"{nd} distinct states"))
        ok_e, det = False, "insufficient data"
        if len(t) >= 20 and t[-1] > t[0] and rpm.mean() > 0:
            meas = (e[-1] - e[0]) / (t[-1] - t[0]); exp = rpm.mean() * 24 / 60
            err = abs(meas - exp) / max(exp, 1) * 100; ok_e = err <= 20
            det = f"edges {meas:.0f}/s vs rpm-implied {exp:.0f}/s ({err:.0f}%)"
        checks.append(("RPM backed by real Hall edges", ok_e, det))
        rmed = float(np.median(rip)) if len(rip) else 0
        checks.append(("Ripple physically sane", 0 < rmed < 800, f"median {rmed:.0f}‰"))
        std = float(np.std(rpm)) if len(rpm) > 10 else 0
        checks.append(("Live variation present (not frozen)", std > 0, f"rpm std {std:.2f}"))
        integ = self.stats.good_pct if self.stats.total else 0.0
        checks.append(("Frame integrity ≥ 95%", integ >= 95, f"{integ:0.1f}%"))
        if not serial:
            verdict = "SIMULATED"
        elif all(ok for _, ok, _ in checks):
            verdict = "VERIFIED"
        else:
            verdict = "SUSPECT"
        return verdict, checks

    # =====================================================  full-cycle run engine
    def _begin_cycle(self, kind, condition, speed, severity, hold, rd, save=True):
        """Spin → record(steady) → cut → coast engine.
        kind='full' (official category run, saved) or 'probe' (live R&D test, NOT recorded)."""
        if getattr(self, "_sweep_running", False) or getattr(self, "_cd", None) \
                or getattr(self, "_cal", None) or self.session.active:
            QtWidgets.QMessageBox.information(self, "Busy", "Stop the current run first."); return False
        if not self.source:
            QtWidgets.QMessageBox.information(self, "Connect first", "Connect a source first."); return False
        try:
            speed = int(speed)
        except (TypeError, ValueError):
            speed = 0
        if speed < 200:
            QtWidgets.QMessageBox.information(self, "Pick a speed",
                "Need ≥ 200 rpm so there's momentum to record and coast."); return False
        if condition and "condition" in self.fw:
            self._set_field("condition", condition)
        if "severity" in self.fw:
            self._set_field("severity", severity or "none")
        self._set_field("speed_rpm_nominal", speed)
        self._update_severity_visibility()
        head = "Test" if kind == "probe" else f"{condition} {speed}rpm"
        # PRE-ZERO (2026-07-30, operator-diagnosed): a cycle used to command the
        # target speed IMMEDIATELY, recording against whatever zero anchor existed,
        # stale by minutes if the operator had been live-testing first. Live tests
        # "worked" precisely because a human idles at rest before spinning, letting
        # the anchor refresh; Coverage never did. Every official run now starts with
        # a commanded stop long enough for the anchor to re-learn (stop ~1 s + 3 s
        # dwell + 8 flat frames), so the zero is NEVER older than 10 s at record.
        self._cd = {"kind": kind, "head": head, "target": speed, "phase": "prezero",
                    "left": 10,
                    "hold": int(hold), "timeout": 25, "rpm_at_cut": None, "rd": rd, "save": save,
                    "condition": condition, "severity": severity,
                    "t0": time.monotonic(), "total": 10 + 5 + int(hold) + 12}
        for b in ("btn_test", "btn_rec", "btn_cal"):
            if hasattr(self, b):
                getattr(self, b).setEnabled(False)
        if hasattr(self, "run_banner"):
            self.run_banner.setVisible(True)
        self._cmd_speed(0)                      # pre-zero: fresh anchor before the spin
        self.live.event(f"CYCLE start [{kind}] | {condition} {speed}rpm | hold {hold}s | save={save}")
        self._cd_timer = QtCore.QTimer(self); self._cd_timer.timeout.connect(self._coastdown_tick)
        self._cd_timer.start(1000)
        return True

    def _rd_test(self):
        """Live R&D probe (Speed panel 'Test' button): spin → brief hold → cut → show coast
        time. NOT recorded, pure live debugging."""
        cond = self._read_field("condition") or "healthy"
        self._begin_cycle("probe", cond, self.sb_live.value(), "none", hold=6, rd=True, save=False)

    def _run_category(self, condition, speed, severity="none"):
        """One-click FULL-CYCLE OFFICIAL run: settle → record → coast → save → consolidate.
        Hold length comes from the plan: condition_overrides.<cond>.hold_s if set
        (rotor_drag records 80 s so its five spectrum points fit 10 minutes total),
        else the global FULL_RUN_HOLD_S."""
        plan = self.tax.data.get("coverage_plan") or {}
        ov = (plan.get("condition_overrides") or {}).get(condition) or {}
        hold = int(ov.get("hold_s", FULL_RUN_HOLD_S))
        return self._begin_cycle("full", condition, speed, severity, hold=hold, rd=False, save=True)

    def _coastdown_tick(self):
        cd = getattr(self, "_cd", None)
        if cd is None:
            return
        ph = cd["phase"]; head = cd.get("head", "Run")
        if hasattr(self, "run_banner") and self.run_banner.isVisible():
            el = int(max(0, time.monotonic() - cd.get("t0", time.monotonic())))
            tot = max(1, int(cd.get("total", 1)))
            rem = max(0, tot - el)
            phase_txt = {"prezero": "re-zeroing", "settle": "warming up",
                         "hold": "recording", "coast": "coasting"}.get(ph, ph)
            self.run_clock.setText(f"⏱ {el // 60:02d}:{el % 60:02d} / {tot // 60:02d}:{tot % 60:02d}"
                                   f"   ·   {rem // 60:02d}:{rem % 60:02d} left   ·   {phase_txt}")
            self.run_prog.setValue(min(100, int(100 * el / tot)))
        if ph == "prezero":
            cd["left"] -= 1
            # PARK RE-ROLL (operator-requested 2026-07-30): the first two ticks blip
            # the motor at 300 rpm, then stop, so the rotor re-parks before the
            # zero is learned. A power-down boundary park (hall code contradicting
            # the true sink state) once biased a whole run's current by ~9 mA; the
            # blip re-rolls the dice for EVERY official run in EVERY category.
            if cd["left"] == 9:
                self._cmd_speed(300)
            elif cd["left"] == 7:
                self._cmd_speed(0)
            self.status.showMessage(f"{head}: park re-roll + re-zeroing… {max(0, cd['left'])}s")
            if cd["left"] <= 0:
                # extend once if the anchor has not refreshed (still coasting or
                # baseline unflat); recording against a stale zero was THE original
                # coverage-vs-live bug; never spin until the zero is re-learned.
                lf = self.last_frame or {}
                if not lf.get("i_dc_zero_mv") and not cd.get("prezero_ext"):
                    cd["prezero_ext"] = True
                    cd["left"] = 6
                    cd["total"] += 6
                    return
                self._cmd_speed(cd["target"])
                cd["phase"] = "settle"; cd["left"] = 5
            return
        if ph == "settle":
            cd["left"] -= 1
            self.status.showMessage(f"{head}: settling… {max(0, cd['left'])}s")
            if cd["left"] <= 0:
                self.stats = FrameStats()
                if cd.get("save", True):
                    meta = self._collect_meta()
                    meta["run_kind"] = "rd" if cd.get("rd") else "official"
                    meta["test_kind"] = "full_cycle"
                    self._session_to(cd.get("rd", False))
                    self.session.start(meta)
                cd["phase"] = "hold"; cd["left"] = cd["hold"]
        elif ph == "hold":
            cd["left"] -= 1
            self.status.showMessage(f"{head}: recording… {max(0, cd['left'])}s left")
            if cd["left"] <= 0:
                cd["rpm_at_cut"] = self.last_frame["rpm"] if self.last_frame else cd["target"]
                self._cmd_speed(0); self.lbl_cmd.setText("Commanded: OFF")
                cd["phase"] = "coast"; cd["left"] = cd["timeout"]
                self.status.showMessage(f"{head}: power cut; measuring coast-down…")
        elif ph == "coast":
            cd["left"] -= 1
            rpm_now = self.last_frame["rpm"] if self.last_frame else 0
            coast_ms = self.last_frame.get("coast_ms", 0) if self.last_frame else 0
            if (rpm_now == 0 and coast_ms > 0) or cd["left"] <= 0:
                self._coastdown_finish(coast_ms if coast_ms else None, cd.get("rpm_at_cut"))

    def _coastdown_finish(self, coast_ms, rpm_at_cut):
        cd = getattr(self, "_cd", None) or {}
        kind = cd.get("kind", "full")
        if getattr(self, "_cd_timer", None):
            self._cd_timer.stop()
        res = None; verdict = None
        if self.session.active:
            if self.stats.good < 20:
                self.session.discard()
            else:
                verdict, _ = self._assess_authenticity()
                extra = {"data_source": "serial" if isinstance(self.source, SerialSource) else "unverified",
                         "authenticity": verdict}
                if not cd.get("rd") and verdict != "VERIFIED":
                    extra["valid_for_classifier"] = "false"   # keep unverified data out of the classifier
                # LABEL-vs-MEASUREMENT gate (2026-07-30): S039 recorded a STATIONARY
                # motor under a '2100 rpm' label and still passed authenticity,
                # authenticity proves the data is real, not that it matches its
                # label. A labeled speed the motor never held poisons the training
                # set worse than any sensor fault, so it can never be valid.
                try:
                    _nom = float((cd or {}).get("target") or self._collect_meta().get("speed_rpm_nominal") or 0)
                    _rpms = [f_.get("rpm", 0) for f_ in [self.last_frame] if f_] or [0]
                    _meas = float(self.buf["rpm"][-1]) if self.buf.get("rpm") else _rpms[-1]
                    _mean = (sum(self.buf["rpm"]) / len(self.buf["rpm"])) if self.buf.get("rpm") else 0.0
                    if not cd.get("rd") and _nom > 0 and abs(_mean - _nom) > 0.25 * _nom:
                        extra["valid_for_classifier"] = "false"
                        extra["label_mismatch"] = (f"labeled {_nom:.0f} rpm but measured "
                                                   f"mean {_mean:.0f} rpm over the record window")
                except Exception:
                    pass
                res = self.session.stop(self.stats, extra=extra)
        self._session_to(False)
        self._cd = None
        if hasattr(self, "run_banner"):
            self.run_banner.setVisible(False)
        for b in ("btn_test", "btn_rec", "btn_cal"):
            if hasattr(self, b):
                getattr(self, b).setEnabled(True)
        sid = res["session_id"] if res else None
        coast_txt = f"{coast_ms} ms" if coast_ms else "no stop detected"
        if kind == "probe":
            if hasattr(self, "lbl_test"):
                self.lbl_test.setText(f"Test result: coast {coast_txt}   (live, not saved)")
            self.status.showMessage(f"R&D test: coast {coast_txt}. Motor OFF.")
        else:
            warn = "" if (verdict == "VERIFIED" or not sid) else f"  ⚠ {verdict} (flagged not-for-classifier)"
            self.status.showMessage(
                (f"Saved {sid} · {verdict} · coast {coast_txt}.{warn} Motor OFF." if sid
                 else "Run not saved (too few valid frames). Motor OFF."))
            if res and not cd.get("rd"):
                self._rebuild_fixture(FIXTURE_MODES[self.fixture_mode][0])
        self.live.event(f"CYCLE done [{kind}] | {'saved ' + sid if sid else 'not saved'} | "
                        f"coast {coast_ms} | auth={verdict}")
        self._refresh_matrix()
        self._update_runnext_label()
        # Category batch: keep going only after a SAVED full-cycle run, an unsaved
        # run means something went wrong (too few frames / discard), and repeating
        # it blind would loop forever on a broken bench.
        if kind == "full" and getattr(self, "_batch_cond", None):
            if sid:
                self.status.showMessage(
                    f"Batch {self._batch_cond}: saved {sid}; next run in 5 s… (STOP ALL cancels)")
                QtCore.QTimer.singleShot(5000, self._batch_step)
            else:
                cond = self._batch_cond
                self._batch_cond = None
                self._batch_state_write(None)
                QtWidgets.QMessageBox.warning(self, "Batch stopped",
                    f"The last {cond} run was not saved, so the batch stopped. "
                    "Fix the issue and press the category button to resume; "
                    "completed runs are kept.")

    # =====================================================  per-category batch runner
    def _batch_todo(self, cond):
        """Remaining (speed, runs) cells for one condition in the current dataset."""
        sub = FIXTURE_MODES[self.fixture_mode][0]
        plan = dict(self.tax.data.get("coverage_plan") or cov.DEFAULT_PLAN)
        if hasattr(self, "sb_target"):
            plan["runs_per_cell"] = self.sb_target.value()
        counts = cov.count_official(self.config["data_dir"], sub)
        _rows, _ov, todo = cov.build_requirements(counts, plan)
        return [t for t in todo if t[0] == cond]

    def _batch_state_write(self, cond):
        """Crash-proofing (2026-07-30): the app can be killed at any moment by an
        external UI-automation collision (Windows 0x8001010d in the Qt event loop,
        caught twice by the console trap, not a bench bug). Batch progress lives on
        disk so a supervisor relaunch resumes the campaign instead of losing it."""
        import json as _json
        path = os.path.join(self.live.dir, "batch_state.json")
        try:
            if cond:
                io.open(path, "w", encoding="utf-8").write(_json.dumps({"cond": cond}))
            elif os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _maybe_resume_batch(self):
        """On startup: if a batch was interrupted (file present), resume it."""
        import json as _json
        path = os.path.join(self.live.dir, "batch_state.json")
        if not os.path.exists(path):
            return
        try:
            cond = _json.load(io.open(path, encoding="utf-8")).get("cond")
        except Exception:
            return
        if not cond or not self.source:
            return
        if getattr(self, "_run_active", False) or getattr(self, "_cd", None):
            return
        self._batch_cond = cond
        self.live.event(f"BATCH resume after restart: {cond}")
        self.status.showMessage(f"Resuming interrupted {cond} batch…")
        self._batch_step()

    def _start_batch(self, cond):
        if getattr(self, "_run_active", False) or getattr(self, "_sweep_running", False)                 or getattr(self, "_cd", None) or getattr(self, "_cal", None):
            QtWidgets.QMessageBox.information(self, "Busy", "Stop the current run first."); return
        if not self.source:
            QtWidgets.QMessageBox.information(self, "Connect first", "Connect a source first."); return
        todo = self._batch_todo(cond)
        if not todo:
            QtWidgets.QMessageBox.information(self, "Complete",
                f"Every planned {cond} test is already recorded."); return
        n = sum(t[2] for t in todo)
        est = cov.plan_workload(None, n)
        if QtWidgets.QMessageBox.question(
                self, "Run whole category",
                f"{cond}: {n} run(s) remaining ≈ {est['hours']:.1f} h of bench time.\n\n"
                f"Set the {cond} fixture up now; it stays the same for the whole batch.\n"
                "Start?", QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes) != QtWidgets.QMessageBox.Yes:
            return
        self._batch_cond = cond
        self._batch_state_write(cond)
        self.live.event(f"BATCH start {cond} ({n} runs)")
        self._batch_step()

    def _batch_step(self):
        cond = getattr(self, "_batch_cond", None)
        if not cond:
            return
        todo = self._batch_todo(cond)
        if not todo:
            self._batch_cond = None
            self._batch_state_write(None)
            self.live.event(f"BATCH complete {cond}")
            self.status.showMessage(f"Category {cond} COMPLETE; every planned run recorded.")
            QtWidgets.QMessageBox.information(self, "Category complete",
                f"All planned {cond} runs are recorded and consolidated.")
            return
        _c, sp, _rem = todo[0]
        sev = "none" if cond == "healthy" else "mild"
        self.tabs.setCurrentIndex(0)
        if not self._run_category(cond, sp, sev):
            self._batch_cond = None
            self._batch_state_write(None)
            self.status.showMessage(f"Batch {cond} could not start (bench busy) - press the button again.")

    # =====================================================  step-through: run the next needed test
    def _run_next_remaining(self):
        """Run the next still-needed OFFICIAL test for the current dataset. Click again for the
        next one (manual step-through). Recomputes 'most needed' after each run."""
        sub = FIXTURE_MODES[self.fixture_mode][0]
        plan = dict(self.tax.data.get("coverage_plan") or cov.DEFAULT_PLAN)
        if hasattr(self, "sb_target"):
            plan["runs_per_cell"] = self.sb_target.value()
        counts = cov.count_official(self.config["data_dir"], sub)
        _rows, _ov, todo = cov.build_requirements(counts, plan)
        if not todo:
            QtWidgets.QMessageBox.information(self, "All done",
                f"Every planned test for {'UNMOUNTED' if sub == 'commissioning_unmounted' else 'MOUNTED'} "
                "is complete."); self._update_runnext_label(); return
        cond, sp, _rem = todo[0]
        sev = "none" if cond == "healthy" else "mild"
        self.tabs.setCurrentIndex(0)
        self._run_category(cond, sp, sev)

    def _update_runnext_label(self):
        """Refresh the primary button text with the next test + remaining count."""
        if not hasattr(self, "btn_rec"):
            return
        try:
            sub = FIXTURE_MODES[self.fixture_mode][0]
            plan = dict(self.tax.data.get("coverage_plan") or cov.DEFAULT_PLAN)
            if hasattr(self, "sb_target"):
                plan["runs_per_cell"] = self.sb_target.value()
            counts = cov.count_official(self.config["data_dir"], sub)
            _r, ov, todo = cov.build_requirements(counts, plan)
            if todo:
                cond, sp, _rem = todo[0]
                self.btn_rec.setText(f"▶  Run next:  {cond} @ {sp} rpm    ·  {ov['remaining']} left")
                self.btn_rec.setEnabled(True)
            else:
                self.btn_rec.setText("✓  All planned tests complete")
        except Exception:
            self.btn_rec.setText("▶  Run next test")

    # =====================================================  calibration check (new day)
    def _calibration_mode(self):
        """Quick verified spin-up: checks rpm tracking, ripple, Hall health, frame rate and
        integrity so you don't record false data. Run it at the start of each day."""
        if getattr(self, "_run_active", False) or getattr(self, "_sweep_running", False) \
                or getattr(self, "_cd", None) or getattr(self, "_cal", None) or self.session.active:
            QtWidgets.QMessageBox.information(self, "Busy", "Stop the current run first."); return
        if not self.source:
            QtWidgets.QMessageBox.information(self, "Connect first", "Connect a source first."); return
        speed = max(800, min(self.sb_live.value() or 1000, int(fwg.RPM_CEILING)))
        self._cal = {"phase": "settle", "left": 6, "sample": 6, "speed": speed}
        for b in ("btn_cal", "btn_rec", "btn_test"):
            if hasattr(self, b):
                getattr(self, b).setEnabled(False)
        self.stats = FrameStats()                       # fresh stats -> integrity reflects this check
        self._set_speed_live(speed)
        self.status.showMessage(f"Calibration: spinning to {speed} rpm and verifying…")
        self.live.event(f"CALIBRATION start | {speed} rpm")
        self._cal_timer = QtCore.QTimer(self); self._cal_timer.timeout.connect(self._calibration_tick)
        self._cal_timer.start(1000)

    def _calibration_tick(self):
        cal = getattr(self, "_cal", None)
        if cal is None:
            return
        cal["left"] -= 1
        if cal["phase"] == "settle":
            self.status.showMessage(f"Calibration: settling at {cal['speed']} rpm… {max(0, cal['left'])}s")
            if cal["left"] <= 0:
                cal["phase"] = "sample"; cal["left"] = cal["sample"]
                self.status.showMessage("Calibration: sampling readings…")
        else:
            self.status.showMessage(f"Calibration: sampling… {max(0, cal['left'])}s")
            if cal["left"] <= 0:
                self._calibration_eval()

    def _calibration_eval(self):
        cal = getattr(self, "_cal", None) or {}
        speed = cal.get("speed", 0)
        if getattr(self, "_cal_timer", None):
            self._cal_timer.stop()
        # --- gather measurements while the motor is STILL spinning (so the operator can read
        #     an external tachometer for the reference check) ---
        n = 60
        rpm = np.fromiter(self.buf["rpm"], float)[-n:]
        rip = np.fromiter(self.buf["ripple_permil"], float)[-n:]
        ill = np.fromiter(self.buf["illegal"], float)[-n:]
        hall = np.fromiter(self.buf["hall"], float)[-n:]
        app_rpm = float(rpm.mean()) if len(rpm) else 0.0
        checks = []  # (name, ok, detail)
        checks.append(("Telemetry flowing", len(rpm) >= 20, f"{len(rpm)} frames sampled"))
        integ = self.stats.good_pct if self.stats.total else 0.0
        checks.append(("Frame integrity ≥ 95%", integ >= 95, f"{integ:0.1f}%"))
        if len(rpm):
            err = abs(app_rpm - speed) / speed * 100 if speed else 999
            checks.append(("Speed tracks command (±8%)", err <= 8,
                           f"cmd {speed} → meas {app_rpm:0.0f} rpm ({err:0.1f}%)"))
        else:
            checks.append(("Speed tracks command (±8%)", False, "no rpm data"))
        if len(rip):
            med = float(np.median(rip)); checks.append(("Ripple in sane band (<600‰)", 0 < med < 600, f"median {med:0.0f}‰"))
        else:
            checks.append(("Ripple in sane band (<600‰)", False, "no data"))
        ill_rise = int(ill[-1] - ill[0]) if len(ill) else 0
        checks.append(("No illegal Hall states", ill_rise == 0, f"+{ill_rise} during check"))
        ndist = len(set(int(h) for h in hall)) if len(hall) else 0
        checks.append(("Hall sensors cycling", ndist >= 4, f"{ndist} distinct states"))

        # REFERENCE check: operator enters an external tachometer reading (optional)
        ref_rpm = ref_err = None
        if app_rpm > 0:
            val, ok = QtWidgets.QInputDialog.getDouble(
                self, "Reference check (tachometer)",
                f"The motor is holding ~{app_rpm:.0f} rpm (setpoint {speed}).\n\n"
                "Read your external tachometer NOW and enter its rpm\n"
                "to verify the reading is exactly correct, or Cancel to skip.",
                round(app_rpm), 0, 6000, 0)
            if ok and val > 0:
                ref_rpm = float(val); ref_err = (app_rpm - ref_rpm) / ref_rpm * 100.0
                checks.append(("RPM vs tachometer (±2%)", abs(ref_err) <= 2.0,
                               f"app {app_rpm:0.0f} vs tach {ref_rpm:0.0f}  ({ref_err:+0.1f}%)"))

        # now stop the motor
        if self.source:
            self._cmd_speed(0); self.lbl_cmd.setText("Commanded: OFF")
        for b in ("btn_cal", "btn_rec", "btn_test"):
            if hasattr(self, b):
                getattr(self, b).setEnabled(True)
        self._cal = None

        passed = all(ok for _, ok, _ in checks)
        self._log_calibration(speed, app_rpm, ref_rpm, ref_err, checks, passed)
        self.live.event(f"CALIBRATION {'PASS' if passed else 'FAIL'} | app {app_rpm:.0f} | "
                        f"ref {ref_rpm} | " + " | ".join(f"{nm}={'ok' if ok else 'BAD'}" for nm, ok, _ in checks))
        ref_tag = "" if ref_rpm is None else f"  ·  tach {ref_rpm:.0f} ({ref_err:+.1f}%)"
        headline = (("Calibration PASSED; rig verified, safe to collect"
                     if passed else "Calibration NEEDS REVIEW; fix before collecting (else false readings)")
                    + ref_tag)
        self._report_dialog("Calibration check", passed, headline,
                             [(f"Reference spin @ {speed} rpm", checks)],
                             note="Logged to data/calibration_log.csv  (dated record of every check + tach reading). "
                                  "Tip: a consistent rpm-vs-tach error across speeds points to the MCU timebase.")
        self.status.showMessage(("Calibration PASSED; ready to collect." if passed
                                 else "Calibration REVIEW; check the rig.") + " Motor OFF.")

    def _log_calibration(self, setpoint, app_rpm, ref_rpm, ref_err, checks, passed):
        """Append a dated calibration record to data/calibration_log.csv (traceability)."""
        path = os.path.join(self.config["data_dir"], "calibration_log.csv")
        row = {
            "iso_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "fixture": self.fixture_mode,
            "setpoint_rpm": setpoint,
            "app_measured_rpm": round(app_rpm, 1),
            "reference_rpm": "" if ref_rpm is None else round(ref_rpm, 1),
            "error_pct": "" if ref_err is None else round(ref_err, 2),
            "reference_ok": "" if ref_rpm is None else ("yes" if abs(ref_err) <= 2.0 else "no"),
            "overall": "PASS" if passed else "REVIEW",
            "checks": " | ".join(f"{nm}={'ok' if ok else 'BAD'}" for nm, ok, _ in checks),
        }
        try:
            new = not os.path.exists(path)
            with open(path, "a", newline="", encoding="utf-8") as f:   # check names use ≥/‰/±
                w = csv.DictWriter(f, fieldnames=list(row.keys()))
                if new:
                    w.writeheader()
                w.writerow(row)
        except Exception as exc:
            self.live.event(f"calibration log write failed: {exc}")

    # =====================================================  remaining-tests picker (Monitor)
    def _open_remaining_popup(self):
        mode = "commissioning_unmounted" if self.fixture_mode == "commissioning_unmounted" else "mounted_baseline"
        counts = cov.count_official(self.config["data_dir"], mode)
        plan = dict(self.tax.data.get("coverage_plan") or cov.DEFAULT_PLAN)
        if hasattr(self, "sb_target"):
            plan["runs_per_cell"] = self.sb_target.value()
        _rows, overall, todo = cov.build_requirements(counts, plan)
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Run a remaining test")
        dlg.setMinimumWidth(440)
        v = QtWidgets.QVBoxLayout(dlg)
        ds = "Commissioning (unmounted)" if mode == "commissioning_unmounted" else "Mounted (baseline)"
        v.addWidget(QtWidgets.QLabel(f"Dataset: <b>{ds}</b>  ·  {overall['remaining']} runs left.\n"
                                     "Pick one; it runs a full-cycle test (spin → record → coast → save)."))
        lst = QtWidgets.QListWidget()
        for cond, sp, rem in todo:
            sev = "none" if cond == "healthy" else "mild"
            it = QtWidgets.QListWidgetItem(f"{cond}   @ {sp} rpm:   {rem} needed")
            it.setData(QtCore.Qt.UserRole, (cond, sp, sev)); lst.addItem(it)
        if not todo:
            lst.addItem("✓  All requirements met for this dataset.")
        v.addWidget(lst, 1)
        bb = QtWidgets.QDialogButtonBox()
        bb.addButton("▶ Run selected", QtWidgets.QDialogButtonBox.AcceptRole)
        bb.addButton(QtWidgets.QDialogButtonBox.Cancel)
        v.addWidget(bb)
        picked = {"v": None}

        def accept():
            it = lst.currentItem()
            if it and it.data(QtCore.Qt.UserRole):
                picked["v"] = it.data(QtCore.Qt.UserRole); dlg.accept()
        bb.accepted.connect(accept); bb.rejected.connect(dlg.reject)
        lst.itemDoubleClicked.connect(lambda _i: accept())
        if dlg.exec_() == QtWidgets.QDialog.Accepted and picked["v"]:
            cond, sp, sev = picked["v"]
            self.tabs.setCurrentIndex(0)
            self._run_category(cond, sp, sev)

    # =====================================================  coverage launch helpers
    def _run_todo_item(self, item):
        data = item.data(QtCore.Qt.UserRole)
        if not data:
            return
        cond, sp, sev = data
        mode = self.cmb_dataset.currentData()
        want = "mounted" if mode == "mounted_baseline" else "commissioning_unmounted"
        if self.fixture_mode != want:
            self._set_fixture(want)
        self.tabs.setCurrentIndex(0)
        self._run_category(cond, sp, sev)

    def _run_next_needed(self):
        for i in range(self.todo.count()):
            it = self.todo.item(i)
            if it.data(QtCore.Qt.UserRole):
                self._run_todo_item(it); return
        QtWidgets.QMessageBox.information(self, "Coverage", "No remaining runs for this dataset.")

    def _row_remaining_cells(self, r):
        """All remaining (cond, speed, sev) for grid row r, one entry per run still needed."""
        cells = []
        for cc in range(1, self.tbl.columnCount() - 1):
            cell = self.tbl.item(r, cc)
            d = cell.data(QtCore.Qt.UserRole) if cell else None
            if d and d[3] and d[3] > 0:
                cells += [(d[0], d[1], d[2])] * int(d[3])
        return cells

    def _coverage_cell_clicked(self, r, c):
        """Click a speed cell to run ONE full-cycle test there (completed cells are locked).
        Click the Category or Left column to run the next still-needed rpm in that row."""
        it = self.tbl.item(r, c)
        data = it.data(QtCore.Qt.UserRole) if it else None
        if not data:
            return
        cond, sp, sev, rem = data
        if sp is None:                              # Category / Left -> next still-needed rpm in row
            picked = None
            for cc in range(1, self.tbl.columnCount() - 1):
                cell = self.tbl.item(r, cc)
                d = cell.data(QtCore.Qt.UserRole) if cell else None
                if d and d[3] and d[3] > 0:
                    picked = d; break
            if not picked:
                QtWidgets.QMessageBox.information(self, "Complete",
                    f"{cond}: every rpm is already complete; locked."); return
            cond, sp, sev, rem = picked
        if not rem or rem <= 0:                     # locked: database already complete here
            QtWidgets.QMessageBox.information(self, "Complete",
                f"{cond} @ {sp} rpm is already complete; locked (cannot be re-run)."); return
        nice = "healthy" if sev == "none" else sev
        if QtWidgets.QMessageBox.question(
                self, "Run test",
                f"Run a 10-minute full-cycle test now?\n\n   {cond}  ·  {sp} rpm  ·  {nice}\n\n"
                "Spin up → 10-min record → coast → save.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes) != QtWidgets.QMessageBox.Yes:
            return
        mode = self.cmb_dataset.currentData()
        want = "mounted" if mode == "mounted_baseline" else "commissioning_unmounted"
        if self.fixture_mode != want:
            self._set_fixture(want)
        self.tabs.setCurrentIndex(0)
        self._run_category(cond, sp, sev)

    # editable plan (persists to taxonomy.yaml > coverage_plan)
    def _plan_obj(self):
        plan = self.tax.data.get("coverage_plan")
        if not isinstance(plan, dict):
            plan = dict(cov.DEFAULT_PLAN); self.tax.data["coverage_plan"] = plan
        return plan

    def _plan_add_rpm(self):
        sp = int(self.sb_addrpm.value()); plan = self._plan_obj()
        speeds = [int(x) for x in plan.get("speeds", cov.DEFAULT_PLAN["speeds"])]
        if sp > 0 and sp not in speeds:
            speeds.append(sp); speeds.sort(); plan["speeds"] = speeds
            plan["runs_per_cell"] = self.sb_target.value()
            self.tax.save(); self._refresh_matrix(); self._update_runnext_label()
            self.status.showMessage(f"Plan: added {sp} rpm to every category.")
        else:
            self.status.showMessage(f"{sp} rpm is already in the plan.")

    def _plan_remove_rpm(self):
        sp = int(self.sb_addrpm.value()); plan = self._plan_obj()
        speeds = [int(x) for x in plan.get("speeds", cov.DEFAULT_PLAN["speeds"])]
        if sp in speeds:
            speeds.remove(sp); plan["speeds"] = speeds
            self.tax.save(); self._refresh_matrix(); self._update_runnext_label()
            self.status.showMessage(f"Plan: removed {sp} rpm.")
        else:
            self.status.showMessage(f"{sp} rpm is not in the plan.")

    def _plan_add_category(self):
        c = self.cmb_addcat.currentText().strip().lower().replace(" ", "_").replace("-", "_")
        if not c:
            return
        plan = self._plan_obj()
        conds = list(plan.get("conditions", cov.DEFAULT_PLAN["conditions"]))
        # register a brand-new name as a real taxonomy condition so it's a valid run label
        tax_conds = list(self.tax.data.get("conditions", []))
        changed = False
        if c not in tax_conds:
            tax_conds.append(c); self.tax.data["conditions"] = tax_conds; changed = True
            if self.cmb_addcat.findText(c) < 0:
                self.cmb_addcat.addItem(c)
        if c not in conds:
            conds.append(c); plan["conditions"] = conds; changed = True
        if changed:
            self.tax.save(); self._refresh_matrix(); self._update_runnext_label()
            self.status.showMessage(f"Plan: category '{c}' added.")
        else:
            self.status.showMessage(f"'{c}' is already in the plan.")

    def _plan_remove_category(self):
        c = self.cmb_addcat.currentText().strip().lower().replace(" ", "_").replace("-", "_")
        plan = self._plan_obj()
        conds = list(plan.get("conditions", cov.DEFAULT_PLAN["conditions"]))
        if c in conds:
            conds.remove(c); plan["conditions"] = conds
            self.tax.save(); self._refresh_matrix(); self._update_runnext_label()
            self.status.showMessage(f"Plan: category '{c}' removed.")
        else:
            self.status.showMessage(f"'{c}' is not in the plan.")

    def _autosweep_button(self):
        """One click: build firmware -> flash -> connect -> then run the sweep.
        If it can't build/flash/connect, stop with a popup error."""
        if getattr(self, "_run_active", False) or getattr(self, "_sweep_running", False):
            return
        gcc = self.tools.paths.get("gcc"); cli = self.tools.paths.get("programmer_cli")
        fw = self.config.get("firmware_dir")
        if not (gcc and cli and fw and os.path.isdir(fw)):
            QtWidgets.QMessageBox.critical(self, "Auto sweep",
                "Cannot build/flash. STM32 toolchain or firmware folder not found.\n\n"
                "Connect the board manually (Connection panel), then use START TEST → Speed sweep."); return
        if isinstance(self.source, SerialSource):
            self._disconnect()                            # free the ST-LINK for SWD
        _g, bargs, elf = fwg.build_command(gcc, fw)
        steps = [(gcc, bargs, "BUILD (arm-none-eabi-gcc)"),
                 (cli, ["-c", "port=SWD", "mode=UR", "-w", elf, "-rst"], "FLASH (STM32_Programmer_CLI)")]
        self._after_chain = self._after_bfr_sweep
        self._chain_fail = lambda: QtWidgets.QMessageBox.critical(
            self, "Auto sweep", "Build or flash FAILED; sweep cancelled. See the log window.")
        self.live.event("AUTO-SWEEP: build+flash+connect+sweep")
        self._run_proc_chain(steps, "Auto sweep: build · flash · connect",
                             preamble="Build → flash → connect → then the sweep starts.")

    def _after_bfr_sweep(self):
        self._rescan_ports()
        if not getattr(self, "_detected_port", None):
            QtWidgets.QMessageBox.critical(self, "Auto sweep",
                "Could not connect; no ST-LINK detected after flashing. Sweep cancelled."); return
        if self.source is None:
            self._toggle_connect()
        QtCore.QTimer.singleShot(2000, self._sweep_after_connect)   # let the board re-enumerate + stream

    def _sweep_after_connect(self):
        ok = self.source is not None and (not isinstance(self.source, SerialSource) or self.source.connected)
        if not ok:
            QtWidgets.QMessageBox.critical(self, "Auto sweep",
                "Could not connect to the board after flashing. Sweep cancelled."); return
        self._auto_sweep()

    def _auto_sweep(self):
        if not self.source:
            QtWidgets.QMessageBox.information(self, "Not connected", "Connect a source first."); return
        if getattr(self, "_run_active", False):
            QtWidgets.QMessageBox.information(self, "Run active",
                "Finish or STOP ALL the current run before starting a sweep."); return
        if getattr(self, "_sweep_running", False):
            QtWidgets.QMessageBox.information(self, "Sweep", "A sweep is already running."); return
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Auto speed sweep")
        v = QtWidgets.QVBoxLayout(dlg)
        v.addWidget(QtWidgets.QLabel("Records one run per speed automatically. Two settings:"))
        form = QtWidgets.QFormLayout()
        ed = QtWidgets.QLineEdit("500, 800, 1100, 1400, 1700, 2000")
        fillrow = QtWidgets.QHBoxLayout()
        sp_step = QtWidgets.QSpinBox(); sp_step.setRange(50, 1000); sp_step.setSingleStep(50); sp_step.setValue(200); sp_step.setSuffix(" rpm step")
        btn_fill = QtWidgets.QPushButton("Fill every speed from 200")
        btn_fill.clicked.connect(lambda: ed.setText(
            ", ".join(str(s) for s in range(200, fwg.RPM_CEILING + 1, max(50, sp_step.value())))))
        fillrow.addWidget(btn_fill); fillrow.addWidget(sp_step)
        sp_dwell = QtWidgets.QSpinBox(); sp_dwell.setRange(5, 3600); sp_dwell.setValue(30); sp_dwell.setSuffix(" seconds")
        form.addRow("Speeds (rpm)", ed)
        form.addRow("", self._wrap(fillrow))
        form.addRow("Record each for", sp_dwell)
        v.addLayout(form)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Start sweep")
        v.addWidget(bb); bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        try:
            speeds = [int(x) for x in ed.text().replace(",", " ").split() if x.strip()]
        except ValueError:
            QtWidgets.QMessageBox.warning(self, "Sweep", "Could not parse the speeds list."); return
        if not speeds:
            return
        cal = self._calibration_gate()                 # one calibration confirm up front
        if cal is None:
            return
        queue = [(s, 1) for s in speeds]
        self.live.event(f"SWEEP start | {len(queue)} speeds | {sp_dwell.value()}s each | {speeds}")
        self._sweep = {"settle": 3, "dwell": sp_dwell.value(), "cal": cal, "queue": queue,
                       "total": len(queue), "done": 0, "saved": 0, "discarded": 0}
        self._sweep_running = True
        self.btn_rec.setText("⏱ Sweep running…"); self.btn_rec.setEnabled(False)
        self.btn_autosweep.setEnabled(False)
        self._open_sweep_progress(len(queue))
        self._sweep_step()

    def _open_sweep_progress(self, total):
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Auto speed sweep: running")
        dlg.setMinimumWidth(460)
        v = QtWidgets.QVBoxLayout(dlg)
        self._sw_status = QtWidgets.QLabel("Starting…")
        self._sw_status.setStyleSheet("font-size:16px; font-weight:bold; color:#6fb0e8; border:0;")
        self._sw_bar = QtWidgets.QProgressBar(); self._sw_bar.setRange(0, total); self._sw_bar.setValue(0)
        self._sw_bar.setFormat("%v / %m runs")
        self._sw_detail = QtWidgets.QLabel(""); self._sw_detail.setStyleSheet("color:#9fb0bf; border:0;")
        self._sw_cancel_btn = QtWidgets.QPushButton("Cancel sweep")
        self._sw_cancel_btn.clicked.connect(self._cancel_sweep)
        v.addWidget(self._sw_status); v.addWidget(self._sw_bar); v.addWidget(self._sw_detail)
        row = QtWidgets.QHBoxLayout(); row.addStretch(1); row.addWidget(self._sw_cancel_btn); v.addLayout(row)
        self._sweep_dlg = dlg; dlg.show()

    def _sw(self, text, detail=""):
        if getattr(self, "_sweep_dlg", None):
            self._sw_status.setText(text); self._sw_detail.setText(detail)
            self._sw_bar.setValue(self._sweep["done"])

    def _sweep_step(self):
        if not getattr(self, "_sweep_running", False):
            return
        if not self._sweep["queue"]:                       # finished
            self._sweep_running = False
            if self.source:
                self._cmd_speed(0)                   # motor OFF after the whole sweep
            if hasattr(self, "lbl_cmd"):
                self.lbl_cmd.setText("Commanded: OFF")
            self._reset_run_button()
            self._sweep["done"] = self._sweep["total"]
            self._sw("Sweep complete  ✓", f"{self._sweep['total']} runs done. Motor OFF.")
            if getattr(self, "_sweep_dlg", None):
                self._sw_cancel_btn.setText("Close")
                try:
                    self._sw_cancel_btn.clicked.disconnect()
                except Exception:
                    pass
                self._sw_cancel_btn.clicked.connect(self._sweep_dlg.accept)
            saved = self._sweep.get("saved", 0); disc = self._sweep.get("discarded", 0)
            self.live.event(f"SWEEP complete; {saved} saved, {disc} discarded; motor OFF")
            self.status.showMessage(f"Auto sweep done: {saved} saved, {disc} discarded. Motor OFF.")
            self._refresh_matrix()
            if saved == 0:
                QtWidgets.QMessageBox.warning(self, "Sweep saved nothing",
                    f"All {disc} sweep points had no valid telemetry, so nothing was saved.\n\n"
                    "The motor wasn't producing Hall data; it likely never spun. Check that the "
                    "speed buttons actually turn the motor and that the 24 V driver is powered.")
            else:
                QtWidgets.QMessageBox.information(self, "Sweep done",
                    f"Saved {saved} of {self._sweep['total']} runs to:\n"
                    f"data/{FIXTURE_MODES[self.fixture_mode][0]}/  (combined_data.csv + per-run folders)\n\n"
                    f"{disc} discarded for no valid data.")
            return
        rpm = self._sweep["queue"][0][0]
        idx = self._sweep["done"] + 1
        self._set_field("speed_rpm_nominal", rpm)
        self._cmd_speed(rpm)
        self._sw(f"Test {idx} of {self._sweep['total']}:  {rpm} rpm",
                 f"Settling {self._sweep['settle']} s…")
        self.status.showMessage(f"Sweep: {rpm} rpm; settling")
        QtCore.QTimer.singleShot(self._sweep["settle"] * 1000, self._sweep_record_start)

    def _sweep_record_start(self):
        if not getattr(self, "_sweep_running", False) or not self._sweep["queue"]:
            return
        rpm = self._sweep["queue"][0][0]
        self.stats = FrameStats()
        meta = self._collect_meta()
        meta["calibration"] = self._sweep["cal"]; meta["cal_verdict"] = self._sweep["cal"]["verdict"]
        meta["notes"] = (meta.get("notes", "") + " auto_sweep").strip()
        meta["run_kind"] = "rd"                       # auto-sweep = R&D / exploratory
        self._session_to(True)                        # -> data/rd
        try:
            self.session.start(meta)
        except Exception as exc:
            self.status.showMessage(f"Sweep start error: {exc}")
        self._sweep_remaining = int(self._sweep["dwell"])
        self._sweep_rec_timer = QtCore.QTimer(self)
        self._sweep_rec_timer.timeout.connect(self._sweep_rec_tick)
        self._sweep_rec_timer.start(1000)
        self._sweep_rec_tick(initial=True)

    def _sweep_rec_tick(self, initial=False):
        if not getattr(self, "_sweep_running", False):
            if getattr(self, "_sweep_rec_timer", None):
                self._sweep_rec_timer.stop()
            return
        if not initial:
            self._sweep_remaining -= 1
        idx = self._sweep["done"] + 1
        rpm = self._sweep["queue"][0][0] if self._sweep["queue"] else 0
        self._sw(f"Test {idx} of {self._sweep['total']}:  {rpm} rpm",
                 f"● Recording…  {max(0, self._sweep_remaining)} s left")
        if self._sweep_remaining <= 0:
            if getattr(self, "_sweep_rec_timer", None):
                self._sweep_rec_timer.stop()
            self._sweep_record_stop()

    def _sweep_record_stop(self):
        if self.session.active:
            if self.stats.good < 20:                          # no valid data -> drop empty point
                self.session.discard(); self._sweep["discarded"] += 1
                self.live.event(f"SWEEP point discarded; {self.stats.good} valid frames")
            else:
                verdict, _ = self._assess_authenticity()
                self.session.stop(self.stats, extra={
                    "data_source": "serial" if isinstance(self.source, SerialSource) else "unverified",
                    "authenticity": verdict}); self._sweep["saved"] += 1
        self._session_to(False)
        if getattr(self, "_sweep_running", False) and self._sweep["queue"]:
            self._sweep["queue"].pop(0); self._sweep["done"] += 1
        QtCore.QTimer.singleShot(400, self._sweep_step)

    def _cancel_sweep(self):
        self._sweep_running = False
        if getattr(self, "_sweep_rec_timer", None):
            self._sweep_rec_timer.stop()
        if self.session.active:
            self.session.discard()                 # drop the partially-recorded point
        self._session_to(False)
        if self.source:
            self._cmd_speed(0)
        self._reset_run_button()
        if getattr(self, "_sweep_dlg", None):
            self._sweep_dlg.accept(); self._sweep_dlg = None
        self.status.showMessage("Sweep cancelled; motor OFF."); self._refresh_matrix()

    def _run_proc_chain(self, steps, title, preamble=""):
        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle(title)
        lay = QtWidgets.QVBoxLayout(dlg)
        self._chain_log = QtWidgets.QPlainTextEdit(); self._chain_log.setReadOnly(True)
        self._chain_log.setMinimumSize(640, 420)
        self._chain_log.setStyleSheet("font-family:Consolas; font-size:11px;")
        lay.addWidget(self._chain_log); self._chain_dlg = dlg; dlg.show()
        if preamble:
            self._chain_log.appendPlainText(preamble)
        self._chain = list(steps)
        self._chain_next()

    def _chain_next(self):
        if not self._chain:
            self._chain_log.appendPlainText("\n=== ALL STEPS DONE ===")
            self.status.showMessage("Firmware: build + flash complete.")
            self._update_tool_buttons()
            self._chain_fail = None
            cb = getattr(self, "_after_chain", None)
            if cb:
                self._after_chain = None
                cb()                                  # e.g. connect & run after a flash
            return
        exe, args, label = self._chain.pop(0)
        self._chain_log.appendPlainText(f"\n--- {label} ---\n$ {os.path.basename(exe)} {' '.join(args)}\n")
        p = QtCore.QProcess(self); self._chain_proc = p
        p.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        p.readyReadStandardOutput.connect(
            lambda: self._chain_log.appendPlainText(
                bytes(p.readAllStandardOutput()).decode("ascii", "ignore").rstrip()))

        def done(code, _s):
            if code != 0:
                self._chain_log.appendPlainText(f"\n*** {label} FAILED (exit {code}); stopping ***")
                self.status.showMessage(f"Firmware: {label} failed (exit {code})")
                self._chain = []
                self._after_chain = None              # don't auto-connect on a failed flash
                cb = getattr(self, "_chain_fail", None)
                if cb:
                    self._chain_fail = None
                    cb()
            else:
                self._chain_log.appendPlainText(f"[{label} OK]")
                self._chain_next()
        p.finished.connect(done)
        p.start(exe, args)

    def _toggle_connect(self):
        if self.source is None:
            port = getattr(self, "_detected_port", None) or find_stlink_port()
            if not port:
                QtWidgets.QMessageBox.warning(self, "No hardware",
                    "No ST-LINK detected. Plug the NUCLEO into USB and click Rescan.")
                return
            self._detected_port = port
            baud = int(self.config.get("baud", 115200))
            self.source = SerialSource(port, baud)
            self.source.start()
            self.btn_conn.setText("Disconnect")
            self.lbl_hw.setText(f"●  Connected: {port}")
            self.lbl_hw.setStyleSheet("border:0; color:#8fdca0;")
            self.status.showMessage(f"Connecting {port}…")
            self.live.event(f"connect {port}")
            # Command OFF as early and as often as it takes. A write can land before
            # the serial thread has opened the port, so retrying leaves the bench in
            # a known safe state even when the first command is missed.
            for _d in (0, 400, 1200, 2500):
                QtCore.QTimer.singleShot(_d, lambda: self._cmd_speed(0))
            # AUTO PARK RE-ROLL (2026-07-30, operator-approved): one ~1.2 s blip at
            # the minimum table speed, then OFF. A rotor that powered down exactly on
            # a hall sector boundary can latch a code that misrepresents how many
            # Hall outputs are sinking (seen once: code said one-sink, meter said
            # two-sink, first reading sat 9 mA low until the next stop re-parked).
            # Re-rolling the park before the first anchor removes that cold-start
            # window entirely. Guarded so it can never fire into an active run.
            def _park_blip(tries=0):
                # _cd is the FULL-CYCLE state, a batch started via cmd.json within
                # the first seconds of a launch runs as _cd, and the blip once stomped
                # its speed command 3 s into the settle (caught live 2026-07-30).
                if (getattr(self, "_run_active", False) or getattr(self, "_sweep_running", False)
                        or getattr(self, "_cd", None) or getattr(self, "_cal", None)
                        or getattr(self, "_batch_cond", None) or not self.source):
                    return
                # RETRY until the link is really up (2026-07-30): _cmd_speed now
                # refuses to lie when a serial write fails, which silently killed the
                # blip whenever the port was still connecting at t+3.2 s.
                if not self._cmd_speed(300):
                    if tries < 10:
                        QtCore.QTimer.singleShot(1000, lambda: _park_blip(tries + 1))
                    return
                self.live.event("connect park re-roll blip")
                QtCore.QTimer.singleShot(1200, lambda: self._cmd_speed(0))
            QtCore.QTimer.singleShot(3200, _park_blip)
            QtCore.QTimer.singleShot(18000, self._maybe_resume_batch)
        else:
            self._disconnect()

    def _disconnect(self):
        # Full unwind, mirroring _stop_all: cycles (_cd) and calibration (_cal) used
        # to survive a disconnect, leaving timers firing against a dead source.
        for t in ("_cd_timer", "_cal_timer"):
            if getattr(self, t, None):
                getattr(self, t).stop()
        self._cd = None; self._cal = None
        self._batch_cond = None
        self._batch_state_write(None)
        if getattr(self, "_sweep_running", False):
            self._cancel_sweep()                   # discards in-progress point
        if getattr(self, "_run_active", False):
            self._abort_run()                      # incomplete run -> discard
        elif self.session.active:
            self.session.discard()
        if self.source:
            self.source.stop(); self.source = None
        self.btn_conn.setText("Connect"); self.status.showMessage("Disconnected.")
        self.live.event("disconnect")
        self._rescan_ports()

    def _build_flash_run(self):
        """One click: build firmware -> flash over ST-LINK -> connect & run."""
        gcc = self.tools.paths.get("gcc"); cli = self.tools.paths.get("programmer_cli")
        fw = self.config.get("firmware_dir")
        if not (gcc and cli and fw and os.path.isdir(fw)):
            QtWidgets.QMessageBox.warning(self, "Build · Flash · Run",
                "Need arm-none-eabi-gcc + STM32_Programmer_CLI + firmware folder."); return
        if QtWidgets.QMessageBox.question(self, "Build · Flash · Run",
                "Build the firmware, flash it to the NUCLEO over ST-LINK, then connect and run?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.Yes) != QtWidgets.QMessageBox.Yes:
            return
        if isinstance(self.source, SerialSource):
            self._disconnect()                        # free the ST-LINK for SWD
        _g, bargs, elf = fwg.build_command(gcc, fw)
        steps = [(gcc, bargs, "BUILD (arm-none-eabi-gcc)"),
                 (cli, ["-c", "port=SWD", "mode=UR", "-w", elf, "-rst"], "FLASH (STM32_Programmer_CLI)")]
        self._after_chain = self._connect_after_flash
        self.live.event("BUILD+FLASH+RUN started")
        self._run_proc_chain(steps, "Build · Flash · Run",
                             preamble="Build → Flash → then auto-connect and run.")

    def _connect_after_flash(self):
        self._rescan_ports()
        if self.source is None:
            QtCore.QTimer.singleShot(800, self._toggle_connect)   # let the board re-enumerate
        self.status.showMessage("Flashed. Connecting; the motor runs the new firmware.")

    def _reset_run_button(self):
        self.btn_rec.setText("▶   START TEST"); self.btn_rec.setEnabled(True)
        self.btn_rec.setStyleSheet("background:#23a55a; color:white; font-weight:bold; font-size:15px; "
                                   "padding:13px; border:0; border-radius:12px;")
        if hasattr(self, "btn_autosweep"):
            self.btn_autosweep.setEnabled(True)

    def _start_test(self):
        """The single primary action: choose Single run or Speed sweep, then walk through."""
        if getattr(self, "_run_active", False) or getattr(self, "_sweep_running", False):
            return
        if self.source is None:
            QtWidgets.QMessageBox.information(self, "Not connected",
                "Connect the hardware first (Connection panel)."); return
        box = QtWidgets.QMessageBox(self); box.setWindowTitle("Start test")
        box.setText("What kind of test do you want to run?")
        b_single = box.addButton("Single run", QtWidgets.QMessageBox.AcceptRole)
        b_sweep = box.addButton("Speed sweep (many speeds)", QtWidgets.QMessageBox.AcceptRole)
        box.addButton("Cancel", QtWidgets.QMessageBox.RejectRole)
        box.exec_()
        clicked = box.clickedButton()
        if clicked is b_single:
            self._verify_and_run()
        elif clicked is b_sweep:
            self._auto_sweep()

    def _verify_and_run(self):
        """Walk-through: verify settings + run length, then the calibration check, then run."""
        if getattr(self, "_run_active", False) or getattr(self, "_sweep_running", False):
            return
        if self.source is None:
            QtWidgets.QMessageBox.information(self, "Not connected", "Connect the hardware first."); return
        missing = [k for k in ("condition", "orientation", "load") if not self._read_field(k)]
        if missing:
            QtWidgets.QMessageBox.warning(self, "Set test settings",
                "Pick these first:\n  " + ", ".join(missing)); return

        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Verify run")
        v = QtWidgets.QVBoxLayout(dlg)
        v.addWidget(QtWidgets.QLabel("<b>Confirm before running:</b>"))
        fix = ("Unmounted (not for classifier)" if self.fixture_mode == "commissioning_unmounted"
               else "Mounted (baseline)")
        rows = [("Fixture", fix), ("Test", self._read_field("condition"))]
        if self._read_field("condition") != "healthy":          # severity only for faults
            rows.append(("Severity", self._read_field("severity")))
        rows += [("Speed", self._read_field("speed_rpm_nominal") + " rpm"),
                 ("Load", self._read_field("load")), ("Position", self._read_field("orientation"))]
        g = QtWidgets.QFormLayout()
        for k, val in rows:
            lab = QtWidgets.QLabel(str(val)); lab.setStyleSheet("color:#8fd3ff; font-weight:bold;")
            g.addRow(k, lab)
        v.addLayout(g)
        v.addWidget(QtWidgets.QLabel("<b>Record for</b>"))
        sb = QtWidgets.QSpinBox(); sb.setRange(5, 3600); sb.setValue(int(getattr(self, "_run_len", 30)))
        sb.setSuffix(" seconds"); v.addWidget(sb)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Next → quick check")
        v.addWidget(bb); bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._run_len = int(sb.value())
        cal = self._calibration_gate()
        if cal is None:
            return
        self._do_run(cal)

    def _do_run(self, cal):
        self._persist_new_combo_values()
        self.stats = FrameStats()
        meta = self._collect_meta()
        meta["calibration"] = cal; meta["cal_verdict"] = cal["verdict"]
        meta["run_length_s"] = self._run_len
        meta["run_kind"] = "rd"                       # manual START TEST = R&D / debugging
        self._session_to(True)                        # -> data/rd
        sid, _folder = self.session.start(meta)
        self._run_active = True
        self._run_remaining = int(self._run_len)
        self.btn_rec.setEnabled(False)
        self.btn_autosweep.setEnabled(False)
        self.btn_rec.setStyleSheet("background:#e0362f; color:white; font-weight:bold; font-size:15px; "
                                   "padding:13px; border:0; border-radius:12px;")
        self.live.event(f"RUN start {sid} | {meta.get('label')} | {self._run_len}s | "
                        f"fixture={meta.get('fixture_state')} | speed={meta.get('speed_rpm_nominal')}")
        self._run_timer = QtCore.QTimer(self); self._run_timer.timeout.connect(self._run_tick)
        self._run_timer.start(1000)
        self._run_tick(initial=True)
        self.status.showMessage(f"RUN {sid}; recording {self._run_remaining}s (STOP ALL to abort)")

    def _run_tick(self, initial=False):
        if not getattr(self, "_run_active", False):
            return
        if not initial:
            self._run_remaining -= 1
        self.btn_rec.setText(f"■ Recording…  {max(0, self._run_remaining)}s")
        if self._run_remaining <= 0:
            self._finish_run()

    def _finish_run(self):
        if getattr(self, "_run_timer", None):
            self._run_timer.stop()
        self._run_active = False
        if self.source:
            self._cmd_speed(0)                 # motor OFF after the test
        if hasattr(self, "lbl_cmd"):
            self.lbl_cmd.setText("Commanded: OFF")
        # Discard ONLY if there's essentially no valid data (motor not spinning / no telemetry).
        # Otherwise save it (validated = PASS or REVIEW) so you always have your data.
        if self.stats.good < 20:
            self.session.discard(); self._session_to(False)
            self.live.event(f"RUN discarded; only {self.stats.good} valid frames (no telemetry?)")
            QtWidgets.QMessageBox.warning(self, "Nothing recorded",
                f"This run captured only {self.stats.good} valid frames, so nothing was saved.\n\n"
                "The motor likely wasn't spinning (no Hall data) or no telemetry was flowing.\n"
                "Check the Frame rate and LIVE SPEED cards, and that the 24 V driver is powered.")
            self.status.showMessage("Run discarded; no valid data. Motor OFF.")
            self._reset_run_button(); self._refresh_matrix(); return
        verdict, _ = self._assess_authenticity()
        res = self.session.stop(self.stats, extra={
            "data_source": "serial" if isinstance(self.source, SerialSource) else "unverified",
            "authenticity": verdict})
        self._session_to(False)
        self._reset_run_button()
        if res:
            self.status.showMessage(f"Saved {res['session_id']} (R&D): {res['n_rows']} rows · "
                                    f"{res['validated']} · {verdict}. Motor OFF.")
            self.live.event(f"RUN saved {res['session_id']} (rd) | {res['n_rows']} rows | "
                            f"{res['validated']} | auth={verdict}")
        self._refresh_matrix()

    def _abort_run(self):
        if getattr(self, "_run_timer", None):
            self._run_timer.stop()
        if getattr(self, "_run_active", False) and self.session.active:
            self.session.discard(); self._session_to(False)   # delete folder, no manifest row
            self.live.event("RUN aborted; discarded")
        self._run_active = False
        self._reset_run_button()

    def _stop_all(self):
        run = getattr(self, "_run_active", False)
        sweep = getattr(self, "_sweep_running", False)
        coast = getattr(self, "_cd", None) is not None
        cal = getattr(self, "_cal", None) is not None
        if coast or cal:                           # abort a running test / R&D probe / calibration
            if getattr(self, "_cd_timer", None):
                self._cd_timer.stop()
            if getattr(self, "_cal_timer", None):
                self._cal_timer.stop()
            if self.session.active:
                self.session.discard()
            self._session_to(False)
            self._cd = None; self._cal = None
            for b in ("btn_test", "btn_rec", "btn_cal"):
                if hasattr(self, b):
                    getattr(self, b).setEnabled(True)
            self._update_runnext_label()
        if run or sweep:
            msg = ("A sweep is running; stop everything and DISCARD the run in progress?"
                   if sweep else "A run is in progress; stop it and DISCARD its data?")
            if QtWidgets.QMessageBox.question(self, "STOP ALL", msg,
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No) != QtWidgets.QMessageBox.Yes:
                return
        self._batch_cond = None              # STOP ALL always ends a category batch
        self._batch_state_write(None)
        if self.source:
            self._cmd_speed(0)               # motor OFF
        if hasattr(self, "lbl_cmd"):
            self.lbl_cmd.setText("Commanded: OFF")
        if sweep:
            self._cancel_sweep()                   # discards the in-progress sweep point
        if run:
            self._abort_run()
        self.live.event("STOP ALL: motor OFF" + ("; run discarded" if (run or sweep) else ""))
        self.status.showMessage("STOPPED: motor OFF" + ("; run discarded" if (run or sweep) else ""))

    def _arm_baseline(self):
        rip = np.array(self.buf["ripple_permil"], dtype=float)
        if len(rip) < 100:
            QtWidgets.QMessageBox.information(self, "Need data",
                "Collect ~10 s of steady healthy data first."); return
        recent = rip[-600:]
        mu, sigma = float(recent.mean()), float(recent.std(ddof=1))
        self.sev.set_baseline(mu, sigma); self.armed = True
        self.val_base.setText(f"{mu:0.0f} ± {3 * sigma:0.0f}")
        self.status.showMessage(f"Baseline armed: ripple mu={mu:0.1f} sigma={sigma:0.2f} "
                                f"-> 3-sigma at {mu + 3*sigma:0.1f}")

    # =====================================================  calibration gate
    def _calibration_gate(self):
        """Pre-test check. Only requirement: data is flowing. Everything else is advisory
        (shown as PASS/WARN) and the checklist is optional. Returns a dict, or None if cancelled."""
        rpm = np.fromiter(self.buf["rpm"], float)
        rip = np.fromiter(self.buf["ripple_permil"], float)
        ill = np.fromiter(self.buf["illegal"], float)
        rated = float(self.config.get("motor", {}).get("rated_rpm", 4000))
        flowing = len(rpm) >= 20
        gp = self.stats.good_pct if self.stats.total else 0.0
        # (name, ok, value, required); only the first is required to proceed
        live = [
            ("Data flowing (>=20 frames)", flowing, f"{len(rpm)} frames", True),
            ("Frame integrity >= 95%", bool(self.stats.total) and gp >= 95, f"{gp:0.1f}%", False),
            (f"RPM in [50, {int(rated*1.1)}]",
             flowing and 50 <= rpm[-1] <= rated * 1.1, f"{rpm[-1]:0.0f}" if flowing else NO_VALUE, False),
            ("Ripple in [0, 600] permil",
             flowing and 0 <= rip[-1] <= 600, f"{rip[-1]:0.0f}" if flowing else NO_VALUE, False),
            ("No illegal-Hall rise",
             flowing and (ill[-1] - ill[0]) == 0, f"+{int(ill[-1]-ill[0])}" if flowing else NO_VALUE, False),
        ]
        all_advisory_ok = all(ok for _, ok, _, req in live if not req)

        dlg = QtWidgets.QDialog(self); dlg.setWindowTitle("Pre-test check")
        v = QtWidgets.QVBoxLayout(dlg)
        v.addWidget(QtWidgets.QLabel("<b>Live signal</b> (only 'data flowing' is required to proceed)"))
        grid = QtWidgets.QGridLayout()
        for r, (name, ok, val, req) in enumerate(live):
            if ok:
                txt, bg = "OK", "#2e7d32"
            elif req:
                txt, bg = "WAIT", "#c62828"
            else:
                txt, bg = "WARN", "#ef6c00"
            tag = QtWidgets.QLabel(txt)
            tag.setStyleSheet("color:white; padding:1px 6px; border-radius:3px; background:" + bg)
            grid.addWidget(tag, r, 0); grid.addWidget(QtWidgets.QLabel(name), r, 1)
            grid.addWidget(QtWidgets.QLabel(val), r, 2)
        v.addLayout(grid)

        v.addWidget(QtWidgets.QLabel("<b>Checklist</b> (optional; tick what applies)"))
        boxes = []
        for item in self.tax.list("calibration_checks"):
            cb = QtWidgets.QCheckBox(str(item)); boxes.append(cb); v.addWidget(cb)

        v.addWidget(QtWidgets.QLabel("<b>Reference readings</b> (optional, logged)"))
        rf = QtWidgets.QFormLayout()
        ed_v = QtWidgets.QLineEdit(self._read_field("supply_V"))
        ed_i = QtWidgets.QLineEdit(self._read_field("supply_ilimit_A"))
        rf.addRow("Supply V (optional)", ed_v)
        rf.addRow("Current A (optional)", ed_i)
        v.addLayout(rf)

        note = QtWidgets.QLabel("")
        note.setWordWrap(True); v.addWidget(note)

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        ok_btn = bb.button(QtWidgets.QDialogButtonBox.Ok)
        v.addWidget(bb)

        def refresh():
            if not flowing:
                ok_btn.setEnabled(False); ok_btn.setText("Waiting for data…")
                note.setStyleSheet("color:#ef6c00;")
                note.setText("No telemetry yet (0 frames). Make sure the NUCLEO is powered and the "
                             "streaming firmware is running; click Cancel, then ⚡ Build · Flash · Run, "
                             "and watch the Frame rate card.")
                return
            ok_btn.setEnabled(True)
            warn = not all_advisory_ok or not all(b.isChecked() for b in boxes)
            ok_btn.setText("Record (provisional)" if warn else "Confirm && record")
            if not all_advisory_ok:
                note.setStyleSheet("color:#ef6c00;")
                note.setText("Some signal checks are WARN (e.g. low frame integrity). You can still "
                             "record (only validated frames are saved), but consider why.")
            else:
                note.setText("")
        for b in boxes:
            b.toggled.connect(refresh)
        refresh()
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return None
        checklist_done = all(b.isChecked() for b in boxes)
        verdict = "PASS" if (all_advisory_ok and checklist_done) else "PROVISIONAL"
        return {
            "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "live_checks": {n: {"pass": bool(ok), "value": str(val)} for n, ok, val, _ in live},
            "live_pass": bool(all_advisory_ok),
            "checklist": {b.text(): bool(b.isChecked()) for b in boxes},
            "refs": {"supply_V_meas": ed_v.text(), "current_meas": ed_i.text()},
            "verdict": verdict,
        }

    # =====================================================  tick
    def _autoconnect_retry(self):
        """Keep trying to attach to the board instead of giving up after one go."""
        try:
            if self.source is not None:
                return                                  # already connected
            if not self.config.get("auto_detect_stlink", True):
                return
            if find_stlink_port():
                self._toggle_connect()
        except Exception:
            pass                                        # never let a retry kill the UI

    def _poll_cmd_file(self):
        """Backend command channel (2026-07-30, operator-requested)."""
        path = os.path.join(self.live.dir, "cmd.json")
        try:
            if not os.path.exists(path):
                return
            if time.time() - os.path.getmtime(path) > 10.0:
                os.remove(path)
                return
            with io.open(path, encoding="utf-8") as fh:
                req = json.load(fh)
            os.remove(path)
            batch = req.get("batch")
            if batch is not None:
                conds = (self.tax.data.get("conditions")
                         or cov.DEFAULT_PLAN["conditions"])
                if batch in conds and not getattr(self, "_run_active", False)                         and not getattr(self, "_cd", None) and self.source:
                    self._batch_cond = batch
                    self._batch_state_write(batch)
                    self.live.event(f"cmd.json -> BATCH {batch}")
                    self._batch_step()
                return
            rpm = req.get("cmd_rpm")
            if rpm is None:
                return
            rpm = max(0, min(2310, int(rpm)))
            self.live.event(f"cmd.json -> S{rpm}")
            self._set_speed_live(rpm)
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass

    def _tick(self):
        self._mon_tick = getattr(self, "_mon_tick", 0) + 1
        if self._mon_tick % 10 == 0:
            self._update_monitor()
        if self._mon_tick % 40 == 0:          # ~2 s: refresh the live status file for review
            self._write_live_status()
        if self._mon_tick % 10 == 0:          # ~0.5 s: backend command channel
            self._poll_cmd_file()
        if not self.source:
            return
        if isinstance(self.source, SerialSource):
            state = (self.source.connected, self.source.last_error)
            if state != getattr(self, "_serial_state", None):
                self._serial_state = state
                port = getattr(self, "_detected_port", None) or "?"
                if self.source.connected:
                    self.lbl_hw.setText(f"●  Streaming: {port}")
                    self.lbl_hw.setStyleSheet("border:0; color:#8fdca0;")
                    self.status.showMessage(f"Streaming {port} (ST-LINK).")
                else:
                    self.lbl_hw.setText(f"○  Waiting for {port}…")
                    self.lbl_hw.setStyleSheet("border:0; color:#ef6c00;")
                    self.status.showMessage(
                        f"Waiting for {port}: {self.source.last_error or '...'}  "
                        "(auto-retry; free it from CubeMonitor?)")
        new_frames = 0
        for line in self.source.drain():
            frame, _ = parse_and_validate(line, self.stats)
            if frame is None:
                continue
            self._augment_current(frame)
            self._augment_temp(frame)
            # Label-gate accumulator: EVERY frame the session records, no sampling.
            # The 1 Hz tick-sampled version misread three perfect 2100-rpm drag runs
            # as ~1400 (2026-07-30); summing the recorded population is exact.
            if self.session.active and getattr(self, "_cd", None) is not None:
                self._cd["rpm_sum"] = self._cd.get("rpm_sum", 0.0) + float(frame.get("rpm", 0))
                self._cd["rpm_n"] = self._cd.get("rpm_n", 0) + 1
            self.last_frame = frame
            # Wall-clock stamp of the last frame that actually PARSED. Reported as
            # frame_age_s so a stale display can be told apart from a live one from
            # outside the GUI: the displayed labels hold their last value when the
            # stream stops, which looks identical to fabricated data.
            self._last_frame_t = time.monotonic()
            new_frames += 1
            for k in self.buf:
                self.buf[k].append(frame[k])
            if self.session.active:
                self.session.write_frame(frame)
            # Severity must advance ONCE PER REAL FRAME (the EWMA/CUSUM/persistence are
            # per-sample). The tick fires ~3x faster than frames arrive, so updating here
            # (not once per tick) keeps drift detection accurate.
            if self.armed:
                sev, _ = self.sev.update(frame["ripple_permil"])
                self._set_sev_label(sev)
        # Nothing new this tick, or not on the Monitor tab -> skip the card repaint work.
        if new_frames == 0 or self.tabs.currentIndex() != 0 or self.last_frame is None:
            return
        f = self.last_frame
        ill_buf = self.buf["illegal"]
        rise = (f["illegal"] - ill_buf[0]) if ill_buf else 0
        rec = "  ● REC" if self.session.active else ""
        self.val_rpm.setText(str(f["rpm"]))
        self.val_ripple.setText(str(f["ripple_permil"]))
        self.val_hall.setText(str(f["hall"]))
        self.val_illegal.setText(f"+{rise}")
        # coast_ms is only meaningful AFTER a real spin-down: the firmware computes it
        # as (last_edge - coast_t0)/1000, and before the first coast coast_t0 is still
        # 0, so it reports elapsed uptime: which is how the card came to show
        # "4153371 ms" (69 minutes). Show a dash until the number is a plausible
        # spin-down; this motor coasts to a stop in about 1 s from full speed.
        _cm = f.get("coast_ms") or 0
        self.val_coast.setText(f"{_cm}" if 0 < _cm <= 120000 else NO_VALUE)
        current_a = f.get("i_dc_a")
        # Render this exact frame. There is no display median, clamp, zero tracking,
        # or fallback to the legacy filtered i_dc_mv channel.
        raw_factory_mv = f.get("i_adc_factory_mv")
        raw_code = f.get("i_adc_raw_mean")
        raw_n = f.get("i_adc_raw_count", 0)
        code_text = f"{raw_code:.6f} cnt" if raw_code is not None else "no raw code"
        mv_text = (f"{raw_factory_mv:.6f} mV"
                   if raw_factory_mv is not None else "no VREF pair")
        # Card shows ONLY the realtime current and the ADC millivolts it was computed
        # from (operator-specified 2026-07-30). Peak/min and the raw aggregates are
        # still computed and LOGGED for the fault model, just not displayed.
        _adc_mv = f.get("i_dc_mv")
        _adc_txt = f"{float(_adc_mv):.0f} mV" if _adc_mv is not None else "no ADC"
        if current_a is None and f.get("i_dc_zeroing"):
            # Brief (<1 s once settled): the anchor now normalizes by parked sector
            # instead of waiting for a specific one, so this state can no longer
            # persist. See the sector-normalized anchor in _augment_current.
            self.val_current.setText(
                f"ZEROING {f.get('i_dc_ratio_pending', 0)}/8  ·  {_adc_txt}")
        elif current_a is None and f.get("i_dc_uncalibrated"):
            self.val_current.setText(
                f"UNCALIBRATED | ADC {code_text} | {mv_text} | n={raw_n}")
        elif current_a is None:
            self.val_current.setText(f"RAW ADC UNAVAILABLE | n={raw_n}")
        else:
            # Per-frame display, no smoothing (operator-requested 2026-07-30: the
            # median made the card look frozen into "predetermined slots"). The
            # visible stepping between adjacent values IS the firmware's integer-mV
            # quantization (1 mV = ~3.8 mA), real data, same as the healthy era.
            self.val_current.setText(f"{current_a:.3f} A  ·  {_adc_txt}")

        st = decode_status(f.get("sensor_status")) if "sensor_status" in f else None
        vib_rms = f.get("accel_rms_mg", 0)
        vib_pk = f.get("accel_pk_mg", 0)
        if st is not None and not st["kx_id"]:
            self.val_vib.setText("NOT DETECTED")
        elif st is not None and not st["accel"]:
            self.val_vib.setText("FAULT · not reading 1 g")
        else:
            self.val_vib.setText(f"{vib_rms} rms  ·  {vib_pk} pk  mg")
        temp_f = f.get("temp_motor_f")
        temp_raw = f.get("temp_mv", 0)
        if temp_f is not None:
            self.val_temp.setText(f"{temp_f:.1f} °F  ·  {temp_raw} mV")
        else:
            self.val_temp.setText(f"FAULT · probe open/short  ·  {temp_raw} mV")
        # one-line, per-channel verdict: the whole rig at a glance
        if st is None:
            self.val_sensors.setText(NO_VALUE)
        else:
            seen = len(st["hall_seen"])
            parts = [f"ADC {'ok' if st['adc'] else 'FAULT'}",
                     f"CURR {'ok' if st['current'] else 'FAULT'}",
                     f"TEMP {'ok' if st['temp'] else 'FAULT'}",
                     f"ACCEL {'ok' if st['accel'] else 'FAULT'}",
                     f"HALL {'ok' if st['hall'] else 'FAULT'} {seen}/6"]
            self.val_sensors.setText("   ".join(parts))
        self.val_rate.setText(self._frame_rate_text())
        # Frame-integrity status pill: green GOOD / amber OK / red LOW (restyle only on band change)
        if not self.stats.total:
            gtxt, gcol = NO_VALUE, CHIP_NEUTRAL
        else:
            gp = self.stats.good_pct
            gtxt = f"GOOD {gp:0.0f}%" if gp >= 99 else (f"OK {gp:0.0f}%" if gp >= 90 else f"LOW {gp:0.0f}%")
            gcol = CHIP_COLOR[0] if gp >= 99 else (CHIP_COLOR[1] if gp >= 90 else CHIP_COLOR[3])
        self.val_good.setText(gtxt + rec)
        if gcol != getattr(self, "_gcol_last", None):
            self.val_good.setStyleSheet(_chip_css(gcol)); self._gcol_last = gcol
        self.lbl_read.setText(self.stats.as_text())

    def closeEvent(self, e):
        self._disconnect(); e.accept()
