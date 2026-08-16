"""Check that every calibration constant has exactly one value.

    python tools/check_calibration.py
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bldc_phm import calibration as cal  # noqa: E402

# Front ends must import the shared module, never restate the physics.
FRONTENDS = [
    "apps/demo/collector.py",
    "apps/web/server.py",
    "apps/bench/ui/main_window.py",
    "tools/validate_rules.py",
    "tools/train_model.py",
]
FORBIDDEN = [
    (r"\b3950\b", "NTC beta 3950 (the bench uses 3550)"),
    (r"1\.0\s*/\s*298\.15", "inline Steinhart/beta equation"),
    (r"MV_PER_AMP\s*=\s*[\d.]+", "local copy of MV_PER_AMP"),
    (r"IDLE_OFFSET_A\s*=\s*[\d.]+", "local copy of IDLE_OFFSET_A"),
    # The gravity baseline used to be restated as a bare G0 = (...) literal in
    # tools/train_model.py; that is the same drift the sensor equations had.
    (r"^G0\s*=\s*\([-\d.,\s]+\)", "local copy of the orientation baseline"),
]


def main() -> int:
    problems = []

    cfg = cal.load_config()
    if not cfg:
        print("app_config.yaml unreadable (PyYAML missing?); "
              "skipping the config comparison")
    else:
        values = cal.config_values(cfg)
        baked = {
            "mv_per_amp": cal.MV_PER_AMP,
            "idle_offset_a": cal.IDLE_OFFSET_A,
            "beta": cal.NTC_BETA,
            "r0_ohm": cal.NTC_R0_OHM,
            "t0_k": cal.NTC_T0_K,
            "series_ohm": cal.NTC_SERIES_OHM,
            "vin_v": cal.NTC_VIN_V,
        }
        print(f"{'constant':<16} {'config':>12} {'baked':>12}")
        for key, baked_value in baked.items():
            config_value = values[key]
            ok = abs(config_value - baked_value) < 1e-9
            print(f"{key:<16} {config_value:>12} {baked_value:>12}"
                  f"{'' if ok else '   <-- MISMATCH'}")
            if not ok:
                problems.append(
                    f"{key}: app_config.yaml says {config_value}, "
                    f"calibration.py bakes {baked_value}")

        accel = cfg.get("accel_sensor") or {}
        baseline = tuple(accel.get("baseline_mg", cal.ORIENTATION_BASELINE_MG))
        print(f"{'orientation_mg':<16} {str(baseline):>12} "
              f"{str(cal.ORIENTATION_BASELINE_MG):>12}"
              f"{'' if baseline == cal.ORIENTATION_BASELINE_MG else '   <-- MISMATCH'}")
        if baseline != cal.ORIENTATION_BASELINE_MG:
            problems.append(f"accel baseline: config {baseline}, "
                            f"baked {cal.ORIENTATION_BASELINE_MG}")
        demo = accel.get("demo_mount") or {}
        raw = tuple(demo.get("raw_baseline_mg", cal.DEMO_MOUNT_RAW_BASELINE_MG))
        if raw != cal.DEMO_MOUNT_RAW_BASELINE_MG:
            problems.append(f"demo raw baseline: config {raw}, "
                            f"baked {cal.DEMO_MOUNT_RAW_BASELINE_MG}")

    print("\nfront ends importing the shared module:")
    for rel in FRONTENDS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            problems.append(f"{rel}: missing")
            continue
        text = open(path, encoding="utf-8").read()
        hits = [why for pattern, why in FORBIDDEN
                if re.search(pattern, text, re.MULTILINE)]
        status = "clean" if not hits else "; ".join(hits)
        print(f"  {rel:<34} {status}")
        problems.extend(f"{rel}: {why}" for why in hits)

    if problems:
        print("\nCALIBRATION IS NOT SINGLE-SOURCED")
        for problem in problems:
            print(" -", problem)
        return 1

    print("\nall calibration constants have exactly one definition")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
