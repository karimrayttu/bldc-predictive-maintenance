"""Hold one commanded speed and log what the board reports on A0, so an external
instrument can be read at the same moment.
"""

import os
import statistics as st
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from bldc_phm.sources import find_stlink_port      # noqa: E402
from bldc_phm.schema import STREAM_COLUMNS         # noqa: E402

I_MV = STREAM_COLUMNS.index("i_dc_mv")
RPM = STREAM_COLUMNS.index("rpm")
SETTLE_S = 6.0          # spin-up + the firmware's 256 ms EMA (>20 tau)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    rpm_cmd = int(sys.argv[1])
    total = float(sys.argv[2]) if len(sys.argv) > 2 else 20.0

    import serial
    port = find_stlink_port()
    if not port:
        print("  No ST-LINK found."); return 2
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print(f"  Cannot open {port}: {e}")
        print("  CLOSE the BLDC Motor Bench app first.")
        return 2
    ser.reset_input_buffer()

    mvs, rpms = [], []
    try:
        ser.write(f"S{rpm_cmd}\n".encode()); ser.flush()
        print(f"  commanded S{rpm_cmd}; settling {SETTLE_S:.0f} s "
              f"then sampling {total - SETTLE_S:.0f} s ...", flush=True)
        end = time.monotonic() + total
        t_sample = time.monotonic() + SETTLE_S
        buf = b""
        while time.monotonic() < end:
            buf += ser.read(256)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                p = line.decode("ascii", "ignore").strip().split(",")
                if len(p) != len(STREAM_COLUMNS):
                    continue
                if time.monotonic() < t_sample:
                    continue
                try:
                    mvs.append(int(p[I_MV])); rpms.append(int(p[RPM]))
                except ValueError:
                    pass
    finally:
        try:
            ser.close()      # leave the speed AS COMMANDED so the DMM can be read
        except Exception:
            pass

    if not mvs:
        print("  no frames captured"); return 1
    print(f"  RESULT  cmd={rpm_cmd}  measured_rpm={st.median(rpms):.0f}  "
          f"A0={st.median(mvs):.1f} mV  (n={len(mvs)}, "
          f"min {min(mvs)} max {max(mvs)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
