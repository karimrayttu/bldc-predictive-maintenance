"""Setup verification; confirms a machine is ready to run BLDC Motor Bench, and tells the user
exactly what's missing and how to install it.
"""
import os
import sys
import shutil
import importlib
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # ...\BLDC_PHM

# (import name, pip name) for everything the app needs
REQUIRED_PKGS = [
    ("PyQt5", "PyQt5"), ("numpy", "numpy"), ("pandas", "pandas"),
    ("openpyxl", "openpyxl"), ("scipy", "scipy"), ("serial", "pyserial"),
    ("yaml", "PyYAML"), ("PIL", "Pillow"),
]

# Where to get the supporting ST apps (CubeIDE bundles gcc + Programmer CLI + ST-LINK driver)
ST_DOWNLOADS = {
    "STM32CubeIDE  (gives arm-none-eabi-gcc + Programmer + ST-LINK driver)":
        "https://www.st.com/en/development-tools/stm32cubeide.html",
    "STM32CubeProgrammer  (flashing)":
        "https://www.st.com/en/development-tools/stm32cubeprog.html",
    "STM32CubeMonitor  (optional live viewer)":
        "https://www.st.com/en/development-tools/stm32cubemonitor.html",
}


def _have(import_name):
    try:
        importlib.import_module(import_name)
        return True
    except Exception:
        return False


def check_python():
    v = sys.version_info
    return ("Python ≥ 3.10", v >= (3, 10), f"{v.major}.{v.minor}.{v.micro}")


def check_packages():
    out = []
    for imp, pip in REQUIRED_PKGS:
        if _have(imp):
            mod = importlib.import_module(imp)
            out.append((pip, True, getattr(mod, "__version__", "ok")))
        else:
            out.append((pip, False, "MISSING: pip install"))
    return out


def missing_packages():
    return [pip for imp, pip in REQUIRED_PKGS if not _have(imp)]


def check_tools(tools):
    """tools: an STM32Tools instance (its .paths dict). Returns (name, ok, detail) rows."""
    rows = []
    spec = [("gcc", "arm-none-eabi-gcc  (build firmware)"),
            ("programmer_cli", "STM32_Programmer_CLI  (flash)"),
            ("cubeprogrammer", "STM32CubeProgrammer GUI  (optional)"),
            ("cubeide", "STM32CubeIDE  (optional)"),
            ("cubemonitor", "STM32CubeMonitor  (optional)")]
    for key, lab in spec:
        p = tools.paths.get(key) if tools else None
        rows.append((lab, bool(p), p or "not found"))
    return rows


def check_serial():
    try:
        from bldc_phm.sources import list_serial_ports
        ports = list_serial_ports()
        stlink = [d for d, _desc, is_st in ports if is_st]
        if stlink:
            return ("ST-LINK serial port", True, ", ".join(stlink))
        return ("ST-LINK serial port", False,
                f"{len(ports)} COM port(s), no ST-LINK (plug in the NUCLEO)" if ports else "no COM ports")
    except Exception as exc:
        return ("ST-LINK serial port", False, str(exc))


def check_project(cfg=None):
    fw_dir = (cfg or {}).get("firmware_dir") or os.path.join(ROOT, "firmware")
    fw_ok = all(os.path.exists(os.path.join(fw_dir, n)) for n in ("main.c", "STM32F401RE.ld"))
    elf_ok = os.path.exists((cfg or {}).get("firmware_elf") or os.path.join(fw_dir, "motor.elf"))
    return [("Firmware sources", fw_ok, "main.c + linker present" if fw_ok else "missing"),
            ("Compiled motor.elf", elf_ok, "present" if elf_ok else "not built yet (use Build·Flash·Run)")]


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _writable(name, path):
    try:
        os.makedirs(path, exist_ok=True)
        t = os.path.join(path, ".wtest"); open(t, "w").close(); os.remove(t)
        return (name, True, "writable")
    except Exception as exc:
        return (name, False, str(exc))


def check_permissions(cfg=None):
    """Confirm the app has the AUTHORITY it needs to RUN (none of this requires admin)."""
    data_dir = (cfg or {}).get("data_dir") or os.path.join(ROOT, "data")
    rows = [_writable("Data folder writable (records data)", data_dir),
            _writable("App folder writable (logo / shortcut)", ROOT)]
    try:
        subprocess.run([sys.executable, "-c", "pass"], capture_output=True, timeout=15)
        rows.append(("Can launch helper processes (pip / flash / tools)", True, "ok"))
    except Exception as exc:
        rows.append(("Can launch helper processes (pip / flash / tools)", False, str(exc)))
    rows.append(("Administrator rights", _is_admin(),
                 "elevated" if _is_admin() else "normal user (NOT needed to run; only the installer/driver needs it)"))
    return rows


def has_winget():
    return shutil.which("winget") is not None


def pip_install(pkgs):
    """Install/upgrade the given pip packages. Returns CompletedProcess."""
    return subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", *pkgs],
                          capture_output=True, text=True)


def run_all(cfg=None, tools=None):
    """Full report as an ordered dict of section -> [(name, ok, detail), ...]."""
    if tools is None:
        try:
            from bldc_phm.stm32tools import STM32Tools
            tools = STM32Tools()
        except Exception:
            tools = None
    return {
        "Python & packages": [check_python()] + check_packages(),
        "Permissions & authority": check_permissions(cfg),
        "STM32 toolchain": check_tools(tools),
        "Hardware link": [check_serial()],
        "Project files": check_project(cfg),
    }


def _light_cfg():
    return {"firmware_dir": os.path.join(ROOT, "firmware"),
            "firmware_elf": os.path.join(ROOT, "firmware", "motor.elf"),
            "data_dir": os.path.join(ROOT, "data")}


def main():
    # Windows may launch this verifier with a legacy console code page. Do not let
    # a diagnostic symbol in a package/version string crash the entire report.
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, OSError):
        pass
    rep = run_all(_light_cfg())
    print("=" * 58)
    print("  BLDC Motor Bench, setup verification")
    print("=" * 58)
    all_ok = True
    for section, rows in rep.items():
        print(f"\n[{section}]")
        for name, ok, det in rows:
            mark = "OK  " if ok else "--  "
            print(f"  {mark}{name}: {det}")
            if not ok and section in ("Python & packages",):
                all_ok = False
    miss = missing_packages()
    if miss:
        print("\nInstall missing Python packages with:")
        print(f"  \"{sys.executable}\" -m pip install -r requirements.txt")
    print("\nMissing STM32 tools? Install STM32CubeIDE (it bundles gcc + Programmer + driver):")
    for lab, url in ST_DOWNLOADS.items():
        print(f"  {lab}\n     {url}")
    print("\nCore status:", "READY" if all_ok else "MISSING PACKAGES (see above)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
