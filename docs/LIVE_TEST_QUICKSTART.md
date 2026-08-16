# Live Testing: Bench Quickstart

Follow this the first time you power the motor with the app.

## 0. One-time install (on this PC or your laptop)
1. Copy the whole **BLDC_PHM** folder to the machine.
2. Double-click **INSTALL.bat**; installs Python packages, makes the desktop shortcut,
   and detects your STM32 tools. (Needs Python 3.10+ with "Add to PATH", and CubeIDE/
   CubeProgrammer installed for flashing.)

## 1. Wiring (power OFF): verified pins
```
24 V (+) -> driver V+        24 V (-) -> driver GND
NUCLEO  <- USB / ST-LINK
PB4 / D5 (TIM3_CH1 PWM) -> driver SV
NUCLEO GND -> driver signal GND   (REQUIRED common ground)
driver HA/HB/HC -> PA10 / PB3 / PB5   (D2 / D3 / D4 Hall taps)
EN -> driver GND (V2.0 = run)    BK, F/R -> open    R-SL pot -> up
```
Safety: bolt the motor down (even temporarily, rigid clamp/vise with protected jaws),
bare shaft, guard the shaft, have an E-stop / quick power cut. Start low speed.

## 2. Flash the firmware once (for live speed control)
1. Launch the app (BLDC PHM shortcut).
2. **STM32 tools → Flash firmware** (or "Speeds → update code / build / flash" →
   "Update + build + flash"). The app releases the serial port, builds, and flashes the
   bundled `firmware/` (now RX-enabled).
3. Watch the log for **SUCCESS**.

> Live speed (`S<rpm>`) and the Auto-sweep need this RX firmware. Without it, speed only
> changes via the board's USER button.

## 3. Connect & verify the link
1. The **Hardware** panel auto-detects the ST-LINK ("● ST-LINK detected on COMx").
   Click **Connect** (the ST-LINK port auto-detects). Or use **Build · Flash · Run** to
   build the firmware, flash it, and connect in one click.
2. Power the driver (24 V). The green LED on the board lights when running.
3. **Status / Tools tab**: confirm Connected = yes, frame rate ~10 fps, integrity ~100 %,
   and the live channels show "reading".
4. **Monitor tab**: the value cards should show a real rpm and ripple.

## 4. Fixture MAIN SWITCH: pick the right mode
- The motor is always mounted now. Taking it off the mount is the **loose_mount**
  fault condition, labelled per run; not a global mode.
  Use this for checkout only; never for the classifier.
- On the rigid baseplate? Switch to **Mounted (baseline)** for real data.

## Changing speed (two ways)
- **App buttons (easy):** the **Speed control** panel; click a preset (OFF / 500 / 800 /
  1100 / 1400 / 1700 / 2000 / 2400), **Cycle next ▶** to step through them, **−100 / +100**
  to nudge, or type a Custom value and **Set**. The motor moves instantly.
  The 2400 preset is above the 2310 rpm firmware fullscale, so it saturates: the
  command clips and the motor holds fullscale. It is there as a live R&D probe only, 
  never record a run at it, and the coverage plan stops at 2100 for this reason.
  *Requires the RX firmware flashed once (step 2).*
- **Board button:** the blue USER button cycles the 6 firmware steps (works with any
  firmware). The "Set firmware speeds" dialog just edits which speeds that button walks.

## 5. Record a run
1. **Test settings**: pick condition (healthy to start), speed, load, orientation, mount.
   (Everything else is under **Advanced**; usually leave it.)
2. Set the speed from the **Speed control** panel.
3. Set **Run length** (e.g. 30 s). Click **Start run** → the quick **pre-test check**
   appears (only "data flowing" is required) → **Confirm & record**.
4. The run records for the set time and **auto-saves**; you can't stop it mid-way.
5. To abort mid-run, use **■ STOP ALL** (top of the panel): it cuts the motor and, after a
   confirm, **discards** the in-progress run (nothing is saved). When idle, STOP ALL just
   stops the motor.

## 6. Thorough automatic test: Auto speed sweep
**Live control & auto-test → ⏱ Auto speed sweep…**
- Enter speeds (e.g. `500,800,1100,1400,1700,2000`), settle time, record time, repeats.
- Confirm calibration once. A **progress window** shows the bar and the live current test
  ("Test 3 of 12: 1500 rpm; recording 30 s"). One validated session per speed.
- Cancel any time; the matrix updates in **Plan & Coverage**.

## 7. Check coverage
**Plan & Coverage tab** → pick the dataset (mounted/commissioning) and the two axes
(e.g. speed × load). Green = all PASS, yellow = some REVIEW, · = empty. Fill the matrix.

## Limits to respect (datasheet)
- Speed ≤ **2310 rpm**: the 5 V-referenced SV input sees 3.3 V PWM, so duty
  scales x0.66. Plan speeds top out at 2100.
- Motor **1.79 A continuous, 6.0 A peak**; keep loaded high-speed runs short.
- Use coast/natural stop; avoid the BK brake pin.

## If something looks wrong
- Port busy → close STM32CubeMonitor (the app and CubeMonitor can't share COM at once;
  clicking a tool button in the app auto-releases the port).
- No data → check common ground, EN to GND, and that the driver is powered.
- Tell me the symptom + what the Status tab shows and I'll help diagnose.
