"""Three-instrument verification of A0 at every plan speed, fully automated."""

import argparse
import datetime
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
from bldc_phm.instruments import SCOPE_ADDR  # noqa: E402

SETTLE_S = 9.0        # spin-up + firmware EMA tau 256 ms (>35 tau) + scope averaging
SAMPLE_S = 5.0


# ----------------------------------------------------------------- scope
class Scope:
    def __init__(self, addr):
        self.addr = addr

    def _io(self, cmd, expect=True):
        info = socket.getaddrinfo(self.addr[0], self.addr[1],
                                 socket.AF_INET6, socket.SOCK_STREAM)[0]
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.settimeout(12)
        s.connect(info[4])
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

    def w(self, cmd):
        self._io(cmd, expect=False)

    def q(self, cmd):
        return self._io(cmd, expect=True)

    def idn(self):
        return self.q("*IDN?")

    def setup_dc(self):
        for c in (":CHANnel1:DISPlay ON", ":CHANnel1:COUPling DC",
                  ":CHANnel1:IMPedance ONEM", ":CHANnel1:BWLimit ON",
                  ":CHANnel1:SCALe 0.5", ":CHANnel1:OFFSet 1.8",
                  ":TIMebase:SCALe 1e-3", ":TRIGger:SWEep AUTO",
                  ":ACQuire:TYPE AVERage", ":ACQuire:COUNt 32"):
            self.w(c)

    def setup_ac(self):
        """AC coupled, fine scale, fast timebase: to see driver chopper ripple."""
        for c in (":CHANnel1:COUPling AC", ":CHANnel1:BWLimit OFF",
                  ":CHANnel1:SCALe 0.02", ":CHANnel1:OFFSet 0",
                  ":TIMebase:SCALe 20e-6", ":ACQuire:TYPE NORMal",
                  ":TRIGger:SWEep AUTO"):
            self.w(c)

    def dc_mv(self):
        try:
            return float(self.q(":MEASure:VAVerage? DISPlay,CHANnel1")) * 1000.0
        except Exception:
            return None

    def ripple(self):
        out = {}
        for key, cmd in (("vpp", ":MEASure:VPP? CHANnel1"),
                         ("vrms", ":MEASure:VRMS? DISPlay,AC,CHANnel1"),
                         ("freq", ":MEASure:FREQuency? CHANnel1")):
            try:
                v = float(self.q(cmd))
                out[key] = v if abs(v) < 1e30 else None
            except Exception:
                out[key] = None
        return out


# ----------------------------------------------------------------- dmm
class Dmm:
    def __init__(self, res):
        import pyvisa
        self.i = pyvisa.ResourceManager().open_resource(res, open_timeout=6000)
        self.i.timeout = 8000
        for c in ("CONF:VOLT:DC 10", "SENS:VOLT:DC:RANG:AUTO OFF",
                  "SENS:VOLT:DC:RANG 10", "SENS:VOLT:DC:NPLC 10",
                  "SENS:VOLT:DC:IMP:AUTO ON", "SENS:VOLT:DC:ZERO:AUTO ON",
                  "TRIG:SOUR IMM"):
            self.i.write(c)

    def mv(self):
        try:
            return float(self.i.query("READ?")) * 1000.0
        except Exception:
            return None

    def close(self):
        try:
            self.i.close()
        except Exception:
            pass


# ----------------------------------------------------------------- board
def board_collect(ser, seconds):
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
        return None, None, 0
    return st.median(mvs), st.median(rpms), len(mvs)


