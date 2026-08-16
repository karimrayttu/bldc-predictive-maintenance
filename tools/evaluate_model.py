"""Evaluate the fault classifier and draw the evidence for it.

    python tools/evaluate_model.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))

import train_model as tm  # noqa: E402  (also chdirs to the repo root)

from sklearn.ensemble import RandomForestClassifier  # noqa: E402
from sklearn.metrics import (auc, confusion_matrix, precision_recall_fscore_support,  # noqa: E402
                             roc_curve)
from sklearn.preprocessing import label_binarize  # noqa: E402

SURFACE, INK, INK_MUTED, GRID = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9"
CLASS_COLOR = {
    "healthy": "#2a78d6",
    "loose_mount": "#eb6834",
    "orientation_change": "#1baf7a",
    "rotor_drag": "#eda100",
    "overheat": "#e87ba4",
}
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#1c5cab", "#104281"]
GRAVITY = ("grav_", "tilt")


def style(ax, title, xlabel, ylabel):
    ax.set_title(title, color=INK, fontsize=12, pad=12, loc="left")
    ax.set_xlabel(xlabel, color=INK_MUTED, fontsize=9)
    ax.set_ylabel(ylabel, color=INK_MUTED, fontsize=9)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8, alpha=0.9)
    ax.set_axisbelow(True)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")


def split_runs(runs, seed=42):
    """The run-wise split train_model.py uses, reproduced exactly."""
    rng = random.Random(seed)
    by_class = {}
    for run in runs:
        by_class.setdefault(run["cond"], []).append(run)

    train, test = [], []
    for cond, group in by_class.items():
        if len(group) == 1:
            windows = tm.windows(group[0])
            cut = int(len(windows) * 0.7)
            train += [(w, cond, group[0]["sid"]) for w in windows[:cut]]
            test += [(w, cond, group[0]["sid"]) for w in windows[cut:]]
            continue
        group = group[:]
        rng.shuffle(group)
        n_test = max(1, round(len(group) * 0.3))
        for i, run in enumerate(group):
            rows = [(w, cond, run["sid"]) for w in tm.windows(run)]
            (test if i < n_test else train).extend(rows)
    return train, test


def as_arrays(rows, keep):
    X = np.array([r[0] for r in rows])[:, keep]
    y = np.array([r[1] for r in rows])
    sids = np.array([r[2] for r in rows])
    return X, y, sids


def fit(train, test, keep):
    X_tr, y_tr, _ = as_arrays(train, keep)
    X_te, y_te, sids = as_arrays(test, keep)
    model = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1,
                                   class_weight="balanced")
    model.fit(X_tr, y_tr)
    return model, X_te, y_te, sids


def run_vote(y_true, y_pred, sids):
    """Per-run majority vote, which is what the bench actually reports."""
    votes = {}
    for sid, truth, pred in zip(sids, y_true, y_pred):
        slot = votes.setdefault(sid, {"truth": truth, "preds": []})
        slot["preds"].append(pred)
    correct = sum(1 for v in votes.values()
                  if max(set(v["preds"]), key=v["preds"].count) == v["truth"])
    return correct, len(votes)


def plot_confusion(results, labels, out):
    fig, axes = plt.subplots(1, 2, figsize=(12.5, 5.2))
    fig.patch.set_facecolor(SURFACE)
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("blues", BLUE_RAMP)

    for ax, (name, res) in zip(axes, results.items()):
        cm = confusion_matrix(res["y_true"], res["y_pred"], labels=labels)
        norm = cm / np.maximum(cm.sum(axis=1, keepdims=True), 1)
        ax.imshow(norm, cmap=cmap, vmin=0, vmax=1)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels([l.replace("_", "\n") for l in labels], fontsize=8)
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels([l.replace("_", " ") for l in labels], fontsize=8)
        for i in range(len(labels)):
            for j in range(len(labels)):
                if cm[i, j] == 0:
                    continue
                shade = SURFACE if norm[i, j] > 0.5 else INK
                ax.text(j, i, f"{cm[i, j]}", ha="center", va="center",
                        color=shade, fontsize=9)
        ax.set_title(f"{name}\n{res['window_acc']:.4f} window accuracy, "
                     f"{res['runs_correct']}/{res['runs_total']} runs",
                     color=INK, fontsize=11, pad=10, loc="left")
        ax.set_xlabel("predicted", color=INK_MUTED, fontsize=9)
        ax.set_ylabel("actual", color=INK_MUTED, fontsize=9)
        ax.tick_params(colors=INK_MUTED, length=0)
        for side in ax.spines.values():
            side.set_visible(False)

    fig.suptitle("Held-out confusion, counts of 1-second windows",
                 color=INK, fontsize=13, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(out / "confusion_matrix.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def plot_per_class(res, labels, out):
    precision, recall, f1, support = precision_recall_fscore_support(
        res["y_true"], res["y_pred"], labels=labels, zero_division=0)

    fig, ax = plt.subplots(figsize=(10, 5.2))
    fig.patch.set_facecolor(SURFACE)
    xs, width = np.arange(len(labels)), 0.26
    for i, (vals, name, color) in enumerate((
            (precision, "precision", "#2a78d6"),
            (recall, "recall", "#eb6834"),
            (f1, "F1", "#1baf7a"))):
        bars = ax.bar(xs + (i - 1) * width, vals, width * 0.92, color=color,
                      label=name, edgecolor=SURFACE, linewidth=2)
        for bar, v in zip(bars, vals):
            ax.annotate(f"{v:.3f}", (bar.get_x() + bar.get_width() / 2, v),
                        textcoords="offset points", xytext=(0, 4), ha="center",
                        color=INK, fontsize=8)

    ax.set_xticks(xs)
    ax.set_xticklabels([f"{l.replace('_', chr(10))}\nn={s}"
                        for l, s in zip(labels, support)], fontsize=8)
    ax.set_ylim(0, 1.12)
    style(ax, "Per-class performance, measured channels only "
              "(11 features, held-out runs)", "", "Score")
    ax.legend(frameon=False, labelcolor=INK, fontsize=9, ncol=3,
              loc="lower center", bbox_to_anchor=(0.5, -0.2))
    fig.tight_layout()
    fig.savefig(out / "per_class_metrics.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)
    return {l: {"precision": float(p), "recall": float(r), "f1": float(f),
                "support": int(s)}
            for l, p, r, f, s in zip(labels, precision, recall, f1, support)}


def plot_roc(res, labels, out):
    y_bin = label_binarize(res["y_true"], classes=labels)
    proba = res["proba"]

    fig, ax = plt.subplots(figsize=(8.5, 6))
    fig.patch.set_facecolor(SURFACE)
    ax.plot([0, 1], [0, 1], color="#c3c2b7", linewidth=1, linestyle="--")

    areas = {}
    for i, label in enumerate(labels):
        if y_bin[:, i].sum() == 0:
            continue
        fpr, tpr, _ = roc_curve(y_bin[:, i], proba[:, i])
        area = auc(fpr, tpr)
        areas[label] = float(area)
        ax.plot(fpr, tpr, color=CLASS_COLOR[label], linewidth=2,
                label=f"{label.replace('_', ' ')}  AUC {area:.4f}")

    style(ax, "One-vs-rest ROC, measured channels only",
          "False positive rate", "True positive rate")
    ax.legend(frameon=False, labelcolor=INK, fontsize=9, loc="lower right")
    ax.set_xlim(-0.02, 1.02)
    ax.set_ylim(-0.02, 1.02)
    fig.tight_layout()
    fig.savefig(out / "roc_curves.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)
    return areas


def plot_importance(results, out):
    fig, ax = plt.subplots(figsize=(9.5, 6.5))
    fig.patch.set_facecolor(SURFACE)

    full = results["all 16 features"]["importance"]
    lean = results["11 measured channels"]["importance"]
    names = [n for n, _ in sorted(full.items(), key=lambda kv: kv[1])]
    ys = np.arange(len(names))

    ax.barh(ys + 0.19, [full.get(n, 0) for n in names], 0.36,
            color="#e34948", label="with gravity and tilt (leaks)",
            edgecolor=SURFACE, linewidth=1.5)
    ax.barh(ys - 0.19, [lean.get(n, 0) for n in names], 0.36,
            color="#2a78d6", label="measured channels only",
            edgecolor=SURFACE, linewidth=1.5)

    ax.set_yticks(ys)
    ax.set_yticklabels(names, fontsize=9)
    style(ax, "Where each model puts its weight", "Feature importance", "")
    ax.legend(frameon=False, labelcolor=INK, fontsize=9, loc="lower right")
    fig.tight_layout()
    fig.savefig(out / "feature_importance.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def plot_learning_curve(runs, keep, out, seed=42):
    """Accuracy against how many runs per class the model was given."""
    rng = random.Random(seed)
    by_class = {}
    for run in runs:
        by_class.setdefault(run["cond"], []).append(run)

    holdout, pool = [], {}
    for cond, group in by_class.items():
        group = group[:]
        rng.shuffle(group)
        if len(group) == 1:
            windows = tm.windows(group[0])
            cut = int(len(windows) * 0.7)
            holdout += [(w, cond, group[0]["sid"]) for w in windows[cut:]]
            pool[cond] = [[(w, cond, group[0]["sid"]) for w in windows[:cut]]]
            continue
        n_test = max(1, round(len(group) * 0.3))
        for run in group[:n_test]:
            holdout += [(w, cond, run["sid"]) for w in tm.windows(run)]
        pool[cond] = [[(w, cond, run["sid"]) for w in tm.windows(run)]
                      for run in group[n_test:]]

    # Classes hold different numbers of runs, so past the smallest class only the
    # larger ones keep growing. Plot against the TOTAL number of training runs,
    # which is unambiguous, and mark where the smallest class is exhausted.
    max_runs = max(len(v) for v in pool.values())
    smallest = min(len(v) for v in pool.values())
    counts, accuracies = [], []
    for n in range(1, max_runs + 1):
        train, used = [], 0
        for cond, run_windows in pool.items():
            for chunk in run_windows[:n]:
                train.extend(chunk)
                used += 1
        if len(set(r[1] for r in train)) < 2:
            continue
        model, X_te, y_te, _ = fit(train, holdout, keep)
        counts.append(used)
        accuracies.append(float((model.predict(X_te) == y_te).mean()))

    fig, ax = plt.subplots(figsize=(9, 5))
    fig.patch.set_facecolor(SURFACE)
    ax.plot(counts, [a * 100 for a in accuracies], color="#2a78d6", linewidth=2,
            marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=1.5)
    for n, a in zip(counts, accuracies):
        ax.annotate(f"{a*100:.1f}", (n, a * 100), textcoords="offset points",
                    xytext=(0, 9), ha="center", color=INK, fontsize=8)
    exhausted = sum(min(smallest, len(v)) for v in pool.values())
    ax.axvline(exhausted, color="#898781", linewidth=1, linestyle=":")
    ax.annotate("smallest class exhausted;\npast here only the larger\nclasses keep growing",
                (exhausted, min(accuracies) * 100 + 4),
                textcoords="offset points", xytext=(8, 0), color=INK, fontsize=8)
    style(ax, "Held-out accuracy against the size of the training set",
          "Training runs used", "Held-out window accuracy (%)")
    ax.set_ylim(min(accuracies) * 100 - 6, 102)
    fig.tight_layout()
    fig.savefig(out / "learning_curve.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)
    return dict(zip(counts, [round(a, 4) for a in accuracies]))


def main():
    out = REPO / "figures"
    out.mkdir(parents=True, exist_ok=True)

    runs = tm.load_runs()
    print(f"{len(runs)} runs pass the integrity gate")
    train, test = split_runs(runs)
    print(f"windows: train {len(train)}  held out {len(test)}")

    keep_all = list(range(len(tm.FEATURES)))
    keep_lean = [i for i, f in enumerate(tm.FEATURES)
                 if not f.startswith(GRAVITY)]

    results = {}
    for name, keep in (("all 16 features", keep_all),
                       ("11 measured channels", keep_lean)):
        model, X_te, y_te, sids = fit(train, test, keep)
        y_pred = model.predict(X_te)
        correct, total = run_vote(y_te, y_pred, sids)
        results[name] = {
            "y_true": y_te, "y_pred": y_pred,
            "proba": model.predict_proba(X_te),
            "classes": list(model.classes_),
            "window_acc": float((y_pred == y_te).mean()),
            "runs_correct": correct, "runs_total": total,
            "importance": {tm.FEATURES[i]: float(v)
                           for i, v in zip(keep, model.feature_importances_)},
        }
        print(f"  {name:<22} window {results[name]['window_acc']:.4f}   "
              f"runs {correct}/{total}")

    labels = [c for c in tm.CLASSES if c in set(test_y := [r[1] for r in test])]
    lean = results["11 measured channels"]
    lean_labels = [c for c in lean["classes"]]

    plot_confusion(results, labels, out)
    per_class = plot_per_class(lean, labels, out)
    areas = plot_roc({**lean, "proba": lean["proba"]}, lean_labels, out)
    plot_importance(results, out)
    curve = plot_learning_curve(runs, keep_lean, out)

    summary = {
        "window_accuracy": {k: v["window_acc"] for k, v in results.items()},
        "runs_correct": {k: f"{v['runs_correct']}/{v['runs_total']}"
                         for k, v in results.items()},
        "per_class_measured_only": per_class,
        "roc_auc_measured_only": areas,
        "learning_curve_runs_to_accuracy": curve,
    }
    (REPO / "model" / "evaluation.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    print("\nper-class, measured channels only:")
    for label, m in per_class.items():
        print(f"  {label:<20} P {m['precision']:.3f}  R {m['recall']:.3f}  "
              f"F1 {m['f1']:.3f}  n={m['support']}")
    print("\nROC AUC:", {k: round(v, 4) for k, v in areas.items()})
    print("learning curve:", curve)
    print("\nwrote 5 figures and model/evaluation.json")


if __name__ == "__main__":
    main()
