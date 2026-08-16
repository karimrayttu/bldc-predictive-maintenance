"""BLDC Motor Bench: setup doctor."""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

RUN, BUILD, FLASH, LINK = "RUN", "BUILD", "FLASH", "LINK"
BLOCKER, DEGRADED = "BLOCKER", "DEGRADED"

# ST does not publish CubeIDE or CubeProgrammer to winget (verified: `winget search
# STM32CubeProgrammer` -> no package). Their installers are behind a MyST account, so
# flashing cannot be made fully unattended. The Arm toolchain IS on winget, and CubeIDE
# bundles both the compiler and the programmer CLI in its plugins, so one CubeIDE
# install is the shortest path to a working flash chain.
ST_DOWNLOAD = "https://www.st.com/en/development-tools/stm32cubeide.html"
ARM_WINGET = "winget install -e --id Arm.GnuArmEmbeddedToolchain"

results = []


def add(cid, enables, severity, ok, detail, remedy=""):
    results.append({"id": cid, "enables": enables, "severity": severity,
                    "ok": bool(ok), "detail": detail, "remedy": remedy if not ok else ""})
    return ok


# ----------------------------------------------------------------- probes
def probe_python():
    v = sys.version_info
    add("python.version", RUN, BLOCKER, v >= (3, 10),
        f"Python {v.major}.{v.minor}.{v.micro}",
        "Install Python 3.10 or newer:  winget install -e --id Python.Python.3.12")
    add("python.bits", RUN, DEGRADED, sys.maxsize > 2 ** 32,
        "64-bit" if sys.maxsize > 2 ** 32 else "32-bit",
        "Install 64-bit Python; some wheels have no 32-bit build.")


def probe_packages():
    need = [("PyQt5", "PyQt5"), ("serial", "pyserial"), ("numpy", "numpy"),
            ("pandas", "pandas"), ("openpyxl", "openpyxl"), ("yaml", "PyYAML")]
    optional = [("scipy", "scipy"), ("PIL", "Pillow")]
    missing = []
    for mod, pkg in need:
        try:
            __import__(mod)
        except Exception:
            missing.append(pkg)
    add("packages.required", RUN, BLOCKER, not missing,
        "all present" if not missing else "missing: " + ", ".join(missing),
        f'{sys.executable} -m pip install -r app\\requirements.txt')
    miss_opt = []
    for mod, pkg in optional:
        try:
            __import__(mod)
        except Exception:
            miss_opt.append(pkg)
    add("packages.optional", RUN, DEGRADED, not miss_opt,
        "all present" if not miss_opt else "missing: " + ", ".join(miss_opt),
        f'{sys.executable} -m pip install ' + " ".join(miss_opt))


def probe_qt():
    """Build a real widget. Importing PyQt5 succeeds in places a window cannot open."""
    code = ("import os,sys\n"
            "from PyQt5 import QtWidgets\n"
            "a=QtWidgets.QApplication([])\n"
            "w=QtWidgets.QWidget(); w.resize(80,40)\n"
            "l=QtWidgets.QLabel('x', w)\n"
            "print('QT_OK')\n")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=60)
        ok = "QT_OK" in r.stdout
        err = (r.stderr or "").strip().splitlines()
        add("qt.widget", RUN, BLOCKER, ok,
            "Qt can create windows" if ok else (err[-1] if err else "failed"),
            "Install the Microsoft Visual C++ Redistributable (x64), then reinstall PyQt5:\n"
            "        winget install -e --id Microsoft.VCRedist.2015+.x64")
    except Exception as e:
        add("qt.widget", RUN, BLOCKER, False, f"{type(e).__name__}: {e}",
            "Reinstall PyQt5:  pip install --force-reinstall PyQt5")


