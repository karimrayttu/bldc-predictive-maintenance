"""Baseline builder (CLI).  Computes a state-conditioned healthy baseline from one or
more recorded healthy sessions and writes it to data/baselines/.

  python app\tools\build_baseline.py --state idle_500 --rpm 500 S001 S004 S007
  python app\tools\build_baseline.py --state idle_500 --rpm 500 --all-healthy
"""

import os
import sys
import json
import glob
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from bldc_phm import baseline as bl   # noqa: E402

# Baselines come from MOUNTED data only (commissioning/unmounted data is not valid for
# the classifier). Override with --mode if you really need to.
DATA = os.path.join(ROOT, "data")


def resolve(sess_dir, session_args, rpm, all_healthy):
    paths = []
    metas = sorted(glob.glob(os.path.join(sess_dir, "**", "meta.json"), recursive=True))
    for meta_p in metas:
        folder = os.path.dirname(meta_p)
        data_p = os.path.join(folder, "data.csv")
        if not os.path.isfile(data_p):
            continue
        meta = json.load(open(meta_p))
        sid = meta.get("session_id", "")
        name = os.path.basename(folder)
        if all_healthy:
            if meta.get("condition") == "healthy" and str(meta.get("speed_rpm_nominal")) == str(rpm):
                paths.append(data_p)
        elif sid in session_args or name in session_args:
            paths.append(data_p)
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*", help="session IDs (S001) or folder names")
    ap.add_argument("--state", required=True)
    ap.add_argument("--rpm", type=int, default=None)
    ap.add_argument("--all-healthy", action="store_true")
    ap.add_argument("--mode", default="mounted_baseline",
                    help="data subroot to read (default mounted_baseline; commissioning data is invalid)")
    a = ap.parse_args()

    mode_dir = os.path.join(DATA, a.mode)
    sess_dir = os.path.join(mode_dir, "sessions")
    paths = resolve(sess_dir, set(a.sessions), a.rpm, a.all_healthy)
    if not paths:
        print(f"No matching sessions found in {os.path.relpath(sess_dir, ROOT)}."); return
    print(f"Using {len(paths)} session(s):")
    for p in paths:
        print("  ", os.path.relpath(p, ROOT))
    base = bl.compute_baseline(paths, a.state, a.rpm)
    out = bl.save_baseline(base, os.path.join(mode_dir, "baselines"))
    print(f"\nBaseline written: {os.path.relpath(out, ROOT)}")
    for feat, s in base["features"].items():
        print(f"  {feat:14s} mu={s['mean']:.2f}  sigma={s['std']:.3f}  (n={s['n']})")
    print(f"  illegal_rise = {base['illegal_rise']}")


if __name__ == "__main__":
    main()
