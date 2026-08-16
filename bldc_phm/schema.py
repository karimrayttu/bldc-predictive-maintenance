"""Canonical data schema for the BLDC PHM acquisition system."""

# What the NUCLEO-F401RE firmware streams over the ST-LINK VCP today
STREAM_COLUMNS = [
    "t_ms",            # ms since MCU boot
    "rpm",             # mechanical RPM from Hall edges (24 edges/rev)
    "ripple_permil",   # speed ripple within one rev, per-mille of average
    "min_us",          # shortest Hall-edge interval in the rev (us)
    "max_us",          # longest Hall-edge interval in the rev (us)
    "hall",            # current Hall state (1..6 valid)
    "illegal",         # cumulative count of illegal Hall states (0 or 7)
    "edges",           # cumulative total Hall edges
    "step",            # speed step index 0..5 (9 = serial-commanded)
    "coast_ms",        # last coast-down time (ms)
    # per-revolution Hall features (firmware integer DFT over the 24 edges/rev)
    "period_std_permil",  # std of the 24 edge intervals / mean  (overall timing jitter)
    "mech1x_permil",      # 1x-MECHANICAL amplitude (24-edge period) -> imbalance/eccentricity
    "elec_permil",        # ELECTRICAL amplitude (6-edge period) -> Hall-spacing/phase asymmetry
    "i_dc_mv",            # legacy median/EMA PA0 voltage, factory-VREF converted (mV)
    "adc_vref_raw",       # current frame's rounded 8-sample VREFINT mean
    "adc_pc0_mv",          # old PC0/A5 current-input path, diagnostic only
    # KX134 vibration (bit-bang I2C, AC magnitude per 32-sample block)
    "accel_rms_mg",       # KX134 vibration RMS, milli-g (0 if sensor absent)  [I2C]
    "accel_pk_mg",        # KX134 vibration peak, milli-g
    "temp_mv",            # NTC divider voltage on PA1/A1 (mV); app converts to deg C
    "sensor_status",      # per-channel self-verification bitmask (see SENSOR_BITS)
    "i_dc_peak_mv",       # max of legacy median-of-3 block means in the window
    "i_dc_min_mv",        # min of legacy median-of-3 block means in the window
    # Untouched ADC aggregates for the exact preceding telemetry window. These
    # bypass filtering, voltage conversion, zeroing, guards, and calibration.
    "i_adc_raw_sum",      # sum of every successful PA0 ADC conversion
    "i_adc_raw_count",    # number of PA0 conversions in that sum
    "i_adc_raw_min",      # minimum untouched PA0 ADC code in the window
    "i_adc_raw_max",      # maximum untouched PA0 ADC code in the window
    "i_adc_ema_q8",       # legacy filtered PA0 state, ADC counts * 256
    "vref_adc_raw_sum",   # sum of eight raw VREFINT conversions for this frame
    "vref_adc_raw_count", # successful VREFINT conversion count
    "vref_adc_ema_q8",    # filtered VREFINT state, ADC counts * 256
    "vref_factory_cal_raw", # this MCU's factory VREFIN_CAL word at 0x1FFF7A2A
    "frame_seq",          # monotonic telemetry-frame sequence (wraps at uint32)
    "pwm_ticks",          # actual TIM3_CCR1 speed command captured for this frame
    "i_adc_window_start_us", # MCU time of first PA0 conversion in raw window
    "i_adc_window_end_us",   # MCU time at completion of last PA0 conversion
    "i_adc_fail_count",      # PA0 conversions that timed out in this window
    "vref_window_start_us",  # MCU time immediately before frame VREFINT burst
    "vref_window_end_us",    # MCU time immediately after frame VREFINT burst
    "i_adc_raw_sumsq",       # sum of squares of every untouched PA0 conversion
    "vref_adc_raw_sumsq",    # sum of squares of the frame VREFINT conversions
    "accel_x_mg",            # ORIENTATION: gravity vector X, signed milli-g (EMA tau ~160 ms)
    "accel_y_mg",            # gravity vector Y, signed milli-g
    "accel_z_mg",            # gravity vector Z, signed milli-g, |(x,y,z)| ~ 1000 at rest
    "fault_code",            # ON-DEVICE fault detector: 0=healthy 1=loose_mount
                             # 2=rotor_drag 3=overheat 4=orientation_change 15=idle/n-a
]