def measure_point(ser, scope, dmm, cmd_rpm):
    """Command a speed, settle, then sample all three instruments together."""
    ser.write(f"S{int(cmd_rpm)}\n".encode()); ser.flush()
    ser.reset_input_buffer()
    time.sleep(SETTLE_S)
    # interleave so the three samples straddle the same window
    d1 = dmm.mv()
    b_mv, b_rpm, n = board_collect(ser, SAMPLE_S / 2)
    sc = scope.dc_mv()
    b2_mv, _r2, n2 = board_collect(ser, SAMPLE_S / 2)
    d2 = dmm.mv()
    dmm_mv = st.mean([v for v in (d1, d2) if v is not None]) if (d1 or d2) else None
    board_mv = st.mean([v for v in (b_mv, b2_mv) if v is not None]) if (b_mv or b2_mv) else None
    return {"cmd": cmd_rpm, "rpm": b_rpm, "dmm": dmm_mv, "scope": sc,
            "board": board_mv, "n": (n or 0) + (n2 or 0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("speeds", nargs="*", type=int)
    ap.add_argument("--ac", action="store_true", help="also hunt chopper ripple")
    args = ap.parse_args()
    speeds = args.speeds or [400, 800, 1200, 1600, 2000, 2400]

    import serial
    port = find_stlink_port()
    if not port:
        print("  no ST-LINK"); return 2
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print(f"  cannot open {port}: {e}\n  CLOSE the app first."); return 2
    ser.reset_input_buffer()

    scope = Scope(SCOPE_ADDR)
    try:
        idn = scope.idn()
    except Exception as e:
        print(f"  scope unreachable: {e}"); ser.close(); return 2
    try:
        dmm = Dmm(DMM_RES)
    except Exception as e:
        print(f"  dmm unreachable: {e}"); ser.close(); return 2

    print("=" * 78)
    print("  THREE-INSTRUMENT VERIFICATION OF A0")
    print("=" * 78)
    print(f"  BOARD : NUCLEO-F401RE on {port}")
    print(f"  SCOPE : {idn}")
    print(f"  DMM   : {dmm.i.query('*IDN?').strip()}")
    print(f"  method: chopped OFF/ON/OFF, {SETTLE_S:.0f} s settle, {SAMPLE_S:.0f} s sample\n")
    scope.setup_dc()

    rows = []
    try:
        base = measure_point(ser, scope, dmm, 0)
        print(f"  {'cmd':>5} {'rpm':>6} {'DMM':>9} {'SCOPE':>9} {'BOARD':>9} "
              f"{'spread':>8} {'b-DMM':>8}")
        print(f"  {'OFF':>5} {base['rpm']:>6.0f} {base['dmm']:>9.2f} "
              f"{base['scope']:>9.2f} {base['board']:>9.1f} "
              f"{max(base['dmm'],base['scope'],base['board'])-min(base['dmm'],base['scope'],base['board']):>8.2f} "
              f"{base['board']-base['dmm']:>+8.2f}")
        prev_off = base
        for sp in speeds:
            on = measure_point(ser, scope, dmm, sp)
            off = measure_point(ser, scope, dmm, 0)
            vals = [on['dmm'], on['scope'], on['board']]
            spread = max(vals) - min(vals)
            print(f"  {sp:>5} {on['rpm']:>6.0f} {on['dmm']:>9.2f} {on['scope']:>9.2f} "
                  f"{on['board']:>9.1f} {spread:>8.2f} {on['board']-on['dmm']:>+8.2f}")
            rows.append((sp, on, prev_off, off))
            prev_off = off
    finally:
        pass

    print("\n" + "=" * 78)
    print("  DRIFT-CANCELLED RISE ABOVE MOTOR-OFF  (delta = ON - mean of OFF either side)")
    print("=" * 78)
    print(f"  {'rpm':>6} {'DMM d':>9} {'SCOPE d':>9} {'BOARD d':>9} "
          f"{'b/DMM':>7} {'s/DMM':>7}")
    for sp, on, o1, o2 in rows:
        dd = on['dmm'] - (o1['dmm'] + o2['dmm']) / 2
        ds = on['scope'] - (o1['scope'] + o2['scope']) / 2
        db = on['board'] - (o1['board'] + o2['board']) / 2
        print(f"  {on['rpm']:>6.0f} {dd:>+9.2f} {ds:>+9.2f} {db:>+9.2f} "
              f"{db/dd if dd else float('nan'):>7.2f} {ds/dd if dd else float('nan'):>7.2f}")
    print("\n  b/DMM and s/DMM near 1.00 == the board and scope agree with the 6.5-digit")
    print("  reference on the CHANGE, which is what a current measurement depends on.")

    if args.ac:
        print("\n" + "=" * 78)
        print("  CHOPPER RIPPLE HUNT (AC coupled, 20 mV/div, 20 us/div)")
        print("=" * 78)
        scope.setup_ac()
        for sp in (0, speeds[len(speeds)//2], speeds[-1]):
            ser.write(f"S{int(sp)}\n".encode()); ser.flush()
            time.sleep(SETTLE_S)
            r = scope.ripple()
            print(f"  {sp:>5} rpm  Vpp {r['vpp']*1000 if r['vpp'] else float('nan'):8.2f} mV   "
                  f"Vrms(AC) {r['vrms']*1000 if r['vrms'] else float('nan'):8.3f} mV   "
                  f"freq {r['freq']/1000 if r['freq'] else float('nan'):9.3f} kHz")
        scope.setup_dc()

    # leave the motor at a mid speed so the app dashboard can be checked live
    hold = speeds[len(speeds)//2]
    ser.write(f"S{hold}\n".encode()); ser.flush()
    print(f"\n  motor LEFT RUNNING at {hold} rpm, reopen the app to verify the dashboard.")
    dmm.close(); ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