def probe_writable():
    d = os.path.join(ROOT, "data")
    try:
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ".doctor_probe")
        with open(p, "w") as f:
            f.write("x")
        os.remove(p)
        ok, detail = True, d
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    add("disk.writable", RUN, BLOCKER, ok, detail,
        "Move the BLDC_Motor_Bench folder somewhere writable: your Desktop or\n"
        "        Documents. Do NOT run it from Program Files or a read-only share.")
    onedrive = "onedrive" in ROOT.lower()
    add("disk.location", RUN, DEGRADED, not onedrive,
        ROOT if not onedrive else "inside OneDrive (sync can lock files mid-run)",
        "Move the folder outside OneDrive so sync cannot lock a CSV while recording.")


def probe_serial():
    try:
        from serial.tools import list_ports
    except Exception as e:
        return add("serial.module", LINK, BLOCKER, False, str(e),
                   f'{sys.executable} -m pip install pyserial')
    ports = list(list_ports.comports())
    add("serial.enumerate", LINK, BLOCKER, True, f"{len(ports)} COM port(s)")
    stlink = [p for p in ports if (p.vid == 0x0483)
              or "st-link" in (p.description or "").lower()
              or "stlink" in (p.description or "").lower()]
    add("serial.stlink", LINK, DEGRADED, bool(stlink),
        stlink[0].device + "  " + (stlink[0].description or "") if stlink
        else "no ST-LINK Virtual COM Port found",
        "Plug the NUCLEO-F401RE into USB (the ST-LINK end, not the user USB).\n"
        "        Windows 11 binds the VCP automatically, no ST driver download needed.\n"
        "        If it still does not appear, try another cable: many USB cables are\n"
        "        charge-only and carry no data lines.")
    return True


def _find(patterns):
    for pat in patterns:
        hits = sorted(glob.glob(pat, recursive=True), reverse=True)
        if hits:
            return hits[0]
    return None


