"""Train the predictive fault detector from the campaign dataset."""
import argparse, glob, io, csv, json, os, sys, math, random
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT); sys.path.insert(0, ROOT)

WIN, STRIDE = 10, 5          # 1 s windows, 0.5 s hop

# Nominal healthy orientation baseline, imported rather than restated so it cannot
# drift from bldc_phm/calibration.py the way the sensor equations once did.
# This is only the fallback: load_runs() prefers each run's own measured
# orientation_baseline_mg from meta.json, which is (-11.7, -21.0, 1050.1).
from bldc_phm.calibration import ORIENTATION_BASELINE_MG as G0
CLASSES = ["healthy", "loose_mount", "rotor_drag", "overheat", "orientation_change"]

FEATURES = [
    "amps_mean", "amps_std", "amps_max",
    "rpm_mean", "rpm_std",
    "ripple_mean", "mech1x_mean",
    "vib_rms_mean", "vib_pk_max",
    "temp_c_mean", "temp_slope_c_per_s",
    "grav_x_mean", "grav_y_mean", "grav_z_mean",
    "tilt_deg", "tilt_std_deg",
]


def tilt_deg(x, y, z):
    mag = math.sqrt(x*x + y*y + z*z) or 1.0
    g0m = math.sqrt(sum(v*v for v in G0))
    c = (x*G0[0] + y*G0[1] + z*G0[2]) / (mag * g0m)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def load_runs():
    runs = []
    for m in sorted(glob.glob("data/mounted_baseline/**/meta.json", recursive=True)):
        meta = json.load(io.open(m, encoding="utf-8"))
        if meta.get("valid_for_classifier") != "true":
            continue
        rows = list(csv.DictReader(io.open(os.path.join(os.path.dirname(m), "data.csv"),
                                           encoding="utf-8")))
        # Runs recorded before per-axis gravity streaming carry no accel_x/y/z at
        # all, so their orientation is a constant rather than a measurement.
        measured_gravity = bool(rows) and "accel_x_mg" in rows[0]
        ob = meta.get("orientation_baseline_mg") or {}
        frames = []
        for r in rows:
            try:
                gx = float(r["accel_x_mg"]) if r.get("accel_x_mg") not in ("", None) \
                    else float(ob.get("x", G0[0]))
                gy = float(r["accel_y_mg"]) if r.get("accel_y_mg") not in ("", None) \
                    else float(ob.get("y", G0[1]))
                gz = float(r["accel_z_mg"]) if r.get("accel_z_mg") not in ("", None) \
                    else float(ob.get("z", G0[2]))
                frames.append((
                    float(r["i_dc_a"]), float(r["rpm"]), float(r["ripple_permil"]),
                    float(r["mech1x_permil"]), float(r["accel_rms_mg"]),
                    float(r["accel_pk_mg"]), float(r["temp_motor_c"]),
                    gx, gy, gz,
                ))
            except (ValueError, KeyError, TypeError):
                continue
        if len(frames) >= WIN:
            runs.append({"sid": meta["session_id"], "cond": meta["condition"],
                         "frames": np.array(frames),
                         "measured_gravity": measured_gravity})
    return runs


def audit_gravity_provenance(runs):
    """Print how gravity provenance lines up with the class labels."""
    table = {}
    for r in runs:
        slot = table.setdefault(r["cond"], [0, 0])
        slot[0 if r["measured_gravity"] else 1] += 1

    print("\ngravity provenance by class (measured / imputed):")
    for cond, (measured, imputed) in sorted(table.items()):
        print(f"  {cond:<20} {measured:>3} measured   {imputed:>3} imputed")

    split = [c for c, (m, i) in table.items() if m and not i]
    constant = [c for c, (m, i) in table.items() if i and not m]
    if split and constant:
        print(f"  WARNING: {', '.join(sorted(constant))} is entirely imputed while "
              f"{', '.join(sorted(split))} is entirely measured.")
        print("  The gravity features encode that split. Re-run with --no-gravity "
              "for the physics-only number.")
    return table


