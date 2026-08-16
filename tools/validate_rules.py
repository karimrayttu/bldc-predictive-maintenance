"""Validate the firmware fault-signature rules against the ENTIRE campaign dataset."""
import glob, io, csv, json, os, sys, math
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT); sys.path.insert(0, ROOT)

from bldc_phm.calibration import (        # noqa: E402
    IDLE_OFFSET_A as IDLE_A,
    MV_PER_AMP,
    ORIENTATION_BASELINE_MG as G0,
)

WIN, STEP = 10, 5                  # 1-s window, classify every 0.5 s (firmware)

# the EXACT firmware thresholds (main.c FAULT SIGNATURE RULES)
TH = dict(
    DRAG_DMV_MEAN=30.0, DRAG_DMV_MAX=90.0,
    DRAG_DEPRESS_RPM=250.0, DRAG_DEPRESS_DMV=16.0, DRAG_DEPRESS_RPMSTD=100.0,
    OVERHEAT_DT=120.0,
    WARMING_DT=25.0,               # any measurable warming blocks LOOSE claims
                                   # (healthy dT max 11.6; heat shows in seconds)
    POS_GDEV=220.0, POS_GSTD_MAX=20.0,   # a REAL tilt is steady (data: <=14.4)
    POS_GDEV_FAST=900.0,           # beyond any imposter (max 825): fire even in motion
    LOOSE_GDEV=55.0, LOOSE_GSTD=30.0, LOOSE_DMV_CEIL=20.0,
    LOOSE_WINS_FROM_FAULT=12,      # cross-bursts during other faults are <=5 s
    LOOSE_WINS_FRESH=4,            # from healthy/idle: latch in 2 s
    # demotion gates (tree fault claims need this mild backing)
    GATE_DRAG_DMV=16.0, GATE_HEAT_DT=80.0, GATE_POS_GDEV=180.0,
    GATE_LOOSE_GDEV=45.0, GATE_LOOSE_VPK=700.0,
)


def load_runs():
    runs = []
    for m in sorted(glob.glob("data/mounted_baseline/**/meta.json", recursive=True)):
        meta = json.load(io.open(m, encoding="utf-8"))
        if meta.get("valid_for_classifier") != "true":
            continue
        rows = list(csv.DictReader(io.open(os.path.join(os.path.dirname(m),
                                                        "data.csv"), encoding="utf-8")))
        ob = meta.get("orientation_baseline_mg") or {}
        fr = []
        for r in rows:
            try:
                gx = float(r["accel_x_mg"]) if r.get("accel_x_mg") not in ("", None) \
                    else float(ob.get("x", G0[0]))
                gy = float(r["accel_y_mg"]) if r.get("accel_y_mg") not in ("", None) \
                    else float(ob.get("y", G0[1]))
                gz = float(r["accel_z_mg"]) if r.get("accel_z_mg") not in ("", None) \
                    else float(ob.get("z", G0[2]))
                fr.append((float(r["i_dc_a"]), float(r["rpm"]),
                           float(r["accel_rms_mg"]), float(r["accel_pk_mg"]),
                           float(r["temp_mv"]), gx, gy, gz))
            except (ValueError, KeyError, TypeError):
                continue
        if len(fr) >= WIN:
            # commanded speed: the labeled setpoint recorded with the run
            cmd = meta.get("speed_rpm_nominal") or meta.get("speed_rpm") or meta.get("label_rpm")
            runs.append(dict(sid=meta["session_id"], cond=meta["condition"],
                             cmd=float(cmd) if cmd else None, F=np.array(fr)))
    return runs


