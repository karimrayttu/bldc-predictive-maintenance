# Architecture

```
   NUCLEO-F401RE firmware ──CSV @115200──▶ ST-LINK VCP (auto-detected COM)
                                              │
                              ┌───────────────▼─────────────────┐
                              │  sources.py  (background thread) │  SerialSource (the only source; there is no simulator)
                              │  raw lines ──▶ thread-safe queue │
                              └───────────────┬─────────────────┘
                                              │ GUI timer drains @20 Hz
                              ┌───────────────▼─────────────────┐
                              │  validator.py                    │  shape + range reject; time gaps counted
                              │  rejects corrupt frames          │  (FrameStats: live data-health)
                              └───────────────┬─────────────────┘
                          valid frame ────────┼───────────────────────────┐
                                              │                           │
                    ┌─────────────────────────▼──────┐      ┌─────────────▼──────────────┐
                    │ ui/main_window.py              │      │ session.py                 │
                    │  live plots, severity banner   │      │  writes data.csv + meta.json│
                    │  drift.py  S0..S3 scoring      │      │  (no RUN_LOG is written)    │
                    └────────────────────────────────┘      └────────────────────────────┘
                                              │
                              ┌───────────────▼─────────────────┐
                              │ baseline.py / apps/bench/tools/build_baseline.py │  per-state mu/sigma → baselines/
                              └─────────────────────────────────┘
```

## Module responsibilities
| Module | Job |
|---|---|
| `bldc_phm/schema.py` | The one canonical wide column set (stream + reserved future channels) and datasheet range limits. |
| `bldc_phm/sources.py` | The live serial source, feeding a queue from a background thread. There is no simulator. |
| `bldc_phm/validator.py` | Gatekeeper: a frame is rejected on shape (exactly `len(STREAM_COLUMNS)` = 44 integer fields) or physical range. A `t_ms` gap is **counted, not rejected**; a late frame is still a real measurement, so `FrameStats.time_gaps` can be non-zero at 100 % integrity. Keeps corruption out of the dataset. |
| `bldc_phm/session.py` | Run lifecycle: folder + `data.csv` (full schema) + `meta.json` (controlled variables). |
| `bldc_phm/baseline.py` | State-conditioned healthy μ/σ from one or more healthy sessions. |
| `bldc_phm/drift.py` | EWMA + CUSUM + 3σ-with-persistence → S0-S3 severity with hysteresis. |
| `bldc_phm/modbus_source.py` | Ready-but-inert BLD-510B RS-485 telemetry poller (activates when a MAX485 is wired). |
| `apps/bench/ui/main_window.py` | The operator screen: connect, plot, label, record, arm baseline, score. |

## Design choices worth knowing
- **Queue + timer, not Qt cross-thread signals**: the simplest real-time pattern that holds here; the
  serial reader never blocks and the GUI never deadlocks. Drops frames under overload
  rather than stalling (and the FrameStats time-gap counter records any drop).
- **Wide stable schema, blanks for future channels**: no migration when sensors arrive.
- **Validation before persistence**: a corrupt row never reaches `data.csv`.
- **Severity decoupled from hardware safety**: S0-S3 is advisory/predictive. The hard
  safety limit lives in the BLD-510B's own over-current/-voltage/-temp protection, not in
  this software (see `WIRING_FUTURE.md`).
