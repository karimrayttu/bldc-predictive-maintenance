"""Sensor calibration: the one place the bench, the Pi and the web view agree."""

from __future__ import annotations

import math
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "config", "app_config.yaml")

# baked defaults
# These MUST equal the corresponding entries in app_config.yaml.
# tools/check_calibration.py fails if they ever diverge.

# Current sense: INA240A2 across a 5 mOhm shunt on PA0.
# Anchored against a series ammeter on 2026-07-30.
MV_PER_AMP = 263.5
IDLE_OFFSET_A = 0.053

# NTC on PA1: 10 k beta-3550 thermistor to GND, 10 k series from 3V3.
NTC_BETA = 3550.0
NTC_R0_OHM = 10000.0
NTC_T0_K = 298.15
NTC_SERIES_OHM = 10000.0
NTC_VIN_V = 3.3

# KX134 gravity vector at rest in the frame the campaign was recorded in (mg).
ORIENTATION_BASELINE_MG = (-12.0, -21.0, 1050.0)

# The demo re-rig moved the accelerometer and changed its scale: 1 g read
# 1577 mg raw instead of 1050. Raw readings are mapped back into the bench
# frame so the demo screen shows campaign-comparable numbers.
DEMO_MOUNT_RAW_BASELINE_MG = (-19.0, -12.0, 1577.0)
DEMO_MOUNT_SCALE = 0.6658            # |bench baseline| / |demo raw baseline|

# Physically plausible range for the motor NTC. Outside this the probe is
# open or shorted, which is a wiring fault and not a temperature.
TEMP_VALID_C = (-20.0, 150.0)


def load_config(path: str = CONFIG_PATH) -> dict:
    """Read app_config.yaml. Returns {} when PyYAML or the file is missing."""
    try:
        import yaml
    except ImportError:
        return {}
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def config_values(cfg: dict | None = None) -> dict:
    """Calibration constants from config, falling back to the baked defaults."""
    cfg = load_config() if cfg is None else cfg
    current = cfg.get("current_sensor") or {}
    temp = cfg.get("temp_sensor") or {}
    return {
        "mv_per_amp": float(current.get("mv_per_amp", MV_PER_AMP)),
        "idle_offset_a": float(current.get("idle_offset_a", IDLE_OFFSET_A)),
        "beta": float(temp.get("beta", NTC_BETA)),
        "r0_ohm": float(temp.get("r0_ohm", NTC_R0_OHM)),
        "t0_k": float(temp.get("t0_k", NTC_T0_K)),
        "series_ohm": float(temp.get("series_ohm", NTC_SERIES_OHM)),
        "vin_v": float(temp.get("vin_v", NTC_VIN_V)),
    }


# current

def amps_from_mv(i_dc_mv, zero_mv, mv_per_amp=MV_PER_AMP,
                 idle_offset_a=IDLE_OFFSET_A):
    """Bus current from the INA240 output, referenced to the learned zero anchor."""
    if i_dc_mv is None or zero_mv is None:
        return None
    return idle_offset_a + (float(i_dc_mv) - float(zero_mv)) / float(mv_per_amp)


# temperature

def temp_c_from_mv(mv, beta=NTC_BETA, r0_ohm=NTC_R0_OHM, t0_k=NTC_T0_K,
                   series_ohm=NTC_SERIES_OHM, vin_v=NTC_VIN_V):
    """Motor temperature from the NTC divider node, via the beta equation."""
    if mv is None:
        return None
    try:
        volts = float(mv) / 1000.0
        if volts <= 0.0 or volts >= vin_v:
            return None
        resistance = series_ohm * volts / (vin_v - volts)
        kelvin = 1.0 / (1.0 / t0_k + math.log(resistance / r0_ohm) / beta)
        celsius = kelvin - 273.15
    except (ValueError, ZeroDivisionError):
        return None
    low, high = TEMP_VALID_C
    return celsius if low <= celsius <= high else None


# orientation

def tilt_deg(gx, gy, gz, baseline_mg=ORIENTATION_BASELINE_MG):
    """Angle between the measured gravity vector and the healthy baseline."""
    if gx is None or gy is None or gz is None:
        return None
    magnitude = math.sqrt(gx * gx + gy * gy + gz * gz)
    if not magnitude:
        return None
    base_magnitude = math.sqrt(sum(v * v for v in baseline_mg))
    cosine = (gx * baseline_mg[0] + gy * baseline_mg[1] + gz * baseline_mg[2]) \
        / (magnitude * base_magnitude)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def to_bench_frame(gx, gy, gz, vib_rms, vib_pk):
    """Map raw demo-mount accelerometer readings into the bench frame."""
    scale = DEMO_MOUNT_SCALE
    base, raw = ORIENTATION_BASELINE_MG, DEMO_MOUNT_RAW_BASELINE_MG
    return (
        int(round(base[0] + (gx - raw[0]) * scale)),
        int(round(base[1] + (gy - raw[1]) * scale)),
        int(round(base[2] + (gz - raw[2]) * scale)),
        int(round(vib_rms * scale)),
        int(round(vib_pk * scale)),
    )