def rule_verdict(w, tbase_mv, cmd_rpm):
    """w = frames[k,8] window. Returns (verdict, features) per the firmware ladder."""
    amps, rpm, vrms, vpk, tmv = w[:, 0], w[:, 1], w[:, 2], w[:, 3], w[:, 4]
    g = w[:, 5:8]
    dmv = (amps - IDLE_A) * MV_PER_AMP
    gdev = np.sqrt(((g - np.array(G0)) ** 2).sum(axis=1))
    f = dict(dmv_mean=dmv.mean(), dmv_max=dmv.max(),
             rpm_mean=rpm.mean(), rpm_std=rpm.std(),
             vib_pk_max=vpk.max(),
             dT=tbase_mv - tmv.mean(),
             gdev_mean=gdev.mean(), gdev_std=gdev.std())
    exp = cmd_rpm if cmd_rpm else f["rpm_mean"]
    warming = f["dT"] > TH["WARMING_DT"]
    if (f["dmv_mean"] > TH["DRAG_DMV_MEAN"] or f["dmv_max"] > TH["DRAG_DMV_MAX"]
            or (exp - f["rpm_mean"] > TH["DRAG_DEPRESS_RPM"]
                and f["dmv_mean"] > TH["DRAG_DEPRESS_DMV"]
                and f["rpm_std"] < TH["DRAG_DEPRESS_RPMSTD"])):
        return 2, f
    if f["dT"] > TH["OVERHEAT_DT"]:
        return 3, f
    # a REAL position change is a large AND steady gravity deviation; gun
    # blasts / hand shakes deviate but are choppy (gdev_std >= 28 in data).
    # Beyond 900 mg no disturbance in the data ever reached it -> fire fast
    # even while the motor is still being moved.
    if f["gdev_mean"] > TH["POS_GDEV_FAST"] or (
            f["gdev_mean"] > TH["POS_GDEV"] and f["gdev_std"] < TH["POS_GSTD_MAX"]):
        return 4, f
    # real loose = a SUSTAINED tilt offset (campaign: gdev>110 on every
    # window, streaks 200+); the rocking-only (gdev_std) path fired ONLY on
    # imposters in the whole dataset, so it is gone
    if (not warming and f["dmv_mean"] < TH["LOOSE_DMV_CEIL"]
            and f["gdev_mean"] > TH["LOOSE_GDEV"]):
        return 1, f
    return 0, f


def gates_open(f):
    """Which demotion gates would let a stray tree claim survive on this window."""
    open_ = []
    if f["dmv_mean"] >= TH["GATE_DRAG_DMV"]:
        open_.append("drag")
    if f["dT"] >= TH["GATE_HEAT_DT"]:
        open_.append("overheat")
    if f["gdev_mean"] >= TH["GATE_POS_GDEV"]:
        open_.append("position")
    if f["dmv_mean"] < TH["LOOSE_DMV_CEIL"] and (
            f["gdev_mean"] >= TH["GATE_LOOSE_GDEV"]
            or f["vib_pk_max"] >= TH["GATE_LOOSE_VPK"]):
        open_.append("loose")
    return open_


