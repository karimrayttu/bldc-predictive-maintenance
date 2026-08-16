"""Coverage: turns the per-run meta.json files into a test-matrix grid so you can see,
at a glance,
which speed x load (or any two axes) cells are captured and how many PASSed.
"""

import os
import glob
import json

AXIS_CHOICES = ["speed_rpm_nominal", "load", "orientation", "condition", "severity"]

# Default data-collection PLAN (override in taxonomy.yaml > coverage_plan). The requirements
# tracker uses this to show, per category, how many runs are still needed.
# Speeds are the reachable ones only. The BLD-510B sees 3.3 V PWM on a 5 V-referenced SV
# input, which scales duty x0.66 -> firmware fullscale is 2310 rpm. A 3500 rpm cell (in the
# old plan) could NEVER be satisfied, so the plan could never reach 100% and "run next test"
# would keep handing back an impossible run. Anything above 2310 is excluded for the same
# reason: the command saturates at fullscale, so a row labelled 2400 would carry a speed
# the motor never actually reached. The plan stops at 2100 to keep headroom below the rail.
DEFAULT_PLAN = {
    "speeds": [300, 500, 700, 900, 1100, 1300, 1500, 1700, 1900, 2100],
    "runs_per_cell": 3,
    "conditions": ["healthy", "loose_mount", "rotor_drag", "overheat", "orientation_change"],
}

# Wall-clock per official run: settle + steady record + coast-down + save.
SETTLE_S = 20
# 2026-07-30 (operator): 2-minute steady record per run, ~1200 frames at 10 Hz,
# 6x more than steady-state feature distributions need, and it cuts the 150-run
# campaign to ~7 h of bench time. MUST equal FULL_RUN_HOLD_S in main_window.
HOLD_S = 120
COAST_S = 25


