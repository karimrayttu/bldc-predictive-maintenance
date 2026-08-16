"""Step the motor through several speeds and log, per frame, exactly what the board
reports on A0 with a wall-clock stamp, so an external instrument sampled during the
same window can be matched against it afterwards.
"""

import datetime
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
HOLD_S = float(os.environ.get("A0_HOLD_S", 45.0))
SETTLE_S = 8.0        # spin-up + the 256 ms EMA (>30 tau)


def main():
    speeds = [int(a) for a in sys.argv[1:]] or [0, 800, 1600, 2400]
    import serial
    port = find_stlink_port()
    if not port:
        print("  no ST-LINK"); return 2
    try:
        ser = serial.Serial(port, 115200, timeout=0.2)
    except Exception as e:
        print(f"  cannot open {port}: {e}\n  CLOSE the app first."); return 2
    ser.reset_input_buffer()

    outdir = os.path.join(ROOT, "data", "verification")
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(outdir, f"a0_chain_{stamp}.csv")
    f = open(path, "w", newline="", encoding="utf-8")
    f.write("unix_time,cmd_rpm,measured_rpm,i_dc_mv\n")

    print(f"  logging to {os.path.basename(path)}")
    print(f"  {len(speeds)} steps x {HOLD_S:.0f} s = {len(speeds)*HOLD_S:.0f} s total")
    print(f"  READ THE DMM during each HOLD window printed below.\n", flush=True)
    t_start = time.time()
    try:
        for sp in speeds:
            ser.write(f"S{sp}\n".encode()); ser.flush()
            t0 = time.time()
            print(f"  [{t0-t_start:6.1f}s] commanded S{sp}, settling {SETTLE_S:.0f}s",
                  flush=True)
            mvs, rpms = [], []
            buf = b""
            announced = False
            while time.time() - t0 < HOLD_S:
                buf += ser.read(256)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    p = line.decode("ascii", "ignore").strip().split(",")
                    if len(p) != len(STREAM_COLUMNS):
                        continue
                    try:
                        mv = int(p[I_MV]); rr = int(p[RPM])
                    except ValueError:
                        continue
                    now = time.time()
                    f.write(f"{now:.3f},{sp},{rr},{mv}\n")
                    if now - t0 >= SETTLE_S:
                        mvs.append(mv); rpms.append(rr)
                if not announced and time.time() - t0 >= SETTLE_S:
                    announced = True
                    print(f"  [{time.time()-t_start:6.1f}s] HOLD WINDOW OPEN for S{sp} "
                          f"-- read the DMM now", flush=True)
            f.flush()
            if mvs:
                print(f"  [{time.time()-t_start:6.1f}s] S{sp} done: rpm "
                      f"{st.median(rpms):.0f}  A0 {st.median(mvs):.1f} mV  "
                      f"(n={len(mvs)}, min {min(mvs)} max {max(mvs)})", flush=True)
    finally:
        try:
            ser.write(b"S0\n"); ser.flush(); ser.close()
        except Exception:
            pass
        f.close()
    print(f"\n  saved {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