def main():
    runs = load_runs()
    print(f"{len(runs)} valid runs loaded")
    fp_hard, fp_gate = [], []
    stats = {}
    margins = dict(healthy_dmv_mean=[], healthy_dmv_max=[], healthy_gdev=[],
                   healthy_gstd=[], healthy_dT=[], healthy_vpk=[])
    for run in runs:
        F = run["F"]
        cond = run["cond"]
        # session temp baseline = median of the first second (rest/settle head)
        tbase = float(np.median(F[:WIN, 4]))
        verdicts = []
        for s in range(0, len(F) - WIN + 1, STEP):
            w = F[s:s + WIN]
            if w[:, 1].mean() < 150:        # firmware: idle below 150 rpm
                verdicts.append((15, None))
                continue
            v, f = rule_verdict(w, tbase, run["cmd"])
            verdicts.append((v, f))
            if cond == "healthy":
                margins["healthy_dmv_mean"].append(f["dmv_mean"])
                margins["healthy_dmv_max"].append(f["dmv_max"])
                margins["healthy_gdev"].append(f["gdev_mean"])
                margins["healthy_gstd"].append(f["gdev_std"])
                margins["healthy_dT"].append(f["dT"])
                margins["healthy_vpk"].append(f["vib_pk_max"])
                if v != 0:
                    fp_hard.append((run["sid"], s, v, f))
                g = gates_open(f)
                if g:
                    fp_gate.append((run["sid"], s, g, f))
        # debounce sim: fault needs 4 consecutive identical, healthy 2
        reported, cand, streak = 15, 15, 0
        first_fault_at = None
        latched = set()
        prev_gdev = None
        last_drag_i = -999
        for i, (v, f) in enumerate(verdicts):
            if v == 15:
                reported, cand, streak = 15, 15, 0
                prev_gdev = None
                continue
            if v == 1 and f is not None:
                if prev_gdev is not None and abs(f["gdev_mean"] - prev_gdev) > 12.0:
                    v = 0                      # unstable gravity: not a lean
                prev_gdev = f["gdev_mean"]
            elif f is not None:
                prev_gdev = f["gdev_mean"]
            if reported == 2:
                last_drag_i = i
            if v == reported:
                cand, streak = v, 0
            else:
                if v == cand:
                    streak += 1
                else:
                    cand, streak = v, 1
                if v == 1:
                    # drag cooldown: after DRAG was reported, loose claims
                    # need 6 s of stable evidence for the next 10 s (drag
                    # pauses produce brief loose-like bursts <= 4.5 s)
                    need = 12 if i - last_drag_i <= 120 else 6
                elif v == 0:
                    need = 2
                else:
                    need = 4
                if streak >= need:
                    reported = v
                    latched.add(v)
                    if v != 0 and first_fault_at is None:
                        first_fault_at = i * 0.5
        st = stats.setdefault(cond, dict(runs=0, detected=0, lat=[], wrong=set()))
        st["runs"] += 1
        want = {"healthy": 0, "loose_mount": 1, "rotor_drag": 2,
                "overheat": 3, "orientation_change": 4}[cond]
        at_speed = [v for v, _ in verdicts if v != 15]
        if cond == "healthy":
            if all(v == 0 for v in at_speed):
                st["detected"] += 1
            else:
                st["wrong"].add(run["sid"])
        else:
            if want in latched:
                st["detected"] += 1
                if first_fault_at is not None:
                    st["lat"].append(first_fault_at)
            else:
                st["wrong"].add(f"{run['sid']}->latched{sorted(latched)}")
            bad = latched - {want, 0}
            if bad:
                st.setdefault("cross", set()).add(f"{run['sid']}:{sorted(bad)}")

    print("\n=== 1. FALSE-POSITIVE EXPOSURE (healthy windows) ===")
    n_healthy = len(margins["healthy_dmv_mean"])
    print(f"healthy at-speed windows: {n_healthy}")
    print(f"hard-rule false fires:    {len(fp_hard)}   <- MUST be 0")
    for sid, s, v, f in fp_hard[:8]:
        print(f"   {sid} win@{s}: verdict {v}  {['%s=%.1f' % kv for kv in f.items()]}")
    print(f"demotion-gate exposure:   {len(fp_gate)} windows where a stray tree "
          f"claim could survive")
    for sid, s, g, f in fp_gate[:8]:
        print(f"   {sid} win@{s}: gates {g} dmv={f['dmv_mean']:.1f} "
              f"gdev={f['gdev_mean']:.1f} vpk={f['vib_pk_max']:.0f} dT={f['dT']:.1f}")

    print("\n=== 2. DETECTION PER CLASS (with debounce) ===")
    for cond, st in sorted(stats.items()):
        lat = (f"  median latency {np.median(st['lat']):.1f}s"
               if st["lat"] else "")
        wrong = f"  MISSES: {sorted(st['wrong'])}" if st["wrong"] else ""
        cross = (f"  CROSS-LATCHES: {sorted(st['cross'])}"
                 if st.get("cross") else "")
        print(f"{cond:>20}: {st['detected']}/{st['runs']} runs correct{lat}{wrong}{cross}")

    print("\n=== 3. THRESHOLD MARGINS (healthy worst-case vs rule) ===")
    print(f"dmv_mean  max {max(margins['healthy_dmv_mean']):6.1f}  vs DRAG rule  {TH['DRAG_DMV_MEAN']}  (gate {TH['GATE_DRAG_DMV']})")
    print(f"dmv_max   max {max(margins['healthy_dmv_max']):6.1f}  vs DRAG rule  {TH['DRAG_DMV_MAX']}")
    print(f"gdev_mean max {max(margins['healthy_gdev']):6.1f}  vs LOOSE rule {TH['LOOSE_GDEV']}  (gate {TH['GATE_LOOSE_GDEV']})")
    print(f"gdev_std  max {max(margins['healthy_gstd']):6.1f}  vs LOOSE rule {TH['LOOSE_GSTD']}")
    print(f"dT        max {max(margins['healthy_dT']):6.1f}  vs HEAT rule  {TH['OVERHEAT_DT']}  (gate {TH['GATE_HEAT_DT']})")
    print(f"vib_pk    max {max(margins['healthy_vpk']):6.0f}  vs LOOSE gate {TH['GATE_LOOSE_VPK']}")


if __name__ == "__main__":
    main()
