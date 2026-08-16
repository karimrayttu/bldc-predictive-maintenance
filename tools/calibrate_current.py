"""Standalone DC-bus current calibration, driven one speed at a time."""

import argparse
import datetime
import io
import json
import math
import os
import socket
import statistics as st
import sys

import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import yaml                                             # noqa: E402
from bldc_phm.sources import find_stlink_port           # noqa: E402
from bldc_phm.schema import STREAM_COLUMNS              # noqa: E402

IDX = {c: i for i, c in enumerate(STREAM_COLUMNS)}
SESSION = os.path.join("data", "verification", "current_cal_session.json")
from bldc_phm.instruments import DMM_RESOURCE as DMM_RES, SCOPE_ADDR  # noqa: E402

SETTLE_S = 9.0          # spin-up + firmware EMA tau 256 ms (35 tau) + scope averaging
SAMPLE_S = 6.0
RAIL_TOL_V = 0.010      # a point whose rail moved more than this is flagged


# ------------------------------------------------------------------ instruments
def dmm_open():
    try:
        import pyvisa
        d = pyvisa.ResourceManager().open_resource(DMM_RES, open_timeout=5000)
        d.timeout = 20000
        for c in ("CONF:VOLT:DC 10", "SENS:VOLT:DC:RANG:AUTO OFF",
                  "SENS:VOLT:DC:RANG 10", "SENS:VOLT:DC:NPLC 10",
                  "SENS:VOLT:DC:ZERO:AUTO ON", "SENS:VOLT:DC:IMP:AUTO ON",
                  "TRIG:SOUR IMM", "SAMP:COUN 1"):
            d.write(c)
        return d
    except Exception as e:
        print(f"  (DMM unavailable: {e})")
        return None


def dmm_mv(d, n=3):
    if d is None:
        return None, None
    vals = []
    for _ in range(n):
        try:
            v = float(d.query("READ?"))
            if abs(v) < 1e30:
                vals.append(v * 1000.0)
        except Exception:
            pass
    if not vals:
        return None, None
    return st.median(vals), (st.pstdev(vals) if len(vals) > 1 else 0.0)


def scope_io(cmd, expect=True, t=8.0):
    try:
        info = socket.getaddrinfo(SCOPE_ADDR[0], SCOPE_ADDR[1],
                                  socket.AF_INET6, socket.SOCK_STREAM)[0]
    except Exception:
        return None
    s = socket.socket(info[0], socket.SOCK_STREAM)
    s.settimeout(t)
    try:
        s.connect(info[4])
        s.sendall((cmd + "\n").encode())
        if not expect:
            return None
        b = b""
        t0 = time.time()
        while not b.endswith(b"\n") and time.time() - t0 < t:
            d = s.recv(8192)
            if not d:
                break
            b += d
        return b.decode(errors="ignore").strip()
    except Exception:
        return None
    finally:
        s.close()


def scope_setup():
    if scope_io("*IDN?") is None:
        return False
    for c in (":CHANnel1:DISPlay ON", ":CHANnel1:COUPling DC",
              ":CHANnel1:IMPedance ONEM", ":CHANnel1:BWLimit ON",
              ":TIMebase:SCALe 2e-3", ":TRIGger:SWEep AUTO",
              ":ACQuire:TYPE AVERage", ":ACQuire:COUNt 16", ":RUN"):
        scope_io(c, expect=False)
    return True


def scope_read():
    def q(c):
        r = scope_io(c)
        try:
            v = float(r)
            return v if abs(v) < 1e30 else None
        except (TypeError, ValueError):
            return None
    return {"vavg_mv": (lambda v: v * 1000 if v is not None else None)(
                q(":MEASure:VAVerage? DISPlay,CHANnel1")),
            "vpp_mv": (lambda v: v * 1000 if v is not None else None)(
                q(":MEASure:VPP? CHANnel1")),
            "probe": q(":CHANnel1:PROBe?")}


