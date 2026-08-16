# BLDC PHM: Data Workspace

All acquired data, split by the **fixture MAIN SWITCH** so commissioning (unmounted) data
can NEVER contaminate the real baseline:

```
data/
├── commissioning_unmounted/   <- MAIN SWITCH = "Commissioning (unmounted)"
│   │                             valid_for_classifier = false. Use for firmware/sensor
│   │                             checkout ONLY; never for training.
│   ├── sessions/
│   └── combined_data.csv        partial mid-campaign export; see below
│
└── mounted_baseline/          <- MAIN SWITCH = "Mounted (baseline)"
    │                             the real predictive-maintenance data.
    ├── sessions/
    └── orientation_baseline.json
```

There is **no RUN_LOG.csv**, and no `raw/`, `baselines/` or `exports/` directory. An
earlier design wrote a master run manifest and those extra trees; they went stale against
the files they indexed, so the per-run `data.csv` + `meta.json` pair is now the only source
of truth. Every consumer scans `meta.json` directly.

- **sessions/**: `<condition>/<speed>/S###_<stamp>/` with `data.csv` + `meta.json`.
- **meta.json** is authoritative for `fixture_state`, `valid_for_classifier`,
  `dataset_use`, `authenticity`, `frame_stats` and every test variable.
- Each `data.csv` row also carries `fixture_state` and `valid_for_classifier`, so you can
  tell unmounted data at a glance from the raw file, but those columns are stamped as the
  run is recorded, **before** the stop-time and QA-sweep verdicts are known. All three runs
  excluded from the mounted campaign still carry `true` in their own `data.csv`. When the
  two disagree, `meta.json` wins.
- Healthy μ/σ baselines are built on demand by
  `apps/bench/tools/build_baseline.py --mode <mode>` (defaults to `mounted_baseline`; it
  refuses to draw baselines from commissioning data unless told). Nothing is committed here.

## The rule that makes this data valid
Between a healthy run and a fault run, **only the fault may change**, same fixture,
orientation, warm-up, speed, supply, room. Each run's `meta.json` captures those
controlled variables so you (and a classifier) can trust that a difference in the data is
a real fault, not a setup change. See `docs/` in the workspace root.

## Data hygiene for the ML stage
- Keep whole runs intact; never split one run's rows across train and test.
- Re-baseline after any re-mount or maintenance; capture a fresh healthy set the same day
  as each fault set.
- Only `validated == PASS` runs should enter a training set (REVIEW = inspect first).

---

## `mounted_baseline/`: the campaign the model is trained on

60 runs recorded 30 July 2026 across five conditions. All 60 logged 100.0 % frame
integrity. Three are nonetheless tagged `valid_for_classifier = false` and are excluded
from every downstream tool; frame integrity is not what excluded them:

| Run | Why | How it was caught |
|---|---|---|
| `healthy/1100rpm/S014` | `t_ms` rolled over mid-run, so elapsed time reads -4174 s. The authenticity check cannot confirm rpm against Hall edges over a negative interval and returns SUSPECT. | automatic (anything short of VERIFIED is excluded) |
| `healthy/1500rpm/S021` | Mean current sits 7.1 mA below its two sibling 1500 rpm runs: the zero anchored on a boundary Hall park. | by hand, in a QA sweep the same day |
| `loose_mount/2100rpm/S039` | Labelled 2100 rpm but the motor never turned; a startup park-blip raced the batch's speed command. The run recorded a stationary bench. | by hand; a label-vs-measurement gate in the bench app now catches it automatically |

The full `invalidated_reason` text is in each run's `meta.json`. All three are kept on disk
deliberately; `tools/plot_dataset.py --include-invalid` will plot them.

---

## `commissioning_unmounted/`: the pre-mount commissioning campaign

This is the earlier run, recorded 20-21 June 2026, **before the motor was bolted to the
instrumented baseplate**. It is included because it is real data and because it contains two
fault classes the shipped mounted campaign does not: **`imbalance`** and
**`phase_asymmetry`**.

33 sessions, plus `combined_data.csv`:

| Condition | Sessions | Speed setpoints (rpm) |
|---|---|---|
| `healthy` | 19 | 500, 800, 1100, 1400, 1600, 1700, 1800, 2000, 2200, 2310 |
| `imbalance` | 6 | 500, 800, 1100, 1700, 2000, 2310 |
| `phase_asymmetry` | 5 | 500, 1100, 1700, 2000, 2310 |
| `looseness` | 2 | 1400, 2310 |
| `drag` | 1 | 1400 |

`combined_data.csv` is a partial export taken mid-campaign: 12,849 rows covering sessions
S001-S025 only, so it holds `healthy` and `imbalance` but none of the sessions recorded after
it was written. The per-session `data.csv` files under `sessions/` are the complete record.

### Do not pool this with `mounted_baseline/`

The fixture state is different, and that difference is not a nuisance parameter; for a
vibration-based classifier it is most of the signal:

- The motor ran **free, unmounted**, on a free shaft with no coupling and no load
  (`coupling: none_free_shaft`, `load: no_load`). Its mechanical impedance, resonances and
  rigid-body modes bear no relation to the bolted-down configuration.
- There was **no accelerometer on the motor yet**; all 33 sessions record
  `accel_location: none_yet`, and across all 27,367 rows the `accel_rms_g`, `accel_pk_g`,
  `temp_motor_c`, `temp_ambient_c` and every current column are empty. Only the Hall-derived
  channels carry data, and `period_std_permil` / `mech1x_permil` / `elec_permil` are present
  in 21,342 of those rows.
- Firmware was a different generation (`motor.elf-2026-06-19`) with different feature maths.
  See `firmware/history/` for what changed.
- Frame capture was less reliable. Every mounted session logged 100 % good frames; here the
  median is 100 % but the worst session kept only 21.9 %. Seven sessions are flagged
  `validated: REVIEW` (26 PASS) and two carry `authenticity: SUSPECT`.

Every row is tagged `fixture_state = commissioning_unmounted` and
`valid_for_classifier = false`, and the baseline builder refuses to draw baselines from this
mode unless explicitly told to. **Training or evaluating on a mix of the two campaigns will
produce a model that has learned the fixture, not the fault.**

Use this data for what it is: firmware and sensor-chain checkout, a record of the two extra
fault classes as they were induced, and a comparison point for how much the mounting changed
the Hall-side signatures. Any conclusion drawn from it needs its own healthy runs from the
same campaign as the reference.