def probe_gcc():
    gcc = None
    try:
        from bldc_phm.stm32tools import STM32Tools
        import yaml
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bldc_phm", "config", "app_config.yaml"),
                                  encoding="utf-8"))
        t = STM32Tools(cfg.get("tools"), cfg.get("firmware_elf"))
        gcc = getattr(t, "paths", {}).get("gcc") or getattr(t, "paths", {}).get("arm_gcc")
    except Exception:
        pass
    if not gcc:
        gcc = _find([
            r"C:\ST\STM32CubeIDE*\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32*\tools\bin\arm-none-eabi-gcc.exe",
            r"C:\Program Files*\STMicroelectronics\**\arm-none-eabi-gcc.exe",
            r"C:\Program Files*\Arm GNU Toolchain*\**\arm-none-eabi-gcc.exe",
            r"C:\Program Files (x86)\Arm GNU Toolchain*\**\arm-none-eabi-gcc.exe",
        ])
    if not gcc:
        import shutil as _sh
        gcc = _sh.which("arm-none-eabi-gcc")
    if not gcc:
        return add("gcc.present", BUILD, BLOCKER, False, "arm-none-eabi-gcc not found",
                   f"Either install STM32CubeIDE (it bundles the compiler AND the\n"
                   f"        programmer: one install covers building and flashing):\n"
                   f"          {ST_DOWNLOAD}\n"
                   f"        or install just the compiler:\n"
                   f"          {ARM_WINGET}")
    # it exists: but can it actually compile?
    try:
        d = tempfile.mkdtemp(prefix="bldc_gcc_")
        src = os.path.join(d, "t.c")
        with open(src, "w") as f:
            f.write("int f(int x){return x+1;}\n")
        r = subprocess.run([gcc, "-mcpu=cortex-m4", "-mthumb", "-c", src,
                            "-o", os.path.join(d, "t.o")],
                           capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0 and os.path.exists(os.path.join(d, "t.o"))
        detail = os.path.basename(gcc) + (" compiles for Cortex-M4" if ok
                                          else " FOUND BUT CANNOT COMPILE")
        add("gcc.compiles", BUILD, BLOCKER, ok, detail,
            "The compiler is present but broken, reinstall STM32CubeIDE, or install\n"
            f"        the standalone toolchain:  {ARM_WINGET}")
        add("gcc.path", BUILD, DEGRADED, True, gcc)
        import shutil as _sh2
        _sh2.rmtree(d, ignore_errors=True)
    except Exception as e:
        add("gcc.compiles", BUILD, BLOCKER, False, f"{type(e).__name__}: {e}",
            f"Reinstall the toolchain:  {ARM_WINGET}")


def probe_programmer():
    cli = None
    try:
        from bldc_phm.stm32tools import STM32Tools
        import yaml
        cfg = yaml.safe_load(open(os.path.join(ROOT, "bldc_phm", "config", "app_config.yaml"),
                                  encoding="utf-8"))
        t = STM32Tools(cfg.get("tools"), cfg.get("firmware_elf"))
        cli, _info = t.flash_command()
        if isinstance(cli, (list, tuple)):
            cli = cli[0] if cli else None
    except Exception:
        pass
    if not cli:
        cli = _find([
            r"C:\ST\STM32CubeIDE*\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer*\tools\bin\STM32_Programmer_CLI.exe",
            r"C:\Program Files*\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
        ])
    if not cli or not os.path.exists(str(cli)):
        return add("programmer.present", FLASH, BLOCKER, False,
                   "STM32_Programmer_CLI.exe not found",
                   f"Install STM32CubeIDE: it bundles the programmer CLI and the\n"
                   f"        compiler together, so this one install fixes both:\n"
                   f"          {ST_DOWNLOAD}\n"
                   f"        (ST requires a free account. There is no winget package --\n"
                   f"         verified. This step cannot be automated.)")
    try:
        r = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
        ok = "STM32" in out or r.returncode == 0
        # the CLI leads with an ASCII banner; find the line that actually names a version
        ver = next((l.strip() for l in out.splitlines()
                    if l.strip() and not set(l.strip()) <= set("-=_*# ")
                    and any(c.isdigit() for c in l)), "responded")
        add("programmer.runs", FLASH, BLOCKER, ok, ver[:70],
            "The programmer CLI is present but did not run. Reinstall STM32CubeIDE.")
        add("programmer.path", FLASH, DEGRADED, True, cli)
    except Exception as e:
        add("programmer.runs", FLASH, BLOCKER, False, f"{type(e).__name__}: {e}",
            f"Reinstall STM32CubeIDE:  {ST_DOWNLOAD}")


def probe_firmware_writable():
    d = os.path.join(ROOT, "firmware")
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".doctor_probe")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        ok, detail = True, "firmware/ is writable"
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    # The build emits speeds_gen.h and motor.elf straight into firmware/. A %TEMP% probe
    # would pass while the real build fails, Controlled Folder Access guards the Desktop
    # and Documents, not %TEMP%.
    add("firmware.writable", BUILD, BLOCKER, ok, detail,
        "Windows is blocking writes to the firmware folder. Either move the\n"
        "        BLDC_Motor_Bench folder out of a protected location, or allow it in\n"
        "        Windows Security > Virus & threat protection > Ransomware protection >\n"
        "        Controlled folder access > Allow an app through.")


def probe_project():
    fw = os.path.join(ROOT, "firmware", "main.c")
    add("project.firmware", BUILD, BLOCKER, os.path.exists(fw),
        "firmware/main.c present" if os.path.exists(fw) else "firmware/main.c MISSING",
        "Re-extract the package; the firmware source did not come across.")
    cfgp = os.path.join(ROOT, "bldc_phm", "config", "app_config.yaml")
    add("project.config", RUN, BLOCKER, os.path.exists(cfgp),
        "app_config.yaml present" if os.path.exists(cfgp) else "app_config.yaml MISSING",
        "Re-extract the package.")


# ----------------------------------------------------------------- report
def verdict():
    """Three independent answers. They must not be chained: an unplugged board is not a
    reason to say the app will not start, and a missing compiler is not either."""
    def blocked(kind):
        return [r for r in results
                if r["enables"] == kind and r["severity"] == BLOCKER and not r["ok"]]
    can_run = not blocked(RUN)
    can_flash = not (blocked(BUILD) or blocked(FLASH))
    # "is the board there" is the stlink row specifically, serial.enumerate succeeds
    # on any machine with a COM port at all, including a Bluetooth one.
    st = next((r for r in results if r["id"] == "serial.stlink"), None)
    can_link = (not blocked(LINK)) and bool(st and st["ok"])
    return can_run, can_flash, can_link


