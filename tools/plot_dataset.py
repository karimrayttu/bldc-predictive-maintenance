"""Build the dataset figures used in the README from the recorded bench sessions.

    python tools/plot_dataset.py
    python tools/plot_dataset.py --include-invalid
    python tools/plot_dataset.py --data-root data/mounted_baseline --out figures
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

REPO = Path(__file__).resolve().parents[1]

# Fixed slot order. Do not cycle or reorder: the ordering is what keeps the
# series separable for colour-blind readers.
CONDITION_COLOR = {
    "healthy": "#2a78d6",
    "loose_mount": "#eb6834",
    "orientation_change": "#1baf7a",
    "rotor_drag": "#eda100",
    "overheat": "#e87ba4",
}
CONDITION_ORDER = list(CONDITION_COLOR)

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#2a78d6", "#1c5cab", "#104281"]

# Raw telemetry columns that carry the fault signatures.
FEATURES = {
    "accel_rms_mg": "Vibration RMS (mg)",
    "i_dc_a": "DC bus current (A)",
    "ripple_permil": "Speed ripple (per mille)",
    "temp_motor_c": "Motor temperature (C)",
}


def style_axes(ax, title, xlabel, ylabel):
    ax.set_title(title, color=INK, fontsize=12, pad=12, loc="left")
    ax.set_xlabel(xlabel, color=INK_MUTED, fontsize=9)
    ax.set_ylabel(ylabel, color=INK_MUTED, fontsize=9)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.8, alpha=0.9)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color("#c3c2b7")


def new_figure(size=(9, 5.2)):
    fig, ax = plt.subplots(figsize=size)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    return fig, ax


def save(fig, out_dir: Path, name: str):
    path = out_dir / name
    fig.tight_layout()
    fig.savefig(path, dpi=160, facecolor=SURFACE)
    plt.close(fig)
    print(f"  wrote {path.relative_to(REPO)}")


def load_sessions(data_root: Path, include_invalid: bool = False
                  ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (per-sample frame, per-run summary frame)."""
    frames = []
    dropped = []
    for csv in sorted(data_root.glob("sessions/*/*/*/data.csv")):
        df = pd.read_csv(csv, low_memory=False)
        if df.empty:
            continue
        run = csv.parent.name.split("_")[0]
        # meta.json is authoritative, NOT the per-row valid_for_classifier column:
        # that column is stamped on every row as the run is recorded, while the
        # verdict is written at stop time (or by a later QA sweep), so all three
        # excluded runs still carry "true" in their own data.csv.
        meta_path = csv.parent / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8")) \
            if meta_path.exists() else {}
        valid = str(meta.get("valid_for_classifier", "true")).strip().lower() == "true"
        if not valid and not include_invalid:
            dropped.append(f"{csv.parent.parent.parent.name}/"
                           f"{csv.parent.parent.name}/{run}")
            continue
        df["run"] = run
        frames.append(df)

    if dropped:
        print(f"  excluded {len(dropped)} run(s) tagged valid_for_classifier=false: "
              + ", ".join(dropped))
        print("  (pass --include-invalid to plot the full recorded set instead)")

    if not frames:
        raise SystemExit(f"no sessions found under {data_root}")

    samples = pd.concat(frames, ignore_index=True)
    samples["condition"] = samples["condition"].astype(str)

    agg = {col: "mean" for col in FEATURES}
    agg["accel_pk_mg"] = "max"
    agg["rpm"] = "mean"
    runs = (
        samples.groupby(["condition", "speed_setpoint_rpm", "run"], as_index=False)
        .agg(agg)
    )
    return samples, runs