# fault_code -> human name (single source of truth for app + tools)
FAULT_NAMES = {0: "healthy", 1: "loose_mount", 2: "rotor_drag", 3: "overheat", 4: "orientation_change", 15: "idle"}


# --- sensor_status bits: each channel proves itself against a physical
# invariant, so a broken wire reports FAULT instead of a plausible number. ---
SENSOR_BITS = {
    "adc":      (1 << 0, "ADC core + VDDA (VREFINT bandgap in band)"),
    "current":  (1 << 1, "INA240 output in its linear band (not railed open/short)"),
    "temp":     (1 << 2, "NTC divider between the rails (not open/shorted)"),
    "accel":    (1 << 3, "KX134 answering AND measuring ~1 g of gravity"),
    "hall":     (1 << 4, "current Hall code is legal (1..6)"),
    "spin":     (1 << 5, "motor is turning"),
    "kx_id":    (1 << 6, "KX134 WHO_AM_I matched at boot"),
    "hall_all6": (1 << 7, "all six Hall codes seen -> all 3 Hall lines proven"),
}
HALL_SEEN_SHIFT = 8      # bits 8..13 = which Hall codes 1..6 have been observed

# Channels that must be healthy for a row to be trustworthy measurement data.
# 'spin' is a state, not a health bit, so it is deliberately excluded.
REQUIRED_SENSOR_BITS = ("adc", "current", "temp", "hall")


def decode_status(value):
    """-> dict of channel -> bool, plus 'hall_seen' (set of codes) and 'faults'."""
    v = int(value or 0)
    out = {name: bool(v & bit) for name, (bit, _) in SENSOR_BITS.items()}
    out["hall_seen"] = {c for c in range(1, 7) if v & (1 << (HALL_SEEN_SHIFT + c - 1))}
    out["faults"] = [n for n in REQUIRED_SENSOR_BITS if not out.get(n)]
    return out

# --- Computed host-side from the stream and written to every session CSV.
# Current engineering units are populated only after a fresh raw-ratio affine
# calibration is valid. The untouched sums/counts above remain the source of truth
# and make every derived value reproducible.
DERIVED_COLUMNS = [
    "i_dc_a",          # total series current from raw PA0/VREFINT affine model
    "i_dc_zero_mv",    # retired auto-zero field; blank for the raw-ratio model
    "i_dc_peak_a",     # current mapped from untouched PA0 maximum in this frame
    "i_dc_min_a",      # current mapped from untouched PA0 minimum in this frame
    "i_dc_a_signed",   # same unclamped physical result as i_dc_a
    "temp_motor_c",    # motor temp (C), NTC beta equation from temp_mv
    "temp_motor_f",    # same in F (display convenience; C is the analysis unit)
]

# Reserved for sensors that are on the way (filled when firmware grows)
FUTURE_COLUMNS = [
    "temp_ambient_c",  # DS18B20 ambient temp (C)                [1-Wire]
    "i_u_a",           # phase-U current (A), X-NUCLEO-IHM08M1 [ADC]
    "i_v_a",           # phase-V current (A)
    "i_w_a",           # phase-W current (A)
    "modbus_rpm",      # BLD-510B reg $8018H actual speed         [RS-485]
    "modbus_fault",    # BLD-510B reg $801BH fault bitmask        [RS-485]
]

