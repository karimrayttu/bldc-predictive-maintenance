"""Flag runs whose bus current is offset by a bad zero anchor.

    python tools/check_current_anchor.py
"""

from __future__ import annotations

import csv
import glob
import io
import os
import statistics as st
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from bldc_phm.calibration import MV_PER_AMP        # noqa: E402

TURNING_RPM = 150.0      # below this the motor is coasting or parked
EXCURSION_NOTE = 0.05    # fraction of negative frames worth reporting


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarise(path: Path):
    zeros, mvs, amps, negative, turning = [], [], [], 0, 0
    for row in csv.DictReader(io.open(path, encoding="utf-8")):
        z, mv, a, rpm = (num(row.get("i_dc_zero_mv")), num(row.get("i_dc_mv")),
                         num(row.get("i_dc_a")), num(row.get("rpm")))
        if z is not None:
            zeros.append(z)
        if mv is not None:
            mvs.append(mv)
        if a is not None:
            amps.append(a)
        if rpm is not None and rpm >= TURNING_RPM and a is not None:
            turning += 1
            negative += a < 0
    if not turning:
        return None
    return {
        "zero_mv": st.median(zeros) if zeros else None,
        "raw_mv": st.median(mvs) if mvs else None,
        "amps": st.median(amps) if amps else None,
        "turning": turning,
        "negative": negative,
        "fraction": negative / turning,
    }


def main() -> int:
    runs = []
    for path in sorted(glob.glob(str(REPO / "data" / "*" / "sessions" / "*" / "*" / "*" / "data.csv"))):
        parts = Path(path).parts
        stats = summarise(Path(path))
        if stats:
            stats.update(condition=parts[-4], speed=parts[-3], run=parts[-2])
            runs.append(stats)

    if not runs:
        print("no runs found")
        return 1

    healthy = [r["zero_mv"] for r in runs
               if r["condition"] == "healthy" and r["zero_mv"] is not None]
    reference = st.median(healthy) if healthy else None
    print(f"{len(runs)} runs with turning frames")
    if reference is not None:
        print(f"healthy median zero anchor: {reference:.0f} mV "
              f"({len(healthy)} runs)")

    flagged = [r for r in runs if r["amps"] is not None and r["amps"] <= 0]
    print(f"\nruns whose median turning current is at or below zero: {len(flagged)}")
    for r in sorted(flagged, key=lambda r: r["amps"]):
        shift = (r["zero_mv"] - reference) if (reference and r["zero_mv"]) else None
        print(f"  {r['condition']}/{r['speed']}/{r['run']}")
        print(f"    {r['negative']} of {r['turning']} turning frames negative "
              f"({r['fraction']:.0%}), median {r['amps']:+.4f} A")
        print(f"    raw sensor {r['raw_mv']:.0f} mV, zero anchor "
              f"{r['zero_mv']:.0f} mV", end="")
        if shift is not None:
            print(f", {shift:+.0f} mV against healthy = {shift / MV_PER_AMP:+.3f} A")
        else:
            print()

    if flagged:
        print("\nThe raw sensor reading is the thing to compare: where it matches\n"
              "the healthy runs but the derived amps do not, the anchor is what\n"
              "moved, not the current.")

    noisy = [r for r in runs
             if r["fraction"] > EXCURSION_NOTE and r not in flagged]
    if noisy:
        print(f"\nruns with negative excursions but a positive median, which is "
              f"bus ripple rather than an offset: {len(noisy)}")
        for r in sorted(noisy, key=lambda r: -r["fraction"]):
            print(f"  {r['condition']}/{r['speed']}/{r['run']}  "
                  f"{r['fraction']:.0%} of frames negative, median "
                  f"{r['amps']:+.4f} A, raw {r['raw_mv']:.0f} mV")

    return 1 if flagged else 0


if __name__ == "__main__":
    raise SystemExit(main())