def plot_feature_vs_speed(runs: pd.DataFrame, out_dir: Path, column: str, name: str):
    """Mean of one channel against commanded speed, one line per condition."""
    fig, ax = new_figure()
    label = FEATURES[column]
    ends = []

    for condition in CONDITION_ORDER:
        sub = runs[runs["condition"] == condition]
        if sub.empty:
            continue
        curve = sub.groupby("speed_setpoint_rpm")[column].mean().sort_index()
        ax.plot(curve.index, curve.values, color=CONDITION_COLOR[condition],
                linewidth=2, marker="o", markersize=8, markeredgecolor=SURFACE,
                markeredgewidth=1.5, label=condition, zorder=3)
        ends.append([curve.index[-1], curve.values[-1], condition])

    style_axes(ax, label + " vs commanded speed", "Commanded speed (rpm)", label)
    ax.legend(frameon=False, labelcolor=INK, fontsize=9, loc="upper left")
    ax.margins(x=0.22)

    # Label only the lines that end clear of their neighbours. Where the curves
    # bunch up, a floating label points at the wrong line, so the legend and the
    # summary table carry those instead.
    lo, hi = ax.get_ylim()
    gap = (hi - lo) * 0.045
    ends.sort(key=lambda e: e[1])
    for i, (x_end, y_end, condition) in enumerate(ends):
        below = y_end - ends[i - 1][1] if i else float("inf")
        above = ends[i + 1][1] - y_end if i + 1 < len(ends) else float("inf")
        if min(below, above) >= gap:
            ax.annotate(condition, (x_end, y_end), textcoords="offset points",
                        xytext=(10, 0), color=INK, fontsize=9, va="center")
    save(fig, out_dir, name)


def plot_condition_spread(runs: pd.DataFrame, out_dir: Path):
    """Per-run spread of each channel, grouped by condition."""
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.5))
    fig.patch.set_facecolor(SURFACE)

    for ax, (column, label) in zip(axes.ravel(), FEATURES.items()):
        ax.set_facecolor(SURFACE)
        present = [c for c in CONDITION_ORDER if (runs["condition"] == c).any()]
        data = [runs.loc[runs["condition"] == c, column].dropna() for c in present]
        bp = ax.boxplot(data, patch_artist=True, widths=0.55,
                        medianprops=dict(color=INK, linewidth=1.6),
                        whiskerprops=dict(color="#c3c2b7"),
                        capprops=dict(color="#c3c2b7"),
                        flierprops=dict(marker="o", markersize=4,
                                        markerfacecolor=INK_MUTED,
                                        markeredgecolor="none", alpha=0.6))
        for patch, condition in zip(bp["boxes"], present):
            patch.set_facecolor(CONDITION_COLOR[condition])
            patch.set_edgecolor(SURFACE)   # 2px surface gap between fills
            patch.set_linewidth(2)
        ax.set_xticks(range(1, len(present) + 1))
        ax.set_xticklabels([c.replace("_", "\n") for c in present], fontsize=8)
        style_axes(ax, label, "", label)

    fig.suptitle("Per-run channel spread by condition", color=INK,
                 fontsize=13, x=0.01, ha="left")
    save(fig, out_dir, "condition_spread.png")


def plot_overheat_trace(samples: pd.DataFrame, out_dir: Path):
    """Temperature against elapsed time: the one channel that names the overheat class."""
    fig, ax = new_figure()
    fastest = samples["speed_setpoint_rpm"].max()

    for condition, color in (("healthy", "#2a78d6"), ("overheat", "#e87ba4")):
        sub = samples[(samples["condition"] == condition)
                      & (samples["speed_setpoint_rpm"] == fastest)]
        if sub.empty:
            continue
        run = sub[sub["run"] == sub["run"].iloc[0]].copy()
        elapsed = (run["t_ms"] - run["t_ms"].iloc[0]) / 1000.0
        ax.plot(elapsed, run["temp_motor_c"], color=color, linewidth=2,
                label=f"{condition} @ {fastest:g} rpm")
        ax.annotate(condition, (elapsed.iloc[-1], run["temp_motor_c"].iloc[-1]),
                    textcoords="offset points", xytext=(8, 0),
                    color=INK, fontsize=9, va="center")

    style_axes(ax, "Motor temperature during a run",
               "Elapsed time (s)", "Motor temperature (C)")
    ax.legend(frameon=False, labelcolor=INK, fontsize=9, loc="upper left")
    ax.margins(x=0.16)
    save(fig, out_dir, "overheat_temperature.png")


