"""LOAD verification: does the current calibration hold when a fault is induced?"""

import os
import sys
import csv
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATUS = os.path.join(ROOT, "data", "_live", "status.json")
OUTDIR = os.path.join(ROOT, "data", "verification")


def board(max_age_s=15.0):
    """Live snapshot from the running app (avoids fighting it for the COM port)."""
    try:
        with open(STATUS, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        return None
    if not s.get("connected"):
        return None
    return s


def fit(xs, ys):
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
    ss_res = sum((y - (m * x + b)) ** 2 for x, y in zip(xs, ys))
    return m, b, (1 - ss_res / ss_tot if ss_tot > 1e-15 else 1.0)


def main():
    import yaml
    cfg = yaml.safe_load(open(os.path.join(ROOT, "bldc_phm", "config", "app_config.yaml")))
    cur = cfg["current_sensor"]
    noload_slope = float(cur["mv_per_amp"])
    idle = float(cur.get("idle_offset_a", 0.0) or 0.0)

    print("=" * 72)
    print("  LOAD VERIFICATION: is the calibration still valid under a fault?")
    print("=" * 72)
    print(f"\n  no-load calibration currently in use: {noload_slope:.1f} mV/A")
    print(f"  driver idle offset:                   {idle:.3f} A")

    if board() is None:
        print("\n  The app is not streaming. Open BLDC Motor Bench, connect, set a")
        print("  fixed speed (1000 rpm suggested), then re-run.")
        return 2

    print("\n  Hold a steady speed. Step 0 = NO drag. Then add drag each step.")
    print("  Enter the current your bench SUPPLY shows.  q = finish.\n")

    rows = []
    step = 0
    while True:
        prompt = "  step 0 (NO drag) supply amps: " if step == 0 else \
                 f"  step {step} (more drag)  supply amps: "
        ans = input(prompt).strip().lower()
        if ans in ("q", ""):
            break
        try:
            supply_a = float(ans)
        except ValueError:
            print("    not a number")
            continue
        b = board()
        if b is None:
            print("    ! app snapshot stale; skipped")
            continue
        rows.append({
            "step": step,
            "supply_a": supply_a,
            "rpm": b.get("rpm"),
            "i_dc_mv": b.get("current_sensor_mv"),
            "zero_mv": b.get("current_zero_mv"),
            "app_a": b.get("current_a"),
            "peak_a": b.get("current_peak_a"),
            "iso_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        })
        print(f"    board: rpm={b.get('rpm')}  A0={b.get('current_sensor_mv')} mV  "
              f"app={b.get('current_a')} A  peak={b.get('current_peak_a')} A")
        step += 1

    if len(rows) < 2:
        print("\n  Need at least the no-drag point plus one loaded point.")
        return 1

    os.makedirs(OUTDIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTDIR, f"load_verify_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # everything is referenced to the no-drag point, so the driver idle and the
    # sensor zero both cancel out of the slope
    base_mv = float(rows[0]["i_dc_mv"])
    base_a = float(rows[0]["supply_a"])

    print("\n" + "=" * 72)
    print(f"{'step':>5} {'supply A':>9} {'delta A':>8} {'A0 mV':>8} {'delta mV':>9} {'app A':>7}")
    xs, ys = [], []
    for r in rows:
        da = float(r["supply_a"]) - base_a
        dmv = float(r["i_dc_mv"]) - base_mv
        print(f"{r['step']:>5} {r['supply_a']:>9.3f} {da:>8.3f} {r['i_dc_mv']:>8} "
              f"{dmv:>+9.2f} {str(r['app_a']):>7}")
        if r["step"] > 0:
            xs.append(da)
            ys.append(dmv)

    res = fit(xs, ys) if len(xs) >= 2 else None
    print("\n--- SLOPE UNDER LOAD ---")
    if res:
        m, b, r2 = res
        print(f"  measured under load : {m:8.1f} mV/A   (r2 {r2:.4f})")
        print(f"  no-load calibration : {noload_slope:8.1f} mV/A")
        diff = (m - noload_slope) / noload_slope * 100.0
        print(f"  difference          : {diff:+8.1f} %")
        print()
        if abs(diff) <= 10:
            print("  VERDICT: LINEAR. The no-load calibration holds across the fault")
            print("           range: no change needed. Fault currents are trustworthy.")
        else:
            print("  VERDICT: NOT LINEAR across the range. The sense path behaves")
            print("           differently under load (most likely a non-Kelvin tap whose")
            print("           contact resistance shifts with self-heating).")
            print("           -> inspect the IN+/IN- tap points, and until fixed use")
            print(f"              mv_per_amp: {m:.1f} for loaded/fault data.")
    elif len(xs) == 1:
        m = ys[0] / xs[0] if xs[0] else 0
        print(f"  single loaded point -> {m:.1f} mV/A vs {noload_slope:.1f} no-load "
              f"({(m-noload_slope)/noload_slope*100:+.1f} %)")
        print("  (take 3+ loaded steps for a proper fit)")

    print(f"\n  saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
