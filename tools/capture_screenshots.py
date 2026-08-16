"""Capture the application screenshots used in the documentation.

    python tools/capture_screenshots.py
    python tools/capture_screenshots.py --run data/mounted_baseline/sessions/healthy/0900rpm/S010_20260730-190545
"""

from __future__ import annotations

import argparse
import csv
import glob
import io
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
os.chdir(REPO)
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

# Qt's offscreen platform renders no text on Windows, so the screenshots are
# taken on the real platform. Windows are shown, grabbed and closed immediately.
os.environ.pop("QT_QPA_PLATFORM", None)

import numpy as np                                   # noqa: E402
from PyQt5 import QtCore, QtWidgets                  # noqa: E402

from bldc_phm.schema import STREAM_COLUMNS           # noqa: E402
import validate_rules as rules                       # noqa: E402

WINDOW = rules.WIN
LIVE_FRAMES = 25          # tail replayed at the recording's real 10 Hz
OUT = REPO / "docs" / "screenshots"

BENCH_TABS = [
    (0, "bench_monitor", "Monitor"),
    (1, "bench_connection", "Connection"),
    (2, "bench_coverage", "Coverage"),
    (3, "bench_wiring", "Wiring"),
]

# The Connection page lists where each STM32 tool was found on this machine.
# Those are absolute paths carrying a user name, so the block is masked in the
# published image. Left, top, right, bottom in the grabbed image.
MASK_REGIONS = {"bench_connection": [(150, 405, 1210, 515)]}


def load_run(run_dir: Path):
    """Every recorded frame, in order, starting from the run's rest head."""
    rows = list(csv.DictReader(io.open(run_dir / "data.csv", encoding="utf-8")))
    if len(rows) < WINDOW * 3:
        raise SystemExit(f"{run_dir} has too few frames")
    return rows


def verdict_for(rows) -> int:
    """Fault code the firmware ladder produces for this window."""
    frames = []
    for r in rows:
        frames.append((float(r["i_dc_a"]), float(r["rpm"]),
                       float(r["accel_rms_mg"]), float(r["accel_pk_mg"]),
                       float(r["temp_mv"]),
                       float(r["accel_x_mg"]), float(r["accel_y_mg"]),
                       float(r["accel_z_mg"])))
    window = np.array(frames)
    baseline = float(np.median(window[:, 4]))
    code, _ = rules.rule_verdict(window, baseline, float(rows[-1]["rpm"]))
    return code


def wire_line(row, fault_code: int) -> str:
    """The 44-column line the firmware would have sent for this frame."""
    values = []
    for name in STREAM_COLUMNS:
        if name == "fault_code":
            values.append(str(fault_code))
            continue
        raw = row.get(name, "")
        values.append(str(int(float(raw))) if raw not in ("", None) else "0")
    return ",".join(values)


def replay(window, rows, pace: float = 0.0):
    """Push recorded frames through the app one at a time, as the serial source
    would. A non-zero pace feeds them at their real 10 Hz spacing, which is what
    lets the frame-rate card measure a rate instead of reporting nothing."""
    for row, code in rows:
        window.source = OneShotSource(wire_line(row, code))
        window._tick()
        if pace:
            QtWidgets.QApplication.processEvents()
            time.sleep(pace)
    QtWidgets.QApplication.processEvents()


def verdicts(rows):
    """Pair each frame with the fault code the firmware ladder gives its window."""
    paired = []
    for index, row in enumerate(rows):
        start = max(0, index - WINDOW + 1)
        chunk = rows[start:index + 1]
        code = verdict_for(chunk) if len(chunk) == WINDOW else 15
        paired.append((row, code))
    return paired


class StubLink(QtCore.QObject):
    """Stands in for the Pi serial link: same signals and the same attributes
    the dashboard reads, but frames are pushed in rather than read off a port."""

    packet = QtCore.pyqtSignal(object)
    portinfo = QtCore.pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._anchor_mv = 1651.0
        self._anchored = True          # the replayed run includes its rest head
        self._flat = []
        self.anchor_snap_t = time.monotonic()

    def send_rpm(self, rpm):
        return True

    def stop(self):
        pass


class OneShotSource:
    """Stands in for the serial source for exactly one frame."""

    def __init__(self, line):
        self._lines = [line]

    def drain(self):
        lines, self._lines = self._lines, []
        return lines


def settle(cycles: int = 12):
    """Let layout, styling and the custom painters finish before grabbing."""
    for _ in range(cycles):
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)


def mask(path: Path, regions):
    """Paint over machine-specific regions after the grab."""
    if not regions:
        return
    from PIL import Image, ImageDraw
    image = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(image)
    for box in regions:
        draw.rectangle(box, fill=(24, 30, 40))
        draw.text((box[0] + 10, box[1] + 8),
                  "local tool paths omitted", fill=(120, 130, 145))
    image.save(path)