def plot_run_inventory(runs: pd.DataFrame, out_dir: Path):
    """How many runs exist per condition and speed. Magnitude, so a one-hue ramp."""
    table = (runs.pivot_table(index="condition", columns="speed_setpoint_rpm",
                              values="run", aggfunc="count")
             .reindex([c for c in CONDITION_ORDER if c in set(runs["condition"])])
             .fillna(0).astype(int))

    fig, ax = new_figure((9, 3.6))
    cmap = matplotlib.colors.LinearSegmentedColormap.from_list("blues", BLUE_RAMP)
    ax.imshow(table.values, cmap=cmap, aspect="auto", vmin=0,
              vmax=max(1, table.values.max()))

    ax.set_xticks(range(len(table.columns)))
    ax.set_xticklabels([f"{int(c)}" for c in table.columns], fontsize=9)
    ax.set_yticks(range(len(table.index)))
    ax.set_yticklabels(table.index, fontsize=9)

    for row in range(table.shape[0]):
        for col in range(table.shape[1]):
            count = table.values[row, col]
            if count:
                shade = INK if count <= table.values.max() / 2 else SURFACE
                ax.text(col, row, str(count), ha="center", va="center",
                        color=shade, fontsize=9)

    ax.set_title("Recorded runs per condition and speed", color=INK,
                 fontsize=12, pad=12, loc="left")
    ax.set_xlabel("Commanded speed (rpm)", color=INK_MUTED, fontsize=9)
    ax.tick_params(colors=INK_MUTED, length=0)
    ax.grid(False)
    for side in ax.spines.values():
        side.set_visible(False)
    save(fig, out_dir, "run_inventory.png")


def write_summary_table(runs: pd.DataFrame, out_dir: Path):
    """Table view: the relief for the low-contrast series colours."""
    summary = (runs.groupby("condition")
               .agg(runs=("run", "nunique"),
                    speeds=("speed_setpoint_rpm", "nunique"),
                    vib_rms_mg=("accel_rms_mg", "mean"),
                    vib_pk_mg=("accel_pk_mg", "max"),
                    current_a=("i_dc_a", "mean"),
                    ripple_permil=("ripple_permil", "mean"),
                    temp_c=("temp_motor_c", "mean"))
               .reindex([c for c in CONDITION_ORDER if c in set(runs["condition"])])
               .round(3))

    summary.to_csv(out_dir / "dataset_summary.csv")
    # Per-run table, one row per recorded run. analysis/matlab/channel_separability.m
    # reads this to score each channel as a single-feature detector.
    runs.to_csv(out_dir / "runs.csv", index=False)
    print(f"  wrote {(out_dir / 'runs.csv').relative_to(REPO)} "
          f"({len(runs)} runs x {runs.shape[1]} columns)")

    lines = ["| condition | runs | speeds | vib RMS (mg) | vib pk (mg) | current (A) | ripple (‰) | temp (C) |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for condition, row in summary.iterrows():
        lines.append(
            f"| {condition} | {int(row['runs'])} | {int(row['speeds'])} | "
            f"{row['vib_rms_mg']:.1f} | {row['vib_pk_mg']:.0f} | {row['current_a']:.3f} | "
            f"{row['ripple_permil']:.0f} | {row['temp_c']:.1f} |")
    (out_dir / "dataset_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote {(out_dir / 'dataset_summary.md').relative_to(REPO)}")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", default="data/mounted_baseline")
    parser.add_argument("--out", default="figures")
    parser.add_argument("--include-invalid", action="store_true",
                        help="also plot runs tagged valid_for_classifier=false "
                             "(the default set matches what the classifier sees)")
    args = parser.parse_args()

    data_root = (REPO / args.data_root).resolve()
    out_dir = (REPO / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"reading sessions from {data_root}")
    samples, runs = load_sessions(data_root, include_invalid=args.include_invalid)
    print(f"  {len(samples):,} samples across {runs['run'].nunique()} runs, "
          f"{runs['condition'].nunique()} conditions")

    plot_feature_vs_speed(runs, out_dir, "accel_rms_mg", "vibration_vs_speed.png")
    plot_feature_vs_speed(runs, out_dir, "i_dc_a", "current_vs_speed.png")
    plot_feature_vs_speed(runs, out_dir, "ripple_permil", "ripple_vs_speed.png")
    plot_condition_spread(runs, out_dir)
    plot_overheat_trace(samples, out_dir)
    plot_run_inventory(runs, out_dir)

    summary = write_summary_table(runs, out_dir)
    print()
    print(summary.to_string())


if __name__ == "__main__":
    main()
