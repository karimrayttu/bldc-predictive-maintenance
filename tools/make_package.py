"""Build a distributable ZIP of the bench app for another laptop."""
import os
import sys
import zipfile
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Directories never included, at any depth.
SKIP_DIRS = {"_archive", "__pycache__", ".git", "build", "dist", ".idea", ".vscode"}
# Extensions never included.
SKIP_EXT = {".bak", ".pyc", ".pyo", ".log", ".xlsx", ".zip"}
# Individual files never included.
# interpreter.txt pins THIS machine's python; setup_status.json is a local result
SKIP_FILES = {".wtest", "interpreter.txt", "setup_status.json"}


def wanted(rel):
    parts = rel.replace("\\", "/").split("/")
    if any(p in SKIP_DIRS for p in parts):
        return False
    name = parts[-1]
    if name in SKIP_FILES or os.path.splitext(name)[1].lower() in SKIP_EXT:
        return False
    # data/: ship the folder structure and its README, never anyone's recordings
    if parts[0] == "data":
        return name.lower() == "readme.md"
    return True


def main():
    stamp = datetime.date.today().strftime("%Y%m%d")
    out = os.path.join(os.path.expanduser("~"), "Desktop",
                       f"BLDC_Motor_Bench_{stamp}.zip")
    n = 0
    total = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for f in files:
                full = os.path.join(base, f)
                rel = os.path.relpath(full, ROOT)
                if not wanted(rel):
                    continue
                z.write(full, os.path.join("BLDC_Motor_Bench", rel))
                n += 1
                total += os.path.getsize(full)
        # keep the data tree present but empty on the target machine
        for d in ("data/mounted_baseline/sessions", "data/_live"):
            z.writestr(f"BLDC_Motor_Bench/{d}/.keep", "")

    print(f"  {n} files, {total / 1e6:.1f} MB raw -> {os.path.getsize(out) / 1e6:.1f} MB zipped")
    print(f"  {out}")
    return out


if __name__ == "__main__":
    main()
