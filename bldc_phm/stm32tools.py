"""STM32 tooling integration: the app drives the installed ST toolchain instead of
bundling its own copy.
"""

import os
import glob
import subprocess

# Candidate locations (first existing wins). Globs keep this version-independent.
CANDIDATES = {
    "cubemonitor": [
        os.path.expandvars(r"%LOCALAPPDATA%\STM32CubeMonitor\STM32CubeMonitor.exe"),
        r"C:\Program Files\STMicroelectronics\STM32CubeMonitor\STM32CubeMonitor.exe",
    ],
    "cubeide": [
        r"C:\ST\STM32CubeIDE_*\STM32CubeIDE\stm32cubeide.exe",
        r"C:\Program Files\STMicroelectronics\STM32CubeIDE\stm32cubeide.exe",
    ],
    "cubeprogrammer": [
        r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32CubeProgrammer.exe",
    ],
    "programmer_cli": [
        # the one the project's flash .bat uses (bundled in CubeIDE), preferred
        r"C:\ST\STM32CubeIDE_*\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.cubeprogrammer.win32_*\tools\bin\STM32_Programmer_CLI.exe",
        r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
        r"C:\ST\STM32CubeCLT_*\STM32CubeProgrammer\bin\STM32_Programmer_CLI.exe",
    ],
    "gcc": [
        # the arm-none-eabi-gcc the project's 1_build.bat uses (bundled in CubeIDE)
        r"C:\ST\STM32CubeIDE_*\STM32CubeIDE\plugins\com.st.stm32cube.ide.mcu.externaltools.gnu-tools-for-stm32.*\tools\bin\arm-none-eabi-gcc.exe",
        r"C:\ST\STM32CubeCLT_*\GNU-tools-for-STM32\bin\arm-none-eabi-gcc.exe",
    ],
}


def _first_existing(patterns):
    for pat in patterns:
        for hit in sorted(glob.glob(pat), reverse=True):   # prefer newest version
            if os.path.exists(hit):
                return hit
    return None


class STM32Tools:
    def __init__(self, overrides=None, firmware_elf=None):
        self.overrides = overrides or {}
        self.firmware_elf = firmware_elf
        self.paths = {k: self.resolve(k) for k in CANDIDATES}

    def resolve(self, key):
        ov = self.overrides.get(key)
        if ov and os.path.exists(ov):
            return ov
        return _first_existing(CANDIDATES.get(key, []))

    def available(self, key):
        return bool(self.paths.get(key))

    def open(self, key):
        """Launch a GUI tool detached. Returns (ok, message)."""
        path = self.paths.get(key)
        if not path:
            return False, f"{key} not found: set its path in app_config.yaml > tools"
        try:
            subprocess.Popen([path], close_fds=True)
            return True, f"launched {os.path.basename(path)}"
        except Exception as exc:
            return False, str(exc)

    def flash_command(self):
        """Return (exe, args) for the proven flash, or (None, reason)."""
        cli = self.paths.get("programmer_cli")
        if not cli:
            return None, "STM32_Programmer_CLI not found"
        elf = self.firmware_elf
        if not elf or not os.path.exists(elf):
            return None, f"firmware ELF not found: {elf}"
        # proven command from the project's 2_flash.bat
        return cli, ["-c", "port=SWD", "mode=UR", "-w", elf, "-rst"]