# Metadata columns prepended to every logged row (host-side)
# These make each row self-describing, so combined_data.csv can be filtered/merged with
# no joins (you always know a row's condition, speed setpoint, fixture, etc.).
META_COLUMNS = [
    "iso_time",              # host wall-clock ISO-8601 (UTC), the fusion clock
    "session_id",            # S###, joins this row to the run's metadata
    "fixture_state",         # mounted | commissioning_unmounted: tagged on EVERY row
    "valid_for_classifier",  # true | false: false for unmounted commissioning data
    "condition",             # healthy | imbalance | drag | ...  (the test label)
    "severity",              # none | mild | moderate | clear
    "speed_setpoint_rpm",    # commanded rpm (the controlled variable)
    "label",                 # full human-readable run label
]

# Full ordered schema written to every session CSV.
ALL_COLUMNS = META_COLUMNS + STREAM_COLUMNS + DERIVED_COLUMNS + FUTURE_COLUMNS

# Numeric types for the live stream (all integers from firmware).
STREAM_INT_COLUMNS = set(STREAM_COLUMNS)

# Physical sanity limits for validation (datasheet-grounded, 2026-06-20).
#   motor 42BL41.010: rated 4000 rpm; driver min ~150 rpm; peak current 6.0 A.
RANGE_LIMITS = {
    "rpm":            (0, 4500),     # 4000 rated + margin
    # Coast/stopped firmware can retain a last partial-revolution value above 1000;
    # keep the frame so independent channels such as PA0 current remain observable.
    "ripple_permil":  (0, 50_000),
    "hall":           (0, 7),        # retain 0/7 as explicit illegal/off states; firmware counts them
    "min_us":         (0, 5_000_000),
    "max_us":         (0, 5_000_000),
    "step":           (0, 15),        # 0-5 button steps; 9 = serial-commanded (RX firmware)
    # per-rev Hall features are per-mille; bounds are loose (catch corruption, never reject
    # a real elevated-fault value; a misaligned field shows up as a 6-7 digit number).
    "period_std_permil": (0, 50_000),
    "mech1x_permil":     (0, 50_000),
    "elec_permil":       (0, 50_000),
    "i_dc_mv":           (0, 3300),
    "adc_vref_raw":      (1, 4095),
    "adc_pc0_mv":        (0, 3300),
    "accel_rms_mg":      (0, 64000),  # 0 = no sensor; generous bound so real spikes pass
    "accel_x_mg":        (-17000, 17000),  # signed gravity component, +/-16 g full scale
    "accel_y_mg":        (-17000, 17000),
    "accel_z_mg":        (-17000, 17000),
    "fault_code":        (0, 15),
    "accel_pk_mg":       (0, 64000),
    "temp_mv":           (0, 3300),
    "sensor_status":     (0, 65535),
    "i_dc_peak_mv":      (0, 3300),
    "i_dc_min_mv":       (0, 3300),
    "i_adc_raw_sum":     (0, 4_095_000),
    "i_adc_raw_count":   (0, 1_000),
    "i_adc_raw_min":     (0, 4095),
    "i_adc_raw_max":     (0, 4095),
    "i_adc_ema_q8":      (0, 4095 * 256),
    "vref_adc_raw_sum":  (0, 4095 * 8),
    "vref_adc_raw_count": (0, 8),
    "vref_adc_ema_q8":   (0, 4095 * 256),
    "vref_factory_cal_raw": (1, 4095),
    "frame_seq":         (0, 0xFFFFFFFF),
    "pwm_ticks":         (0, 1000),
    "i_adc_window_start_us": (0, 0xFFFFFFFF),
    "i_adc_window_end_us":   (0, 0xFFFFFFFF),
    "i_adc_fail_count":      (0, 1000),
    "vref_window_start_us":  (0, 0xFFFFFFFF),
    "vref_window_end_us":    (0, 0xFFFFFFFF),
    "i_adc_raw_sumsq":       (0, (4095 ** 2) * 1000),
    "vref_adc_raw_sumsq":    (0, (4095 ** 2) * 8),
}

# Cumulative (monotonic) channels; report DELTA, never SUM, for these.
CUMULATIVE_COLUMNS = {"illegal", "edges"}
