"""Re-derive mv_per_amp on the REAL board against the operator reference table."""

import argparse
import os
import statistics as st
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bldc_phm.sources import find_stlink_port          # noqa: E402
from bldc_phm.schema import STREAM_COLUMNS             # noqa: E402

# the operator's series-meter reference table (2026-07-28)
# total DC-bus current, motor commanded to each speed, measured with a meter in
# series with the 24 V feed.
REFERENCE = {400: 0.073, 800: 0.079, 1200: 0.084, 1600: 0.093, 2000: 0.101, 2400: 0.105}
IDLE_A = 0.052              # driver draw with the motor commanded OFF

OFF_SETTLE_S = 6.0          # let the 256 ms EMA fully settle (>20 tau)
OFF_SAMPLE_S = 2.5
ON_SETTLE_S = 7.0           # spin-up + EMA settle
ON_SAMPLE_S = 3.5

I_MV = STREAM_COLUMNS.index("i_dc_mv")
RPM = STREAM_COLUMNS.index("rpm")


def open_port():
    import serial
    port = find_stlink_port()
    if not port:
        print("  No ST-LINK found. Plug in the NUCLEO.")
        return None, None
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print(f"  Could not open {port}: {e}")
        print("  CLOSE the BLDC Motor Bench app first; the ST-LINK is single-owner.")
        return None, None
    ser.reset_input_buffer()
    return ser, port


def collect(ser, seconds):
    """Read frames for `seconds`; return (median i_dc_mv, median rpm, n)."""
    mvs, rpms = [], []
    end = time.monotonic() + seconds
    buf = b""
    while time.monotonic() < end:
        buf += ser.read(256)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            p = line.decode("ascii", "ignore").strip().split(",")
            if len(p) != len(STREAM_COLUMNS):
                continue
            try:
                mvs.append(int(p[I_MV])); rpms.append(int(p[RPM]))
            except ValueError:
                continue
    if not mvs:
        return None, None, 0
    return st.median(mvs), st.median(rpms), len(mvs)


def cmd(ser, rpm):
    ser.write(f"S{int(rpm)}\n".encode())
    ser.flush()


def fit_through_origin(xs, ys):
    """y = m*x through the origin (physically the intercept must be zero)."""
    num = sum(x * y for x, y in zip(xs, ys))
    den = sum(x * x for x in xs)
    return num / den if den else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="update app_config.yaml")
    ap.add_argument("--cycles", type=int, default=1)
    args = ap.parse_args()

    ser, port = open_port()
    if not ser:
        return 2
    print("=" * 72)
    print("  DC CURRENT RE-CALIBRATION, chopped OFF/ON/OFF against the reference table")
    print("=" * 72)
    print(f"  port {port}   idle {IDLE_A} A   speeds {sorted(REFERENCE)}")
    per = (OFF_SETTLE_S + OFF_SAMPLE_S + ON_SETTLE_S + ON_SAMPLE_S)
    print(f"  ~{per*len(REFERENCE)*args.cycles/60:.1f} min. Leave the motor alone, "
          f"nothing must touch the shaft.\n")

    speeds = sorted(REFERENCE)
    pts = []            # (motor_amps_reference, delta_mv)
    rows = []

    try:
        for cyc in range(args.cycles):
            cmd(ser, 0)
            time.sleep(OFF_SETTLE_S)
            off_prev, _, _ = collect(ser, OFF_SAMPLE_S)
            for sp in speeds:
                cmd(ser, sp)
                time.sleep(ON_SETTLE_S)
                on_mv, on_rpm, n_on = collect(ser, ON_SAMPLE_S)
                cmd(ser, 0)
                time.sleep(OFF_SETTLE_S)
                off_next, _, n_off = collect(ser, OFF_SAMPLE_S)
                if None in (on_mv, off_prev, off_next):
                    print(f"  {sp:>5} rpm  NO FRAMES; is the board streaming?")
                    off_prev = off_next if off_next is not None else off_prev
                    continue
                base = (off_prev + off_next) / 2.0
                d = on_mv - base
                motor_a = REFERENCE[sp] - IDLE_A
                pts.append((motor_a, d))
                rows.append((sp, on_rpm, base, on_mv, d, REFERENCE[sp], motor_a))
                print(f"  {sp:>5} rpm -> measured {on_rpm:>5}   "
                      f"base {base:7.1f}  on {on_mv:7.1f}  delta {d:+7.2f} mV   "
                      f"ref {REFERENCE[sp]:.3f} A  (motor {motor_a:.3f} A)"
                      + (f"   [{d/motor_a:7.1f} mV/A]" if motor_a > 0 else ""))
                off_prev = off_next
    finally:
        try:
            cmd(ser, 0)
            ser.close()
        except Exception:
            pass

    if len(pts) < 3:
        print("\n  Not enough points. Is the motor free to spin and the board streaming?")
        return 1

    xs = [a for a, _ in pts]
    ys = [d for _, d in pts]
    slope = fit_through_origin(xs, ys)
    per_point = [d / a for a, d in pts if a > 0]

    print("\n" + "-" * 72)
    print(f"  through-origin fit over {len(pts)} points :  {slope:.1f} mV/A")
    print(f"  per-point slopes                        :  "
          f"{', '.join(f'{v:.0f}' for v in per_point)}")
    print(f"  spread                                  :  "
          f"{min(per_point):.0f} .. {max(per_point):.0f} mV/A")
    print(f"  implied shunt (gain 50 V/V)             :  {slope/50:.2f} mOhm")
    print(f"  previous constant                       :  175.7 mV/A "
          f"(={175.7/50:.2f} mOhm)")

    # what the app would report at each speed with the new slope
    print("\n  With this slope the app would report:")
    print(f"  {'rpm':>5} {'delta mV':>9} {'app A':>8} {'ref A':>8} {'err mA':>7}")
    errs = []
    for sp, rpm_m, base, on, d, ref, motor_a in rows:
        got = d / slope + IDLE_A
        errs.append(abs(got - ref) * 1000)
        print(f"  {sp:>5} {d:>9.2f} {got:>8.3f} {ref:>8.3f} {got*1000-ref*1000:>+7.1f}")
    print(f"\n  mean |error| {st.mean(errs):.1f} mA   worst {max(errs):.1f} mA")

    if args.write:
        import re
        p = os.path.join(ROOT, "bldc_phm", "config", "app_config.yaml")
        s = open(p, encoding="utf-8").read()
        s2 = re.sub(r"^  mv_per_amp: [\d.]+", f"  mv_per_amp: {slope:.1f}", s, count=1,
                    flags=re.M)
        if s2 != s:
            open(p, "w", encoding="utf-8").write(s2)
            print(f"\n  WROTE mv_per_amp: {slope:.1f} to app_config.yaml")
        else:
            print("\n  Could not find mv_per_amp to update.")
    else:
        print(f"\n  Not written. Re-run with --write to set mv_per_amp: {slope:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