def _redact(obj):
    """Strip machine-identifying paths out of anything bound for a committed file."""
    home = os.path.expanduser("~")
    subs = []
    for real, token in ((ROOT, "<repo>"), (home, "<home>")):
        if not real:
            continue
        subs.append((real, token))
        subs.append((real.replace("\\", "/"), token))
    # Longest first, so <repo> wins over <home> when the repo lives under home.
    subs.sort(key=lambda pair: len(pair[0]), reverse=True)

    if isinstance(obj, str):
        out = obj
        for real, token in subs:
            out = re.sub(re.escape(real), token, out, flags=re.IGNORECASE)
        return out
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="machine-readable output only")
    args = ap.parse_args()

    probe_python(); probe_packages(); probe_qt(); probe_writable()
    probe_project(); probe_firmware_writable()
    probe_serial(); probe_gcc(); probe_programmer()
    can_run, can_flash, can_link = verdict()

    payload = {"can_run": can_run, "can_flash": can_flash, "can_link": can_link,
               "python": sys.version.split()[0], "root": ROOT, "checks": results}
    try:
        os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
        with open(os.path.join(ROOT, "data", "setup_status.json"), "w",
                  encoding="utf-8") as f:
            # data/setup_status.json is committed, so it must never carry this
            # machine's absolute paths. Redacting by hand does not survive the
            # next `python tools/doctor.py`; redact here instead.
            json.dump(_redact(payload), f, indent=2)
    except Exception:
        pass

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0 if can_run else 1

    W = 68
    print("=" * W)
    print("  BLDC Motor Bench  -  setup check")
    print("=" * W)
    order = [RUN, LINK, BUILD, FLASH]
    titles = {RUN: "RUNNING THE APP", LINK: "TALKING TO THE BOARD",
              BUILD: "BUILDING FIRMWARE", FLASH: "FLASHING FIRMWARE"}
    for kind in order:
        rows = [r for r in results if r["enables"] == kind]
        if not rows:
            continue
        print(f"\n{titles[kind]}")
        for r in rows:
            mark = "  ok  " if r["ok"] else (", " if r["severity"] == DEGRADED
                                             else " FAIL ")
            print(f" [{mark}] {r['id']:22} {r['detail']}")

    fails = [r for r in results if not r["ok"] and r["severity"] == BLOCKER]
    warns = [r for r in results if not r["ok"] and r["severity"] == DEGRADED]
    if fails:
        print("\n" + "-" * W + "\nWHAT TO DO\n" + "-" * W)
        for r in fails:
            print(f"\n  {r['id']}  -  {r['detail']}")
            for line in r["remedy"].splitlines():
                print("        " + line.strip() if not line.startswith("  ") else "     " + line)
    if warns:
        print("\n" + "-" * W + "\nWORTH KNOWING (not blocking)\n" + "-" * W)
        for r in warns:
            print(f"  {r['id']}: {r['detail']}")
            if r["remedy"]:
                print("        " + r["remedy"].splitlines()[0].strip())

    print("\n" + "=" * W)
    print(f"  CAN YOU RUN THE APP?     {'YES' if can_run else 'NO'}")
    print(f"  CAN YOU FLASH FIRMWARE?  {'YES' if can_flash else 'NO'}")
    print(f"  IS THE BOARD PLUGGED IN? {'YES' if can_link else 'not right now'}")
    print("=" * W)
    if can_run and can_flash:
        print("  Everything is ready. Launch with run_bench.bat.")
    elif can_run:
        print("  You can record data. You cannot build or flash firmware yet --")
        print("  see WHAT TO DO above. If the board is already flashed, that is fine:")
        print("  flashing is a one-time step per board.")
    else:
        print("  The app will not start yet. Fix the FAIL items above, then re-run:")
        print("      py -3 tools\\doctor.py")
    print()
    return 0 if can_run else 1


if __name__ == "__main__":
    sys.exit(main())