def plan_workload(plan, remaining_runs=None):
    """Bench time this plan implies. The scheduler is useless if it quietly asks for more
    hours than exist, so the Coverage page shows this instead of only a percentage."""
    plan = plan or DEFAULT_PLAN
    speeds = plan.get("speeds", DEFAULT_PLAN["speeds"])
    conds = plan.get("conditions", DEFAULT_PLAN["conditions"])
    overrides = plan.get("condition_overrides") or {}
    total = 0
    seconds_total = 0
    for c in conds:
        ov_c = overrides.get(c) or {}
        raw = ov_c.get("speeds", speeds)
        if isinstance(raw, dict):
            n = sum(int(v) for v in raw.values())
        else:
            n = len(raw) * int(ov_c.get("runs_per_cell", plan.get("runs_per_cell", 5)))
        # per-condition hold override (e.g. rotor_drag 80 s so 5 runs fit 10 min)
        hold = int(ov_c.get("hold_s", HOLD_S))
        total += n
        seconds_total += n * (SETTLE_S + hold + COAST_S)
    per_run_s = (seconds_total // total) if total else (SETTLE_S + HOLD_S + COAST_S)
    if remaining_runs is None:
        runs, seconds = total, seconds_total
    else:
        runs = int(remaining_runs)
        seconds = runs * per_run_s
    return {
        "runs": runs,
        "total_runs": total,
        "per_run_s": per_run_s,
        "seconds": seconds,
        "hours": seconds / 3600.0,
    }


def count_official(data_dir, subroot):
    """Count OFFICIAL full-cycle runs per (condition, speed) by scanning session meta.json
    (no RUN_LOG needed). Returns {(condition, int_speed): count}. Excludes R&D + coast-downs."""
    counts = {}
    base = os.path.join(data_dir, subroot, "sessions")
    for m in glob.glob(os.path.join(base, "**", "meta.json"), recursive=True):
        try:
            meta = json.load(open(m))
        except Exception:
            continue
        if meta.get("run_kind") == "rd" or meta.get("test_kind") == "coastdown":
            continue
        # A run flagged not-classifier-valid (e.g. authenticity SUSPECT, frame gaps)
        # is kept on disk but must NOT satisfy its cell, otherwise the scheduler
        # believes the cell is complete while the training set is one run short.
        # Found live 2026-07-30: S014 (healthy@1100, SUSPECT) hid a missing run.
        if str(meta.get("valid_for_classifier")).lower() == "false":
            continue
        # a run without closed_utc is a crash fragment (or still recording): it must
        # not satisfy its cell; the scheduler would skip a run the dataset lacks.
        if not meta.get("closed_utc"):
            continue
        cond = meta.get("condition")
        try:
            sp = int(float(meta.get("speed_rpm_nominal")))
        except (TypeError, ValueError):
            continue
        if cond:
            counts[(cond, sp)] = counts.get((cond, sp), 0) + 1
    return counts


def build_requirements(counts, plan):
    """Per (condition, speed) target vs collected. `counts` = {(cond, int_speed): n}."""
    counts = counts or {}
    speeds = [int(s) for s in plan.get("speeds", DEFAULT_PLAN["speeds"])]
    target = int(plan.get("runs_per_cell", DEFAULT_PLAN["runs_per_cell"]))
    conds = plan.get("conditions", DEFAULT_PLAN["conditions"])
    # Per-condition overrides (2026-07-30, operator): faults whose signature does
    # not depend strongly on speed get a REDUCED plan, fewer speeds and fewer
    # runs, instead of the full grid. e.g. overheat's temperature signature and
    # orientation_change's gravity vector barely change with rpm, so 3 speeds x 2
    # runs characterizes them; rotor_drag's load signature IS speed-dependent, so
    # it keeps the full grid. Shape: {condition: {speeds:[...], runs_per_cell: n}}
    overrides = plan.get("condition_overrides") or {}

    rows, todo = [], []
    have_total = target_total = cells_done = cells_total = 0
    for ci, cond in enumerate(conds):
        ov_c = overrides.get(cond) or {}
        raw_speeds = ov_c.get("speeds", speeds)
        c_target = int(ov_c.get("runs_per_cell", target))
        # speeds may be a list (uniform target) or {speed: target} for per-cell
        # targets; e.g. loose_mount 2100 gets 3 runs while 500/1300 get 2.
        if isinstance(raw_speeds, dict):
            cell_targets = {int(k): int(v) for k, v in raw_speeds.items()}
        else:
            cell_targets = {int(x): c_target for x in raw_speeds}
        cells, remaining_c = [], 0
        for sp in sorted(cell_targets):
            c_target = cell_targets[sp]
            have = int(counts.get((cond, sp), 0))
            rem = max(0, c_target - have)
            cells.append({"speed": sp, "have": have, "target": c_target, "remaining": rem})
            have_total += min(have, c_target)
            target_total += c_target
            cells_total += 1
            if have >= c_target:
                cells_done += 1
            remaining_c += rem
            if rem > 0:
                todo.append((cond, sp, rem, ci))
        rows.append({"condition": cond, "cells": cells, "remaining": remaining_c})

    remaining_total = target_total - have_total
    overall = {
        "have": have_total, "target": target_total,
        "pct": (100 * have_total // target_total) if target_total else 0,
        "cells_done": cells_done, "cells_total": cells_total,
        "remaining": remaining_total,
        "total_recorded": sum(counts.values()),
        "workload": plan_workload(plan, remaining_total),
        "plan_workload": plan_workload(plan),
    }

    # Order the queue by PLAN ORDER: every speed of one condition, then the next
    # condition. Previously this sorted by most-remaining, which interleaved conditions
    # and meant physically re-rigging the fault between nearly every run. Sorting by the
    # condition's position in the plan also keeps orientation_change last, which is where
    # it belongs; it is the only fault that requires moving the motor itself.
    todo.sort(key=lambda x: (x[3], x[1]))
    todo = [(c, sp, rem) for c, sp, rem, _ci in todo]
    return rows, overall, todo
