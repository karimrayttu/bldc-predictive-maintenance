"""Create (or refresh) the 'BLDC PHM' desktop shortcut, portably, for whatever Python
is running this. Called by INSTALL.bat; safe to re-run."""
import os
import sys
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.exists(pyw):
    pyw = sys.executable
desktop = os.path.join(os.path.expanduser("~"), "Desktop")
if not os.path.isdir(desktop):
    desktop = ROOT
lnk = os.path.join(desktop, "BLDC Motor Bench.lnk")
old_lnk = os.path.join(desktop, "BLDC PHM.lnk")     # remove the pre-rename shortcut if present
if os.path.exists(old_lnk):
    try:
        os.remove(old_lnk)
    except OSError:
        pass

# use the app logo for the shortcut icon if present, else fall back to the python icon
ico = os.path.join(ROOT, "app", "ui", "assets", "app_logo.ico")
icon_loc = f"{ico},0" if os.path.exists(ico) else f"{pyw},0"

ps = (
    "$w = New-Object -ComObject WScript.Shell; "
    f"$s = $w.CreateShortcut('{lnk}'); "
    f"$s.TargetPath = '{pyw}'; "
    "$s.Arguments = '-m apps.bench.main'; "
    f"$s.WorkingDirectory = '{ROOT}'; "
    f"$s.IconLocation = '{icon_loc}'; "
    "$s.Description = 'BLDC Motor Bench - Predictive Maintenance'; "
    "$s.WindowStyle = 1; $s.Save()"
)
try:
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print("Shortcut created:", lnk)
except Exception as exc:
    print("Could not create shortcut:", exc)
    print("You can still launch with run_bench.bat")
