# BLDC Motor Bench: install on a new machine

End-to-end setup for a laptop or any Windows PC. The app **runs with just Python**; the
STM32 tools are only needed to **build/flash firmware**, and the **board** is only needed to
**record** data. The app tells you what's missing at every step.

## 1. Copy the folder
Copy the whole **`BLDC_PHM`** folder to the new machine (e.g. the Desktop). It's fully
self-contained; no absolute paths, the firmware, configs, and logo are all inside it.

## 2. One-click install
Double-click **`INSTALL.bat`** and click **Yes** on the UAC prompt. That's it; it:
1. **Self-elevates** (so it has authority to install packages, drivers, and the shortcut).
2. **Finds Python 3**, and if it's missing, installs it automatically via `winget`
   (falls back to a clear python.org message if winget isn't available).
3. `pip install`s everything in `requirements.txt` (with a per-user `--user` retry if needed).
4. **Verifies the whole setup** (Python, packages, **permissions/authority**, STM32 tools,
   ST-LINK port, files) and prints exactly what's present / missing.
5. Creates the **"BLDC Motor Bench"** desktop shortcut (with the app logo).

It **reports any problem instead of failing silently**; read the colored lines; anything
marked `[X]` or `--` tells you what to fix. No installer can guarantee a flawless machine,
but this one tells you precisely what's wrong and how to finish it.

## 3. Launch & verify
Open **BLDC Motor Bench** (desktop shortcut) or `run_bench.bat`.
Go to the **Connection** tab → **Verify setup**. It re-checks everything and offers:
- **Install / repair Python packages**: re-runs the pip install in-app.
- **Get STM32 tools**: opens the STM32CubeIDE download (bundles `arm-none-eabi-gcc`,
  the Programmer CLI, and the ST-LINK driver; that's all you need for build + flash).

## 4. What each capability needs
| To do this | You need |
|---|---|
| Run the app, browse data, build workbooks | **Python + packages only** (step 2) |
| Build / flash firmware (Build · Flash · Run) | **STM32CubeIDE** (gives gcc + Programmer CLI + ST-LINK driver) |
| Record live data | NUCLEO plugged in (**ST-LINK** COM port) + firmware flashed once |

## 5. First real session
1. Plug in the NUCLEO (ST-LINK), open the app.
2. **Build · Flash · Run** once (needs CubeIDE) to load the firmware.
3. **Calibration check** (new day); confirms rpm/ripple/Hall are real, not false readings.
4. **Coverage** tab → click a category cell, or **▶▶ Run ALL remaining**, to fill the plan.
   Each official run auto-appends to **`UNMOUNTED.xlsx`** / **`MOUNTED.xlsx`**.
5. Manual runs (START TEST, coast-down, sweep) are **R&D** → kept in `data/rd/`, never in the
   official Excels. Use **Verify data authenticity** anytime to confirm the official data is real.

## Data layout
```
data/
  UNMOUNTED.xlsx / MOUNTED.xlsx   ← official deliverables (1 sheet per category, sorted by rpm)
  commissioning_unmounted/sessions/ , mounted_baseline/sessions/   ← raw per-run backup
  rd/                              ← R&D / debugging runs only
```