# ------------------------------------------------------------------ board
def board_open():
    import serial
    port = find_stlink_port()
    if not port:
        print("  no ST-LINK found"); sys.exit(2)
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print(f"  cannot open {port}: {e}\n  CLOSE the app first; the VCP is "
              f"single-owner."); sys.exit(2)
    return ser, port


def board_sample(ser, seconds):
    rows, buf = [], b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        buf += ser.read(256)
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            p = ln.decode("ascii", "ignore").strip().split(",")
            if len(p) != len(STREAM_COLUMNS):
                continue
            try:
                rows.append({k: int(p[i]) for k, i in IDX.items()})
            except ValueError:
                pass
    return rows


def summarise(rows):
    if not rows:
        return None
    a0 = [r["i_dc_mv"] for r in rows]
    vr = st.median([r["adc_vref_raw"] for r in rows])
    return {
        "n": len(rows),
        "a0_mv": st.median(a0),
        "a0_sd": st.pstdev(a0) if len(a0) > 1 else 0.0,
        "a0_min": min(a0), "a0_max": max(a0),
        "peak_mv": st.median([r["i_dc_peak_mv"] for r in rows]),
        "min_mv": st.median([r["i_dc_min_mv"] for r in rows]),
        "rpm": st.median([r["rpm"] for r in rows]),
        "rpm_sd": st.pstdev([r["rpm"] for r in rows]) if len(rows) > 1 else 0.0,
        "temp_mv": st.median([r["temp_mv"] for r in rows]),
        "vref_raw": vr,
        "rail_v": 1.21 * 4095.0 / vr if vr else None,
        "ripple_permil": st.median([r["ripple_permil"] for r in rows]),
        "accel_rms_mg": st.median([r["accel_rms_mg"] for r in rows]),
        "sensor_status": rows[-1]["sensor_status"],
    }


# ------------------------------------------------------------------ session file
def load():
    if os.path.exists(SESSION):
        return json.load(io.open(SESSION, encoding="utf-8"))
    return {"created": None, "points": []}


def save(s):
    os.makedirs(os.path.dirname(SESSION), exist_ok=True)
    io.open(SESSION, "w", encoding="utf-8").write(json.dumps(s, indent=2) + "\n")


def stamp():
    return datetime.datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ measure
def measure(cmd_rpm, note=""):
    s = load()
    ser, port = board_open()
    have_scope = scope_setup()
    d = dmm_open()

    ser.write(f"S{int(cmd_rpm)}\n".encode()); ser.flush()
    ser.reset_input_buffer()
    print(f"  commanded {'OFF' if cmd_rpm == 0 else f'{cmd_rpm} rpm'}, "
          f"settling {SETTLE_S:.0f} s ...")
    time.sleep(SETTLE_S)

    dv1, dsd1 = dmm_mv(d)
    rows = board_sample(ser, SAMPLE_S)
    sc = scope_read() if have_scope else {}
    dv2, dsd2 = dmm_mv(d)

    b = summarise(rows)
    if b is None:
        print("  NO FRAMES; is the board streaming?")
        ser.close()
        if d:
            d.close()
        return 2

    dvals = [v for v in (dv1, dv2) if v is not None]
    pt = {
        "t": stamp(),
        "kind": "off" if cmd_rpm == 0 else "on",
        "cmd_rpm": int(cmd_rpm),
        "note": note,
        "board": b,
        "dmm_mv": (st.mean(dvals) if dvals else None),
        "dmm_spread_mv": (abs(dv1 - dv2) if None not in (dv1, dv2) else None),
        "scope": sc,
        "true_bus_a": None,          # filled in by --fit --truth
    }
    s["points"].append(pt)
    if not s.get("created"):
        s["created"] = pt["t"]
    save(s)

    print(f"\n  {'measured rpm':<22}{b['rpm']:.0f}  (sd {b['rpm_sd']:.1f})")
    print(f"  {'A0 board':<22}{b['a0_mv']:.0f} mV   sd {b['a0_sd']:.2f}   "
          f"range {b['a0_min']}-{b['a0_max']}")
    if pt["dmm_mv"] is not None:
        print(f"  {'A0 DMM':<22}{pt['dmm_mv']:.3f} mV   "
              f"(two reads differ by {pt['dmm_spread_mv']:.3f} mV)")
        print(f"  {'board - DMM':<22}{b['a0_mv'] - pt['dmm_mv']:+.2f} mV")
    if sc.get("vavg_mv") is not None:
        print(f"  {'A0 scope Vavg':<22}{sc['vavg_mv']:.2f} mV   "
              f"Vpp {sc['vpp_mv']:.1f} mV   probe {sc['probe']}:1")
    print(f"  {'rail (VREFINT)':<22}{b['rail_v']:.3f} V")
    print(f"  {'temp_mv':<22}{b['temp_mv']:.0f} mV")
    print(f"  {'peak / min A0':<22}{b['peak_mv']:.0f} / {b['min_mv']:.0f} mV")
    print(f"  {'ripple':<22}{b['ripple_permil']:.0f} permil     "
          f"vib {b['accel_rms_mg']:.0f} mg")

    if cmd_rpm == 0:
        print(f"\n  ZERO REFERENCE recorded. Motor is OFF.")
        print(f"  -> read your series meter now for the IDLE bus current.")
    else:
        print(f"\n  MOTOR LEFT RUNNING at {b['rpm']:.0f} rpm.")
        print(f"  -> read your series meter now and note the TOTAL bus current.")
        print(f"     then run:  py -3 tools\\calibrate_current.py --off")

    ser.close()
    if d:
        d.close()
    print(f"\n  {len(s['points'])} point(s) in {SESSION}")
    return 0


