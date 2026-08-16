"""State-conditioned baseline computation."""

import os
import json
import datetime
import pandas as pd

# Features we trend for health (skip cumulative counters and raw timing).
BASELINE_FEATURES = ["rpm", "ripple_permil", "min_us", "max_us",
                     "accel_rms_mg", "accel_pk_mg", "i_dc_mv", "temp_mv"]


def compute_baseline(session_csvs, state_name, speed_rpm=None):
    """
    session_csvs: list of paths to healthy data.csv files for ONE state.
    Returns a baseline dict (also suitable to json.dump).
    """
    frames = []
    for path in session_csvs:
        try:
            df = pd.read_csv(path)
            frames.append(df)
        except Exception:
            continue
    if not frames:
        raise ValueError("no readable session CSVs")
    data = pd.concat(frames, ignore_index=True)

    stats = {}
    for feat in BASELINE_FEATURES:
        if feat in data and data[feat].notna().any():
            col = pd.to_numeric(data[feat], errors="coerce").dropna()
            stats[feat] = {
                "mean": float(col.mean()),
                "std": float(col.std(ddof=1)) if len(col) > 1 else 0.0,
                "min": float(col.min()),
                "max": float(col.max()),
                "n": int(len(col)),
            }

    # Illegal-Hall should not rise during a healthy run -> record the rise, not the sum.
    illegal_rise = 0
    if "illegal" in data:
        ill = pd.to_numeric(data["illegal"], errors="coerce").dropna()
        if len(ill):
            illegal_rise = int(ill.iloc[-1] - ill.iloc[0])

    return {
        "state": state_name,
        "speed_rpm": speed_rpm,
        "computed_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "n_sessions": len(frames),
        "n_rows": int(len(data)),
        "features": stats,
        "illegal_rise": illegal_rise,
        "source_files": [os.path.basename(p) for p in session_csvs],
    }


def save_baseline(baseline, baselines_dir):
    os.makedirs(baselines_dir, exist_ok=True)
    path = os.path.join(baselines_dir, f"baseline_{baseline['state']}.json")
    with open(path, "w") as f:
        json.dump(baseline, f, indent=2)
    return path


def load_baseline(baselines_dir, state_name):
    path = os.path.join(baselines_dir, f"baseline_{state_name}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)
