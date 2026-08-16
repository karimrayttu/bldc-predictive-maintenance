# Experiment Design: how to build a valid PHM dataset

The golden rule: **vary ONE controlled variable at a time, label it explicitly, and keep
every other bench condition fixed.** Everything below serves that rule. The app's
taxonomy (`bldc_phm/config/taxonomy.yaml`) and labels are built to enforce it.

## Three independent axes (don't mix them in one run)
| Axis | Values (editable in taxonomy.yaml) | Role |
|---|---|---|
| **Operating state** | speed step × load (no_load/light/medium/heavy) | What you *condition the baseline on* |
| **Installation** | orientation × mount stiffness | What you *characterize separately* |
| **Health** | condition × severity | What you *detect* |

> Note: "load" is an operating-condition axis, NOT an installation axis. A low, repeatable
> friction is *healthy light load*; an abnormal one is the *drag* fault, same physics,
> different label, distinguished by being known/repeatable vs abnormal.

## Stage 1: Controlled healthy baseline (the anchor)
ONE fixed configuration: rigid horizontal mount, marked bolt positions + torque, fixed
cable routing, fixed accelerometer position (when fitted), fixed warm-up (15-20 min).
Capture no-load first, then known load levels once you have a load mechanism.
This teaches the system what changes are caused by the **motor**, not the test stand.

## Stage 2: Installation sensitivity (separate labels!)
Repeat the healthy tests while changing ONE installation variable:
`horizontal`, `vertical_shaft_up`, `vertical_shaft_down`, `rigid`/`slightly_loose`/
`compliant`. Keep these labels **separate from fault labels**. This lets you prove the
classifier is detecting a real fault, not just a mounting change.
- The motor must stay physically restrained in every case.
- `unsecured_DEMO` (unmounted) is a **low-speed, separate demo only**, labeled
  `invalid_mounting`; never healthy training data. Unmounted = motor bouncing on the
  bench, not motor health, and unsafe at speed.

## Stage 3: Fault validation (one fault at a time)
Introduce faults individually, graded mild→clear, at the SAME fixed installation:
imbalance, drag, misalignment, looseness, phase-resistance increase, thermal obstruction.
Do **not** change orientation, mount, load, and fault simultaneously.

## Baseline test matrix (each cell = its own healthy baseline)

**Speed ceiling first.** The BLD-510B's SV input is referenced to 5 V while the NUCLEO
drives 3.3 V PWM, so duty scales x0.66 and firmware full scale is **2310 rpm**, not the
motor's rated 4000 (`bldc_phm/firmwaregen.py`, `DRIVER_FULLSCALE_RPM`). Anything commanded
above 2310 is unreachable, so the matrix stops at 2100 with headroom.

| Speed | no_load | light | medium |
|---|---|---|---|
| 500 rpm | yes | yes | optional |
| 1100 rpm | yes | yes | yes |
| 1700 rpm | yes | yes | yes |
| 2100 rpm | yes | optional | **only if safe: short, watch current/temp** |

**Datasheet limit:** motor is **1.79 A continuous, 6.0 A peak**. High speed + heavy load
drives current toward peak and overheats; keep those runs short and stop on the driver's
over-current flag.

The mounted campaign actually filled a denser healthy row than this: 300, 500, 700, 900,
1100, 1300, 1500, 1700, 1900 and 2100 rpm, against 3 to 5 speeds for each fault class.
The load columns are still empty; see "Practical gap to close" below.

The **Plan & Coverage** tab scans every run's `meta.json` and shows this matrix live as PASS/total
per cell, for whatever two axes you pick, so you always know what's captured and what's left.

## Practical gap to close
You have no controlled-load mechanism yet (brake/dyno/generator). Until you do, the load
column stays empty; capture the full **no-load** matrix across speeds and the installation
variations first. Add the load axis the moment a repeatable load is on the bench.

## Hygiene
- Only `validated == PASS` runs enter a training set.
- Keep whole runs intact across train/test (never split one run).
- Re-baseline after any re-mount/maintenance; capture fresh healthy the same day as faults.

## Every variable the app captures (uniformly, on every run)
All of these are declared in `bldc_phm/config/taxonomy.yaml` (`fields:`) and written to
`meta.json` for **every** run, so any variable combination is comparable.
Pick a **Test type** template to pre-fill a coherent set, then vary the one variable you're
studying.

| Group | Variables |
|---|---|
| Identity | motor_id, operator, stage (1_baseline / 2_installation / 3_fault / 4_demo) |
| Health | condition, severity, fault_mechanism |
| Mechanical | orientation, mount stiffness, baseplate, bolt_torque_Nm, coupling, alignment, shaft_guard, accel_location, accel_axis |
| Operating | speed_step→rpm, direction, ramp, load_mechanism, load level |
| Electrical | supply_V, supply_ilimit_A, driver_ilimit_A, sv_command_V (measured) |
| Environment | thermal_state (cold_start/warming/warm_steady), warmup_min, motor_start_temp_C, case_temp_C, ambient_temp_C, humidity_pct |
| Instrumentation | firmware_version, sensor_cal_version, dmm_ref, sample_profile, channels_enabled |
| Notes | notes, bench_activity/anomalies |

### Customizing
- **Add a value** to any dropdown: just type it in the app (it saves back to the YAML), or
  edit the list in `taxonomy.yaml`.
- **Add a whole new variable**: add one row under `fields:` (key, label, group, type,
  options/default). The form and `meta.json` both pick it up automatically,
  no code change.
- **Add a test type**: add an entry under `test_templates:` mapping fields to preset values.
- The **completeness guard** blocks recording until condition/orientation/mount/load are set,
  so no run lands in the dataset without its core labels.