# ------------------------------------------------------------------ fit
def interp_zero(points, t_target):
    """Zero at time t_target, linearly interpolated between bracketing OFF points."""
    # Only SETTLED OFF points may define the baseline. An --off taken 9 s after a
    # spin is still settling: measured on 2026-07-30 those came back with the A0
    # range spanning 9 mV (1673-1682) while consecutive OFFs with no spin between
    # them are stable to 0.04 mV. Interpolating through an unsettled OFF injected a
    # 5 mV error into a 3 mV signal and turned a 250 mV/A point into 458.
    UNSETTLED_RANGE_MV = 4.0
    allo = [p for p in points if p["kind"] == "off"]
    good = [p for p in allo
            if (p["board"]["a0_max"] - p["board"]["a0_min"]) <= UNSETTLED_RANGE_MV]
    dropped = len(allo) - len(good)
    offs = [(p["_ts"], p["board"]["a0_mv"]) for p in good]
    if not offs:
        if allo:
            return None, (f"all {len(allo)} OFF points are unsettled "
                          f"(A0 range > {UNSETTLED_RANGE_MV:.0f} mV), re-take --off "
                          f"after letting the baseline sit")
        return None, "no OFF reference recorded: run --off"
    suffix = f" [{dropped} unsettled OFF ignored]" if dropped else ""
    offs.sort()
    before = [o for o in offs if o[0] <= t_target]
    after = [o for o in offs if o[0] >= t_target]
    if before and after:
        (t1, z1), (t2, z2) = before[-1], after[0]
        if t2 == t1:
            return z1, "single OFF at the same instant"
        f = (t_target - t1) / (t2 - t1)
        return (z1 + f * (z2 - z1),
                f"interpolated between {z1:.0f} and {z2:.0f} mV{suffix}")
    z = (before[-1][1] if before else after[0][1])
    return z, f"extrapolated from the nearest OFF (NOT bracketed){suffix}"