def grab(widget, path: Path, width: int, height: int, trim_bottom: int = 0):
    """Grab a window. trim_bottom drops the status bar, which prints the local
    data folder as an absolute path and has no place in a published image."""
    widget.resize(width, height)
    widget.show()
    settle()
    shot = widget.grab()
    if trim_bottom:
        shot = shot.copy(0, 0, shot.width(), shot.height() - trim_bottom)
    shot.save(str(path))
    print(f"  wrote {path.relative_to(REPO).as_posix()}")


def capture_bench(app, paired):
    from apps.bench.main import QSS, load_config
    from apps.bench.ui.main_window import MainWindow

    app.setStyleSheet(QSS)          # the theme the bench app ships with

    window = MainWindow(load_config())

    # Warm the app the way a real session does: the run's rest head lets the
    # current channel anchor its zero, then the body builds the baselines.
    warm = paired[: max(0, len(paired) - LIVE_FRAMES)]
    replay(window, warm)
    # Then feed the tail at the recording's own 10 Hz so the frame-rate card
    # measures a real rate rather than reporting nothing.
    replay(window, paired[-LIVE_FRAMES:], pace=0.1)
    settle()

    for index, name, label in BENCH_TABS:
        if index >= window.tabs.count():
            continue
        window.tabs.setCurrentIndex(index)
        settle()
        target = OUT / f"{name}.png"
        grab(window, target, 1500, 950, trim_bottom=26)
        mask(target, MASK_REGIONS.get(name))


def capture_demo(app, paired):
    from apps.demo.collector import QSS, Dashboard, cal_accel
    from bldc_phm.calibration import tilt_deg, temp_c_from_mv

    app.setStyleSheet(QSS)          # the Pi dashboard has its own type scale

    link = StubLink()
    dashboard = Dashboard(link)
    dashboard.tick.stop()          # frames are pushed here, not by a timer
    link.portinfo.emit("/dev/ttyACM0")
    def packet_for(row, code):
        gx, gy, gz, vrms, vpk = cal_accel(
            int(row["accel_x_mg"]), int(row["accel_y_mg"]), int(row["accel_z_mg"]),
            int(row["accel_rms_mg"]), int(row["accel_pk_mg"]))
        return {
            "fault_code": code,
            "rpm": int(float(row["rpm"])),
            "current_a": float(row["i_dc_a"]),
            "i_mv": int(float(row["i_dc_mv"])),
            "temp_c": temp_c_from_mv(float(row["temp_mv"])),
            "vib_rms_mg": vrms,
            "vib_pk_mg": vpk,
            "grav_x_mg": gx, "grav_y_mg": gy, "grav_z_mg": gz,
            "tilt_deg": tilt_deg(gx, gy, gz) or 0.0,
        }
    for row, code in paired[-LIVE_FRAMES:]:
        dashboard.on_packet(packet_for(row, code))
        dashboard.on_tick()        # what marks the link live
        QtWidgets.QApplication.processEvents()
        time.sleep(0.1)
    settle()
    grab(dashboard, OUT / "demo_dashboard.png", 800, 480)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only", choices=["bench", "demo"],
        help="capture one front end; without it both are captured, "
             "each in its own process so their timers cannot collide")
    parser.add_argument(
        "--run",
        default="data/mounted_baseline/sessions/rotor_drag/2100rpm/*",
        help="recorded run to drive the screenshots from")
    args = parser.parse_args()

    matches = sorted(glob.glob(args.run))
    if not matches:
        raise SystemExit(f"no run matched {args.run}")
    run_dir = Path(matches[0])
    rows = load_run(run_dir)
    # A run ends with a coast-down to zero rpm. Stop at the last turning frame so
    # the screenshots show the bench mid-run rather than parked.
    spinning = [i for i, r in enumerate(rows) if float(r["rpm"]) >= 150]
    if spinning:
        rows = rows[: spinning[-1] + 1]
    paired = verdicts(rows)
    row, fault_code = paired[-1]

    OUT.mkdir(parents=True, exist_ok=True)
    print(f"driving from {run_dir.as_posix()}")
    print(f"  frame: {row['rpm']} rpm, {row['i_dc_a']} A, "
          f"{row['accel_rms_mg']} mg, firmware verdict {fault_code}")

    if args.only is None:
        # The bench window keeps live timers running, so the two front ends are
        # captured in separate processes rather than sharing one event loop.
        import subprocess
        for which in ("bench", "demo"):
            result = subprocess.run(
                [sys.executable, __file__, "--only", which, "--run", args.run],
                cwd=REPO)
            if result.returncode != 0:
                raise SystemExit(f"{which} capture failed ({result.returncode})")
        return

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    if args.only == "bench":
        capture_bench(app, paired)
    else:
        capture_demo(app, paired)

    # Both windows own running timers that fire during interpreter shutdown and
    # take the process down with them. The screenshots are already on disk, so
    # leave immediately rather than unwinding Qt.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
