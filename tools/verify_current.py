"""Current-channel verification / calibration against an independent meter."""

import os
import sys
import csv
import json
import argparse
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS = os.path.join(ROOT, "data", "_live", "status.json")
OUTDIR = os.path.join(ROOT, "data", "verification")

# 3.3 V PWM into the driver's 5 V-referenced SV caps the reachable speed at
# ~2300 rpm, so the ladder stops at 2000.
DEFAULT_POINTS = [0, 500, 800, 1100, 1400, 1700, 2000]

KEYSIGHT_HINTS = ("34460", "34461", "34465", "EDU34450")


# --------------------------------------------------------------------------- #
def read_board(max_age_s=15.0):
    """Live snapshot from the running app. Returns dict or None."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return None
    try:
        upd = datetime.datetime.strptime(s.get("updated", ""), "%Y-%m-%d %H:%M:%S")
        upd = upd.replace(tzinfo=datetime.timezone.utc)
        if (datetime.datetime.now(datetime.timezone.utc) - upd).total_seconds() > max_age_s:
            return None            # stale: the app is not actually streaming
    except Exception:
        pass
    if not s.get("connected"):
        return None
    return s


def open_dmm():
    """Return a pyvisa handle to a Keysight DMM set to DC current, or None."""
    try:
        import pyvisa
    except ImportError:
        print("  (pyvisa not installed -> manual entry)")
        return None
    try:
        rm = pyvisa.ResourceManager()
        for res in rm.list_resources("?*"):
            try:
                inst = rm.open_resource(res, timeout=6000)
                idn = inst.query("*IDN?").strip()
                if any(h in idn for h in KEYSIGHT_HINTS):
                    inst.write("CONF:CURR:DC AUTO")
                    inst.write("CURR:DC:NPLC 10")     # quiet, 6.5-digit class
                    print(f"  DMM: {idn.split(',')[1]} ({res}) -> DC current")
                    return inst
                inst.close()
            except Exception:
                pass
    except Exception as exc:
        print(f"  (VISA error: {exc} -> manual entry)")
    return None


def dmm_amps(inst, n=5):
    """Median of n readings: immune to a single glitch."""
    vals = []
    for _ in range(n):
        try:
            vals.append(float(inst.query("READ?")))
        except Exception:
            pass
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def fit(xs, ys):
    """Least-squares y = m*x + b. Returns (m, b, r2, max_abs_resid)."""
    n = len(xs)
    if n < 2:
        return None
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    den = n * sxx - sx * sx
    if abs(den) < 1e-12:
        return None
    m = (n * sxy - sx * sy) / den
    b = (sy - m * sx) / n
    ybar = sy / n
    ss_tot = sum((y - ybar) ** 2 for y in ys)
    resid = [y - (m * x + b) for x, y in zip(xs, ys)]
    ss_res = sum(r * r for r in resid)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-15 else 1.0
    return m, b, r2, max(abs(r) for r in resid)


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dmm", action="store_true", help="auto-read a Keysight DMM over USB")
    ap.add_argument("--points", type=str, default="",
                    help="comma-separated rpm points (default 0,500,800,1100,1400,1700,2000)")
    args = ap.parse_args()

    points = ([int(p) for p in args.points.split(",") if p.strip()]
              if args.points else list(DEFAULT_POINTS))

    print("=" * 72)
    print("  CURRENT CHANNEL VERIFICATION, board vs independent meter")
    print("=" * 72)
    if read_board() is None:
        print("\nThe app is not streaming. Open BLDC Motor Bench, connect to the")
        print("board, and make sure the Monitor tab shows live values. Then re-run.")
        return 2

    inst = open_dmm() if args.dmm else None
    if args.dmm and inst is None:
        print("  no Keysight DMM found -> falling back to manual entry")

    print("\nAt each step: set that speed in the app, wait ~10 s for it to settle,")
    print("then enter the meter's amps.  [Enter]=use auto DMM  s=skip  q=finish\n")

    rows = []
    for rpm in points:
        label = "OFF (idle)" if rpm == 0 else f"{rpm} rpm"
        print("-" * 72)
        print(f"SET THE APP TO: {label}")
        if inst is None:
            ans = input(f"  meter amps at {label} (s/q): ").strip().lower()
            if ans == "q":
                break
            if ans in ("s", ""):
                continue
            try:
                meter = float(ans)
            except ValueError:
                print("  not a number, skipping")
                continue
        else:
            ans = input(f"  press Enter when settled at {label} (s/q): ").strip().lower()
            if ans == "q":
                break
            if ans == "s":
                continue
            meter = dmm_amps(inst)
            if meter is None:
                print("  DMM read failed, skipping")
                continue
            print(f"  meter: {meter:+.4f} A")

        b = read_board()
        if b is None:
            print("  ! board snapshot stale/disconnected: skipped")
            continue
        rows.append({
            "iso_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "setpoint_rpm": rpm,
            "measured_rpm": b.get("rpm"),
            "i_dc_mv": b.get("current_sensor_mv"),
            "zero_mv": b.get("current_zero_mv"),
            "board_a": b.get("current_a"),
            "meter_a": round(meter, 4),
            "temp_f": b.get("temp_f"),
            "sensor_status_ok": (b.get("current_a") is not None),
        })
        print(f"  board: rpm={b.get('rpm')}  i_dc_mv={b.get('current_sensor_mv')}  "
              f"amps={b.get('current_a')}")

    if inst is not None:
        try:
            inst.close()
        except Exception:
            pass

    if not rows:
        print("\nNo points captured.")
        return 1

    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTDIR, f"current_verify_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # analysis
    print("\n" + "=" * 72)
    print("  RESULTS")
    print("=" * 72)

    idle = next((r["meter_a"] for r in rows if r["setpoint_rpm"] == 0), None)
    if idle is None:
        print("\nNo OFF point captured, so the driver's idle draw is unknown.")
        print("The meter includes it and the board does not, expect a constant")
        print("offset between the two columns below. Re-run including 0 rpm.")
        idle = 0.0
    else:
        print(f"\nDriver idle (meter at 0 rpm) = {idle:.4f} A")
        print("The board subtracts this by design, so it is removed below.")

    print(f"\n{'setpt':>6} {'rpm':>6} {'i_dc_mv':>8} {'zero':>7} "
          f"{'board A':>8} {'meter-idle':>11} {'diff':>8}")
    xs, ys = [], []
    for r in rows:
        if r["setpoint_rpm"] == 0:
            continue
        motor_meter = r["meter_a"] - idle
        ba = r["board_a"]
        diff = (ba - motor_meter) if ba is not None else None
        print(f"{r['setpoint_rpm']:>6} {str(r['measured_rpm']):>6} "
              f"{str(r['i_dc_mv']):>8} {str(r['zero_mv']):>7} "
              f"{('%.3f' % ba) if ba is not None else 'FAULT':>8} "
              f"{motor_meter:>11.3f} {('%+.3f' % diff) if diff is not None else '   -':>8}")
        if r["i_dc_mv"] is not None and r["zero_mv"] is not None:
            xs.append(motor_meter)                       # true amps (independent)
            ys.append(float(r["i_dc_mv"]) - float(r["zero_mv"]))   # sensor mV
        if ba is not None:
            pass

    res = fit(xs, ys) if len(xs) >= 2 else None
    if res:
        m, b, r2, worst = res
        print("\n--- empirical calibration  (sensor mV vs meter amps) ---")
        print(f"  slope      = {m:8.2f} mV/A     (datasheet: 250.00 = 50 V/V x 5 mOhm)")
        print(f"  error      = {abs(m) - 250.0:+8.2f} mV/A  ({(abs(m)-250.0)/2.5:+.1f} %)")
        print(f"  intercept  = {b:8.2f} mV        (should be ~0 after auto-zero)")
        print(f"  linearity  = {r2:8.4f} r^2      (1.000 = perfectly linear)")
        print(f"  worst resid= {worst:8.2f} mV     ({worst/abs(m) if m else 0:.3f} A)")
        print("\n  To adopt the measured slope, set in bldc_phm/config/app_config.yaml:")
        print(f"      current_sensor.mv_per_amp: {abs(m):.1f}")
        if r2 < 0.98:
            print("\n  ! r^2 below 0.98; points are not on a line. Check that the")
            print("    meter is in series and that each point had time to settle.")
    else:
        print("\nNeed at least two non-zero points for a calibration fit.")

    print(f"\nSaved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