def do_fit(truth, apply_it):
    s = load()
    pts = s.get("points", [])
    if not pts:
        print(f"  no session data. Run --off and --hold first."); return 2
    for p in pts:
        p["_ts"] = datetime.datetime.fromisoformat(p["t"]).timestamp()

    for k, v in truth.items():
        best = None
        for p in pts:
            if p["kind"] != "on":
                continue
            if abs(p["board"]["rpm"] - k) < 120 or p["cmd_rpm"] == k:
                if best is None or abs(p["board"]["rpm"] - k) < abs(best["board"]["rpm"] - k):
                    best = p
        if best is None:
            print(f"  WARNING: no measured point near {k} rpm, ignoring truth {v}")
        else:
            best["true_bus_a"] = v

    cfg = yaml.safe_load(io.open("bldc_phm/config/app_config.yaml", encoding="utf-8"))
    CS = cfg["current_sensor"]
    idle = float(s.get("idle_a", CS["idle_offset_a"]))
    shunt_gain = CS["shunt_ohm"] * CS["gain_v_v"] * 1000.0

    print("=" * 82)
    print("  CURRENT CALIBRATION FIT")
    print("=" * 82)
    print(f"  session {s.get('created')}   {len(pts)} points")
    print(f"  idle_offset_a {idle} A (operator series meter, NOT fitted here)")
    print(f"  parts-derived sensitivity: {CS['shunt_ohm']}ohm x {CS['gain_v_v']} "
          f"= {shunt_gain:.1f} mV/A\n")

    # PAIRED (differential) rows: an ON point whose bracketing OFF points BOTH have
    # their own series-meter reading needs no global idle at all,
    #     k = (A0_on - A0_off) / (I_on - I_off)
    # Both differences are taken within a minute of each other, so a drift in the
    # rail AND a drift in the driver's own quiescent draw cancel out of the slope.
    # This is the only way to get an unambiguous k when the idle figure itself has
    # been observed to wander (0.05 / 0.068 / 0.069 A on this bench).
    paired = []
    for q in pts:
        if q["kind"] != "on" or q.get("true_bus_a") is None:
            continue
        # Same settledness requirement as interp_zero: an OFF still recovering from a
        # spin carries several mV of error, which is larger than the signal at every
        # speed below ~2000 rpm. Including one turned a 250 mV/A point into 458.
        def _settled(o):
            return (o["board"]["a0_max"] - o["board"]["a0_min"]) <= 4.0
        befores = [o for o in pts if o["kind"] == "off" and o["_ts"] <= q["_ts"]
                   and o.get("true_bus_a") is not None and _settled(o)]
        afters = [o for o in pts if o["kind"] == "off" and o["_ts"] >= q["_ts"]
                  and o.get("true_bus_a") is not None and _settled(o)]
        if not befores:
            continue
        near = befores[-1]
        if afters:
            # average the two bracketing OFFs: cancels linear drift in both channels
            b, aft = befores[-1], afters[0]
            i_off = (b["true_bus_a"] + aft["true_bus_a"]) / 2.0
            v_off = (b["board"]["a0_mv"] + aft["board"]["a0_mv"]) / 2.0
            how = "bracketed"
        else:
            i_off, v_off, how = near["true_bus_a"], near["board"]["a0_mv"], "one-sided"
        di = q["true_bus_a"] - i_off
        dv = q["board"]["a0_mv"] - v_off
        if di > 0:
            paired.append((q["board"]["rpm"], dv, di, how))

    if paired:
        print("  PAIRED DIFFERENTIAL POINTS  (no global idle used; each speed is")
        print("  referenced to the meter reading taken at its OWN off state)")
        print(f"  {'rpm':>6} {'dA0 mV':>8} {'dI A':>8} {'mV/A':>8}  bracket")
        for r, dv, di, how in sorted(paired):
            print(f"  {r:>6.0f} {dv:>+8.2f} {di:>8.4f} {dv/di:>8.1f}  {how}")
        if len(paired) >= 2:
            u = sum(d * i for _r, d, i, _h in paired) / sum(d * d for _r, d, i, _h in paired)
            kp = 1.0 / u if u else float("nan")
            res = [abs(d * u - i) for _r, d, i, _h in paired]
            print(f"\n  PAIRED FIT: {kp:.1f} mV/A   "
                  f"mean |resid| {1000*sum(res)/len(res):.1f} mA   "
                  f"({len(paired)} points)")
        print()

    rows = []
    rails = [p["board"]["rail_v"] for p in pts if p["board"].get("rail_v")]
    rail_span = (max(rails) - min(rails)) if rails else 0.0

    print(f"  {'rpm':>6} {'A0':>6} {'zero':>7} {'dmV':>7} {'true A':>7} "
          f"{'dI A':>7} {'mV/A':>7} {'rail V':>7}  flag")
    for p in sorted((q for q in pts if q["kind"] == "on"), key=lambda q: q["board"]["rpm"]):
        if p.get("true_bus_a") is None:
            continue
        z, how = interp_zero(pts, p["_ts"])
        if z is None:
            print(f"  {p['board']['rpm']:>6.0f}   {how}")
            continue
        dmv = p["board"]["a0_mv"] - z
        di = p["true_bus_a"] - idle
        flags = []
        notes = []
        if di <= 0:
            flags.append("true<=idle")
        if abs(p["board"]["rpm"] - p["cmd_rpm"]) > 250:
            flags.append("speed-miss")
        if p["board"]["a0_sd"] > 4.0:
            flags.append("noisy")
        # "unbracketed" is INFORMATIONAL, not disqualifying. Once unsettled OFFs are
        # excluded, the only surviving reference is usually the pre-spin OFF, and
        # that is the CORRECT zero to use, because it was taken before the spin
        # perturbed the baseline. Treating it as a defect rejected every good point.
        if "extrapolated" in how:
            notes.append("pre-spin zero")
        mva = (dmv / di) if di > 0 else float("nan")
        print(f"  {p['board']['rpm']:>6.0f} {p['board']['a0_mv']:>6.0f} {z:>7.1f} "
              f"{dmv:>+7.2f} {p['true_bus_a']:>7.3f} {di:>7.4f} {mva:>7.1f} "
              f"{p['board']['rail_v']:>7.3f}  "
              f"{','.join(flags + notes)}")
        if di > 0 and not flags:
            rows.append((p["board"]["rpm"], dmv, di))

    print(f"\n  rail span across the whole session: {rail_span*1000:.1f} mV "
          f"({'OK' if rail_span < RAIL_TOL_V else 'TOO MUCH; points are not comparable'})")

    if len(rows) < 2:
        print(f"\n  need at least 2 clean points with a true current. Have {len(rows)}.")
        return 1

    # least squares in AMPS: minimise sum (dmv*u - di)^2, u = 1/k. Amps is the
    # quantity that matters, so the residual is minimised there, not in millivolts.
    u = sum(d * i for _r, d, i in rows) / sum(d * d for _r, d, i in rows)
    k = 1.0 / u if u else float("nan")
    resid = [(r, (d * u) - i) for r, d, i in rows]
    rms = math.sqrt(sum(e * e for _r, e in resid) / len(resid))
    # through-origin fit in mV as a cross-check
    k2 = sum(d * i for _r, d, i in rows) / sum(i * i for _r, d, i in rows)

    print("\n" + "-" * 82)
    print(f"  BEST FIT           {k:.1f} mV/A     (cross-check, mV-space fit: {k2:.1f})")
    print(f"  residual RMS       {1000*rms:.1f} mA")
    print(f"  vs parts-derived   {shunt_gain:.1f} mV/A  "
          f"({100*(k-shunt_gain)/shunt_gain:+.0f}%)")
    print(f"  points used        {len(rows)} of "
          f"{sum(1 for p in pts if p['kind']=='on')} ON points")
    print(f"\n  {'rpm':>6} {'err mA':>8}")
    for r, e in sorted(resid):
        print(f"  {r:>6.0f} {1000*e:>+8.1f}")

    gates = {
        "at least 3 clean points": len(rows) >= 3,
        "residual RMS under 10 mA": rms < 0.010,
        f"rail stable within {RAIL_TOL_V*1000:.0f} mV": rail_span < RAIL_TOL_V,
        "fit within 40% of parts-derived": abs(k - shunt_gain) / shunt_gain < 0.40,
        "sensitivity positive": k > 0,
    }
    print(f"\n  {'QUALITY GATES':<40}")
    for name, ok in gates.items():
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}")

    # RATIO-MODEL FIT: amps = slope * (PA0_raw / VREF_raw) + intercept
    # This is the model app_config declares (calibration_model raw_pa0_vref_affine_v1)
    # and the one MainWindow._augment_current actually reads. Fitting in RATIO space
    # rather than millivolts makes the result immune to VDDA moving, because PA0 and
    # VREFINT scale together: which is precisely the failure that produced today's
    # 133/175.7/191/194/238.5/296.3 spread.
    #
    # HONEST LIMIT: the firmware streams i_dc_mv as an INTEGER millivolt, not the raw
    # count, so PA0_raw is reconstructed as mv*4095/3300 and is only known to about
    # +/-0.6 LSB. A true raw calibration needs main.c to stream the ADC count.
    def _ratio(mv, vref):
        return (mv * 4095.0 / 3300.0) / float(vref)

    rpts = []
    for q in pts:
        if q.get("true_bus_a") is None or not q["board"].get("vref_raw"):
            continue
        # Settledness disqualifies an OFF only. An OFF with A0 still moving is a
        # contaminated ZERO; there is nothing legitimate about it. An ON point's
        # spread, by contrast, is real current variation from the speed hunting;
        # its median is still a valid operating point, so it stays.
        if (q["kind"] == "off"
                and (q["board"]["a0_max"] - q["board"]["a0_min"]) > 4.0):
            continue
        rpts.append((_ratio(q["board"]["a0_mv"], q["board"]["vref_raw"]),
                     q["true_bus_a"], q["board"]["rpm"], q["kind"]))
    ratio_slope = ratio_icept = None
    if len(rpts) >= 3:
        n = len(rpts)
        mx = sum(r for r, _a, _s, _k in rpts) / n
        my = sum(a for _r, a, _s, _k in rpts) / n
        sxy = sum((r - mx) * (a - my) for r, a, _s, _k in rpts)
        sxx = sum((r - mx) ** 2 for r, _a, _s, _k in rpts)
        if sxx > 0:
            ratio_slope = sxy / sxx
            ratio_icept = my - ratio_slope * mx
            print("  RATIO MODEL  amps = slope * (PA0_raw/VREF_raw) + intercept")
            print(f"  {'rpm':>6} {'kind':>4} {'ratio':>10} {'true A':>8} "
                  f"{'fit A':>8} {'err mA':>8}")
            rerr = []
            for r, a, spd, kind in sorted(rpts, key=lambda z: z[2]):
                fa = ratio_slope * r + ratio_icept
                rerr.append(abs(fa - a))
                print(f"  {spd:>6.0f} {kind:>4} {r:>10.5f} {a:>8.4f} {fa:>8.4f} "
                      f"{1000*(fa-a):>+8.2f}")
            vrefs = {q["board"]["vref_raw"] for q in pts if q["board"].get("vref_raw")}
            print(f"\n  slope     {ratio_slope:.6f} A per unit ratio")
            print(f"  intercept {ratio_icept:.6f} A")
            print(f"  worst residual {1000*max(rerr):.2f} mA over {n} points")
            print(f"  equivalent sensitivity: "
                  f"{1.0/(ratio_slope*4095.0/3300.0/max(vrefs)):.1f} mV/A")
            if len(vrefs) == 1:
                print(f"  NOTE: VREF_raw was constant ({vrefs.pop()}) for every point, so"
                      f"\n  this dataset cannot demonstrate the ratio model's rail"
                      f"\n  immunity; here it is mathematically the same as the mV fit.")
            print()


    if not apply_it:
        print(f"\n  not applied. Re-run with --apply to write mv_per_amp = {k:.1f}")
        return 0
    if not all(gates.values()):
        print(f"\n  REFUSING to apply: quality gates failed. Fix the bench, not the number.")
        return 1

    if ratio_slope is None:
        print("\n  REFUSING to apply: the ratio model needs >=3 points with a true "
              "current.\n  That is the model app_config declares and the app reads.")
        return 1

    path = "bldc_phm/config/app_config.yaml"
    txt = io.open(path, encoding="utf-8").read()
    import re
    # Match `key: <anything>` so it works whether the current value is a number,
    # `null`, or quoted. The old patterns required [\d.]+ and therefore silently
    # matched NOTHING once the fields had been nulled, the apply then reported
    # success while writing no change at all. Every substitution is verified below.
    def setkey(text, key, value):
        pat = rf"^(  {re.escape(key)}:)[^\n]*"
        out, n = re.subn(pat, lambda m: f"{m.group(1)} {value}", text,
                         count=1, flags=re.M)
        return out, n

    updates = [
        ("raw_ratio_slope_a_per_ratio", f"{ratio_slope:.6f}"),
        ("raw_ratio_intercept_a", f"{ratio_icept:.6f}"),
        ("calibration_valid", "true"),
        ("calibration_quality", f"{1000*max(rerr):.2f}   # worst residual, mA"),
        ("calibration_session", f'"{s.get("created")}"'),
        ("mv_per_amp", f"{k:.1f}"),
        ("idle_offset_a", f"{idle:.4f}"),
        ("calibration_date", f'"{datetime.date.today().isoformat()}"'),
        ("calibration_method",
         f'"ratio fit over {len(rpts)} synchronized points, '
         f'worst residual {1000*max(rerr):.2f} mA; series meter paired per point"'),
    ]
    new = txt
    failed = []
    for key, val in updates:
        new, n = setkey(new, key, val)
        if n != 1:
            failed.append(key)
    if failed:
        print(f"\n  REFUSING partial apply: could not update {failed} in {path}")
        return 1
    # Keep board-side and host-side calculations on the exact same measured
    # slope. A disagreement is a useful diagnostic; it must not be caused by a
    # stale duplicated constant.
    # ONLY if the firmware actually carries a slope. Verified 2026-07-30: it does
    # not, and that is deliberate, main.c streams raw i_dc_mv and every amp value
    # is computed host-side in MainWindow._augment_current. current_cal.h says so in
    # its own header comment ("No current sensitivity, zero, idle current, or fitted
    # coefficient is compiled into the MCU"). Writing a sensitivity define there
    # would create a SECOND source of truth for a number the MCU never reads, which
    # is the exact stale-duplicate failure that comment exists to prevent. So this
    # syncs the define when one is present and otherwise leaves the firmware alone.
    cal_path = "firmware/current_cal.h"
    cal_note = "firmware carries no slope by design, host-side conversion only"
    if os.path.exists(cal_path):
        cal = io.open(cal_path, encoding="utf-8").read()
        cal_new = re.sub(
            r"^#define CURRENT_SENSITIVITY_UV_PER_A\s+\d+u",
            f"#define CURRENT_SENSITIVITY_UV_PER_A   {int(round(k * 1000.0))}u",
            cal, count=1, flags=re.M)
        if cal_new != cal:
            io.open(cal_path, "w", encoding="utf-8").write(cal_new)
            cal_note = f"firmware slope define synced in {cal_path}"
    io.open(path, "w", encoding="utf-8").write(new)
    print(f"\n  APPLIED: mv_per_amp = {k:.1f} mV/A  "
          f"(residual RMS {1000*rms:.1f} mA)")
    print(f"  {cal_note}")
    print(f"  raw session kept at {SESSION}")
    return 0


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--new", action="store_true", help="start a fresh session")
    ap.add_argument("--off", action="store_true", help="measure the 0 A reference")
    ap.add_argument("--hold", type=int, help="spin to RPM, measure, leave running")
    ap.add_argument("--note", default="")
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--truth", default="", help='e.g. "800=0.086,1200=0.101"')
    ap.add_argument("--record", default="", metavar="RPM=AMPS",
                    help="persist one series-meter reading on the nearest ON point")
    ap.add_argument("--record-off", type=float, metavar="AMPS",
                    help="series-meter reading taken at the LAST --off point. Pairing "
                         "each speed with its own OFF reading is what removes the "
                         "idle ambiguity from the slope entirely.")
    ap.add_argument("--record-idle", type=float, metavar="AMPS",
                    help="persist the motor-OFF total bus current")
    ap.add_argument("--apply", action="store_true", help="write mv_per_amp to the config")
    ap.add_argument("--show", action="store_true", help="list the session")
    a = ap.parse_args()

    if a.new:
        if os.path.exists(SESSION):
            bak = SESSION.replace(".json", f"_{int(time.time())}.json")
            os.rename(SESSION, bak)
            print(f"  previous session archived -> {bak}")
        save({"created": stamp(), "points": []})
        print(f"  new session at {SESSION}")
        return 0

    if a.show:
        s = load()
        print(f"  {len(s.get('points', []))} points, created {s.get('created')}, "
              f"idle {s.get('idle_a')}")
        for p in s.get("points", []):
            print(f"    {p['t']}  {p['kind']:<3} cmd {p['cmd_rpm']:>5}  "
                  f"rpm {p['board']['rpm']:>6.0f}  A0 {p['board']['a0_mv']:>6.0f} mV  "
                  f"rail {p['board']['rail_v']:.3f} V  true "
                  f"{p.get('true_bus_a')}")
        return 0

    if a.record_off is not None:
        if not (0.0 <= a.record_off <= 10.0):
            print("  current must be between 0 and 10 A"); return 2
        s = load()
        offs = [q for q in s.get("points", []) if q.get("kind") == "off"]
        if not offs:
            print("  no --off point recorded yet"); return 2
        offs[-1]["true_bus_a"] = float(a.record_off)
        save(s)
        print(f"  recorded {a.record_off:.4f} A at the OFF point "
              f"{offs[-1]['t']} (A0 {offs[-1]['board']['a0_mv']:.0f} mV)")
        return 0

    if a.record_idle is not None:
        if not (0.0 <= a.record_idle <= 2.0):
            print("  idle current must be between 0 and 2 A"); return 2
        s = load()
        s["idle_a"] = float(a.record_idle)
        save(s)
        print(f"  recorded idle total bus current = {a.record_idle:.4f} A")
        return 0

    if a.record:
        try:
            rpm_s, amps_s = a.record.split("=", 1)
            rpm, amps = int(float(rpm_s)), float(amps_s)
        except ValueError:
            print(f"  bad --record value {a.record!r}; expected RPM=AMPS"); return 2
        if not (0.0 <= amps <= 10.0):
            print("  current must be between 0 and 10 A"); return 2
        s = load()
        candidates = [p for p in s.get("points", [])
                      if p.get("kind") == "on"
                      and (p.get("cmd_rpm") == rpm
                           or abs(float(p["board"]["rpm"]) - rpm) < 150)]
        if not candidates:
            print(f"  no ON measurement found near {rpm} rpm"); return 2
        best = min(candidates,
                   key=lambda p: (abs(int(p.get("cmd_rpm", 0)) - rpm),
                                  abs(float(p["board"]["rpm"]) - rpm),
                                  -datetime.datetime.fromisoformat(p["t"]).timestamp()))
        best["true_bus_a"] = amps
        save(s)
        print(f"  recorded {amps:.4f} A for cmd {best['cmd_rpm']} rpm "
              f"(measured {best['board']['rpm']:.0f} rpm)")
        return 0

    if a.fit:
        truth = {}
        for chunk in a.truth.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                r, v = chunk.split("=")
                truth[int(float(r))] = float(v)
            except ValueError:
                print(f"  bad --truth entry: {chunk!r}"); return 2
        return do_fit(truth, a.apply)

    if a.off:
        return measure(0, a.note)
    if a.hold is not None:
        return measure(a.hold, a.note)

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