def windows(run):
    F = run["frames"]
    out = []
    for s in range(0, len(F) - WIN + 1, STRIDE):
        w = F[s:s+WIN]
        amps, rpm, rip, m1x = w[:, 0], w[:, 1], w[:, 2], w[:, 3]
        vrms, vpk, tc = w[:, 4], w[:, 5], w[:, 6]
        gx, gy, gz = w[:, 7].mean(), w[:, 8].mean(), w[:, 9].mean()
        tilts = [tilt_deg(a, b, c) for a, b, c in w[:, 7:10]]
        out.append([
            amps.mean(), amps.std(), amps.max(),
            rpm.mean(), rpm.std(),
            rip.mean(), m1x.mean(),
            vrms.mean(), vpk.max(),
            tc.mean(), (tc[-1] - tc[0]) / (WIN / 10.0),
            gx, gy, gz,
            float(np.mean(tilts)), float(np.std(tilts)),
        ])
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-gravity", action="store_true",
                    help="drop the five gravity/tilt features and train on the "
                         "measured channels only")
    args = ap.parse_args()

    rng = random.Random(42)
    runs = load_runs()
    by_class = {}
    for r in runs:
        by_class.setdefault(r["cond"], []).append(r)
    print("runs per class:", {k: len(v) for k, v in by_class.items()})
    audit_gravity_provenance(runs)

    if args.no_gravity:
        keep = [i for i, f in enumerate(FEATURES)
                if not (f.startswith("grav_") or f.startswith("tilt"))]
        suffix = "_nogravity"
        print(f"\ntraining WITHOUT gravity features "
              f"({len(keep)} of {len(FEATURES)} features)")
    else:
        keep = list(range(len(FEATURES)))
        suffix = ""
    features_used = [FEATURES[i] for i in keep]

    train_w, train_y, test_w, test_y, test_run_ids = [], [], [], [], []
    split_note = {}
    for cond, rs in by_class.items():
        if len(rs) == 1:
            # single-run class: temporal split inside the run, loudly caveated
            r = rs[0]
            ws = windows(r)
            cut = int(len(ws) * 0.7)
            train_w += ws[:cut]; train_y += [cond] * cut
            test_w += ws[cut:]; test_y += [cond] * (len(ws) - cut)
            test_run_ids += [r["sid"]] * (len(ws) - cut)
            split_note[cond] = (f"SINGLE RUN ({r['sid']}): temporal 70/30 split, "
                                "cross-run generalization UNPROVEN until more runs exist")
            continue
        rs = rs[:]; rng.shuffle(rs)
        n_test = max(1, round(len(rs) * 0.3))
        for i, r in enumerate(rs):
            ws = windows(r)
            if i < n_test:
                test_w += ws; test_y += [cond] * len(ws)
                test_run_ids += [r["sid"]] * len(ws)
            else:
                train_w += ws; train_y += [cond] * len(ws)
        split_note[cond] = (f"{len(rs)-n_test} train runs / {n_test} held-out runs")

    X_tr, y_tr = np.array(train_w)[:, keep], np.array(train_y)
    X_te, y_te = np.array(test_w)[:, keep], np.array(test_y)
    print(f"windows: train {len(X_tr)}  test {len(X_te)}")

    from sklearn.ensemble import RandomForestClassifier
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.metrics import confusion_matrix, classification_report, accuracy_score

    rf = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1,
                                class_weight="balanced")
    rf.fit(X_tr, y_tr)
    pred = rf.predict(X_te)
    acc = accuracy_score(y_te, pred)
    print(f"\nRANDOM FOREST on held-out RUN windows: accuracy {acc:.4f}")
    labels = [c for c in CLASSES if c in set(y_te) | set(y_tr)]
    cm = confusion_matrix(y_te, pred, labels=labels)
    print("confusion matrix (rows=truth):")
    print(f"{'':>20}" + "".join(f"{l[:9]:>10}" for l in labels))
    for i, l in enumerate(labels):
        print(f"{l:>20}" + "".join(f"{v:>10}" for v in cm[i]))
    print(classification_report(y_te, pred, labels=labels, digits=3))

    # per-run majority vote (what the bench will actually report)
    votes = {}
    for sid, t, p in zip(test_run_ids, y_te, pred):
        votes.setdefault(sid, {"true": t, "preds": []})["preds"].append(p)
    run_correct = sum(1 for v in votes.values()
                      if max(set(v["preds"]), key=v["preds"].count) == v["true"])
    print(f"per-RUN majority vote: {run_correct}/{len(votes)} held-out runs correct")

    # MCU tree: depth-limited, must stay close to the forest
    tree = DecisionTreeClassifier(max_depth=7, random_state=42,
                                  class_weight="balanced")
    tree.fit(X_tr, y_tr)
    tacc = accuracy_score(y_te, tree.predict(X_te))
    print(f"\nMCU DECISION TREE (depth<=7): held-out accuracy {tacc:.4f} "
          f"({tree.tree_.node_count} nodes)")

    # feature importances (what the model actually keys on)
    imp = sorted(zip(features_used, rf.feature_importances_), key=lambda x: -x[1])
    print("\ntop features:")
    for n, v in imp[:8]:
        print(f"  {n:<22} {v:.3f}")

    # artifacts
    os.makedirs("model", exist_ok=True)
    import joblib
    joblib.dump(rf, f"model/fault_model{suffix}.joblib")
    json.dump({"features": features_used, "window": WIN, "stride": STRIDE,
               "classes": sorted(set(y_tr)), "gravity_baseline": G0},
              io.open(f"model/features{suffix}.json", "w", encoding="utf-8"), indent=2)

    # compile the tree to C for the firmware
    t = tree.tree_
    class_names = tree.classes_
    lines = []
    def emit(node, ind):
        pad = "    " * ind
        if t.children_left[node] == -1:
            cls = class_names[int(np.argmax(t.value[node]))]
            code = CLASSES.index(cls)
            lines.append(f"{pad}return {code}; /* {cls} */")
            return
        f_i = t.feature[node]; thr = t.threshold[node]
        # f_i indexes the TRAINED matrix X[:, keep], i.e. features_used, NOT the
        # full FEATURES list. Indexing FEATURES here only happens to be right while
        # the dropped features are the tail of the list; reorder FEATURES or drop a
        # middle one and every emitted comment silently names the wrong channel
        # while the thresholds stay correct.
        lines.append(f"{pad}if (f[{f_i}] <= {thr:.6f}f) {{ /* {features_used[f_i]} */")
        emit(t.children_left[node], ind + 1)
        lines.append(f"{pad}}} else {{")
        emit(t.children_right[node], ind + 1)
        lines.append(f"{pad}}}")
    emit(0, 1)
    c_src = (
        "/* AUTO-GENERATED by tools/train_model.py; DO NOT EDIT.\n"
        f" * Depth-{tree.get_depth()} decision tree, {t.node_count} nodes, "
        f"held-out window accuracy {tacc:.4f}.\n"
        f" * Input: float f[{len(features_used)}] in the exact order of "
        f"model/features{suffix}.json.\n"
        " * Returns fault code: 0=healthy 1=loose_mount 2=rotor_drag 3=overheat\n"
        " *                     4=orientation_change\n */\n"
        "#include \"fault_tree.h\"\n\n"
        "int fault_tree_classify(const float *f)\n{\n" + "\n".join(lines) + "\n}\n")
    io.open(f"model/fault_tree{suffix}.c", "w", encoding="utf-8").write(c_src)
    io.open(f"model/fault_tree{suffix}.h", "w", encoding="utf-8").write(
        "#ifndef FAULT_TREE_H\n#define FAULT_TREE_H\n"
        "int fault_tree_classify(const float *f);\n"
        "#define FAULT_HEALTHY 0\n#define FAULT_LOOSE_MOUNT 1\n"
        "#define FAULT_ROTOR_DRAG 2\n#define FAULT_OVERHEAT 3\n"
        "#define FAULT_ORIENTATION 4\n#endif\n")

    json.dump({"rf_window_accuracy": acc, "tree_window_accuracy": tacc,
               "runs_correct": f"{run_correct}/{len(votes)}",
               "split_notes": split_note,
               "caveats": [
                   "overheat has ONE run: temporal split only; record more overheat "
                   "runs before trusting cross-run generalization for that class",
                   "overheat run's current channel drifted negative (heat warmed the "
                   "sense electronics); temperature is that class's true signature",
                   "28 of the 30 classifier-valid healthy runs predate per-axis gravity "
                   "streaming (30 of the 32 recorded), so their "
                   "gravity features are a constant while every fault run's are measured; "
                   "the gravity/tilt features therefore separate healthy from faulted on "
                   "data provenance. Run with --no-gravity for the physics-only number.",
               ],
               "gravity_features_used": not args.no_gravity,
               "top_features": [[n, float(v)] for n, v in imp]},
              io.open(f"model/report{suffix}.json", "w", encoding="utf-8"), indent=2)
    print(f"\nartifacts: model/fault_model{suffix}.joblib, fault_tree{suffix}.c/.h, "
          f"features{suffix}.json, report{suffix}.json")


if __name__ == "__main__":
    main()
