"""Measure the current-sense chain's ACTUAL transfer function, end to end."""

import argparse
import os
import socket
import statistics as st
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bldc_phm.sources import find_stlink_port          # noqa: E402
from bldc_phm.schema import STREAM_COLUMNS             # noqa: E402

I_MV = STREAM_COLUMNS.index("i_dc_mv")
RPM = STREAM_COLUMNS.index("rpm")
from bldc_phm.instruments import DMM_RESOURCE as DMM_RES  # noqa: E402
from bldc_phm.instruments import SCOPE_ADDR as SCOPE  # noqa: E402
R_SHUNT = 0.005          # Vishay WSL25125L000FEA18, 1%
SETTLE_S = 9.0


def scope_io(cmd, expect=True):
    i = socket.getaddrinfo(SCOPE[0], SCOPE[1], socket.AF_INET6, socket.SOCK_STREAM)[0]
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM); s.settimeout(15)
    s.connect(i[4])
    try:
        s.sendall((cmd + "\n").encode())
        if not expect:
            return None
        b = b""
        while not b.endswith(b"\n"):
            d = s.recv(8192)
            if not d:
                break
            b += d
        return b.decode(errors="ignore").strip()
    finally:
        s.close()


def scope_mv():
    try:
        v = float(scope_io(":MEASure:VAVerage? DISPlay,CHANnel1"))
        return v * 1000.0 if abs(v) < 1e30 else None
    except Exception:
        return None


def board_mv(ser, seconds=3.0):
    mvs, rpms = [], []
    buf = b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        buf += ser.read(256)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            p = line.decode("ascii", "ignore").strip().split(",")
            if len(p) != len(STREAM_COLUMNS):
                continue
            try:
                mvs.append(int(p[I_MV])); rpms.append(int(p[RPM]))
            except ValueError:
                pass
    if not mvs:
        return None, None
    return st.median(mvs), st.median(rpms)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("speeds", nargs="*", type=int)
    args = ap.parse_args()
    speeds = args.speeds or [400, 800, 1200, 1600, 2000]

    import serial, pyvisa
    port = find_stlink_port()
    if not port:
        print("  no ST-LINK"); return 2
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print(f"  cannot open {port}: {e}\n  CLOSE the app first."); return 2
    ser.reset_input_buffer()

    dmm = pyvisa.ResourceManager().open_resource(DMM_RES, open_timeout=6000)
    dmm.timeout = 20000
    # 1 V range: the shunt differential is only a few mV, but switching transients
    # overload the 100 mV range. NPLC 100 = 1.67 s aperture for maximum averaging.
    for c in ("CONF:VOLT:DC 1", "SENS:VOLT:DC:RANG:AUTO OFF", "SENS:VOLT:DC:RANG 1",
              "SENS:VOLT:DC:NPLC 100", "SENS:VOLT:DC:ZERO:AUTO ON", "TRIG:SOUR IMM"):
        dmm.write(c)

    def dmm_mv(n=3):
        vals = []
        for _ in range(n):
            try:
                v = float(dmm.query("READ?"))
                if abs(v) < 1e30:
                    vals.append(v * 1000.0)
            except Exception:
                pass
        return st.median(vals) if vals else None

    for c in (":CHANnel1:DISPlay ON", ":CHANnel1:COUPling DC", ":CHANnel1:BWLimit ON",
              ":CHANnel1:SCALe 0.5", ":CHANnel1:OFFSet 1.8", ":TIMebase:SCALe 1e-3",
              ":TRIGger:SWEep AUTO", ":ACQuire:TYPE AVERage", ":ACQuire:COUNt 32"):
        scope_io(c, expect=False)

    print("=" * 80)
    print("  CURRENT-SENSE CHAIN TRANSFER FUNCTION  (DMM on shunt, scope on A0)")
    print("=" * 80)
    print(f"  shunt {R_SHUNT*1000:.0f} mOhm   DMM 1 V range @100 NPLC   scope avg x32")
    print(f"  chopped OFF/ON/OFF, {SETTLE_S:.0f} s settle per state\n")

    def point(cmd):
        ser.write(f"S{int(cmd)}\n".encode()); ser.flush()
        ser.reset_input_buffer()
        time.sleep(SETTLE_S)
        vin = dmm_mv()
        vout = scope_mv()
        bmv, brpm = board_mv(ser)
        return {"cmd": cmd, "rpm": brpm, "vin": vin, "vout": vout, "board": bmv}

    rows = []
    off_prev = point(0)
    print(f"  {'cmd':>5} {'rpm':>6} {'Vshunt mV':>11} {'A0 mV':>9} {'board mV':>9}")
    print(f"  {'OFF':>5} {off_prev['rpm']:>6.0f} "
          f"{off_prev['vin'] if off_prev['vin'] is not None else float('nan'):>11.4f} "
          f"{off_prev['vout'] if off_prev['vout'] is not None else float('nan'):>9.2f} "
          f"{off_prev['board'] if off_prev['board'] is not None else float('nan'):>9.1f}")
    for sp in speeds:
        on = point(sp)
        off = point(0)
        print(f"  {sp:>5} {on['rpm']:>6.0f} "
              f"{on['vin'] if on['vin'] is not None else float('nan'):>11.4f} "
              f"{on['vout'] if on['vout'] is not None else float('nan'):>9.2f} "
              f"{on['board'] if on['board'] is not None else float('nan'):>9.1f}")
        rows.append((on, off_prev, off))
        off_prev = off

    print("\n" + "=" * 80)
    print("  MEASURED TRANSFER FUNCTION (drift-cancelled)")
    print("=" * 80)
    print(f"  {'rpm':>6} {'dVin mV':>9} {'dVout mV':>9} {'GAIN':>7} "
          f"{'dI (A)':>8} {'mV/A':>8}")
    gains, mvas = [], []
    for on, o1, o2 in rows:
        if None in (on["vin"], on["vout"], o1["vin"], o2["vin"], o1["vout"], o2["vout"]):
            print(f"  {on['rpm']:>6.0f}   (missing reading)")
            continue
        dvin = on["vin"] - (o1["vin"] + o2["vin"]) / 2
        dvout = on["vout"] - (o1["vout"] + o2["vout"]) / 2
        if abs(dvin) < 1e-6:
            continue
        g = dvout / dvin
        di = dvin / 1000.0 / R_SHUNT
        mva = dvout / di if di else float("nan")
        gains.append(g); mvas.append(mva)
        print(f"  {on['rpm']:>6.0f} {dvin:>9.4f} {dvout:>9.2f} {g:>7.2f} "
              f"{di:>8.3f} {mva:>8.1f}")

    if gains:
        print("\n" + "-" * 80)
        print(f"  GAIN        median {st.median(gains):7.2f} V/V   "
              f"range {min(gains):.2f} .. {max(gains):.2f}")
        print(f"  mv_per_amp  median {st.median(mvas):7.1f} mV/A   "
              f"range {min(mvas):.0f} .. {max(mvas):.0f}")
        print(f"\n  INA240 options: A1=20  A2=50  A3=100  A4=200 V/V")
        best = min((20, 50, 100, 200), key=lambda g: abs(g - st.median(gains)))
        print(f"  closest standard gain to the measurement: {best} V/V "
              f"({100*abs(best-st.median(gains))/best:.0f}% away)")
        print(f"  mv_per_amp implied by that part: {best*R_SHUNT*1000:.0f} mV/A")

    ser.write(b"S0\n"); ser.flush()
    dmm.close(); ser.close()
    print("\n  motor OFF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
