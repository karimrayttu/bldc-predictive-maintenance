# BLDC Motor Bench: Team Quick Start

The bench app for the NUCLEO-F401RE / BLD-510B rig.

**This repository ships source, not a binary.** `BLDCMotorBench.spec` is the PyInstaller
recipe for the single-file Windows build; the resulting `BLDCMotorBench.exe` is not
committed here. Either build it yourself (`pyinstaller BLDCMotorBench.spec`) or run the
app from source, which is what `INSTALL.bat` sets up:

```
pip install -r requirements.txt
python -m apps.bench.main
```

## Start

1. Connect the NUCLEO-F401RE through its ST-LINK USB connector.
2. Launch the app.
3. Click **Connect**. The Hardware box shows **Streaming - COMx** once frames arrive.

There is no simulator to fall back to. `SerialSource` is the only source and it is gated
behind ST-LINK detection, so an unplugged board gives a *Disconnected* sidebar and a
5-second retry rather than invented rows. If you do not see a COM port, the board is not
enumerating. Fix that before anything else.

For firmware build/flash buttons, install STM32CubeCLT (or STM32CubeIDE) and
STM32CubeProgrammer. Monitoring an already-flashed board only needs the ST-LINK USB/VCP
driver.

## Current sensor calibration

**Do not restate these constants anywhere.** `bldc_phm/calibration.py` is the single
definition and `tools/check_calibration.py` fails the build when a copy reappears, this
page used to carry a superseded set (250 mV/A, a 1101.5 mV nominal offset) and that is
exactly the drift the check exists to catch.

- Input: PA0 / Arduino A0 / **ADC1_IN0** (PA1 / A1 is ADC1_IN1, the NTC)
- Amplifier: INA240A2 (50 V/V)
- Shunt: 0.005 ohm
- Scale: `MV_PER_AMP` = 263.5 mV/A, anchored against a series ammeter on 2026-07-30
- Idle offset: `IDLE_OFFSET_A` = 0.053 A
- Conversion: `bldc_phm.calibration.amps_from_mv(i_dc_mv, zero_mv)`

`zero_mv` is a **learned** anchor, not a nominal midpoint, and it has no default on
purpose: passing a nominal midpoint instead silently biases every reading. The bench app
re-learns it from the live stream whenever the motor is commanded off and settled (3 s
past coast-down, 8 consecutive flat frames, normalised for which Hall sector the rotor
parked in) and stamps the value it used into every row as `i_dc_zero_mv`. Until the first
anchor completes the app publishes no amps at all.

The current card always shows the raw PA0 millivolt reading alongside the amps, so you can
always see what the conversion was handed. Current calibration is still provisional; see
"Known issues" in the top-level README.

## Wiring

- SV speed command: PB4 / D5 / TIM3_CH1 PWM
- Hall HA: PA10 / D2
- Hall HB: PB3 / D3
- Hall HC: PB5 / D4
- Current sensor output: PA0 / A0
- Motor NTC divider node: PA1 / A1
- All signal grounds: common NUCLEO / sensor / BLD-510B ground

Do not connect 24 V directly to any NUCLEO input. Only the conditioned INA240 output goes
to PA0. The full map, including the pins you must never reassign, is in `docs/PINOUT.md`.

## Layout

- `apps/bench/`: the desktop application
- `bldc_phm/config/`: shared editable hardware, taxonomy, and run-plan configuration
- `firmware/`: STM32F401RET6 source, linker script, Makefile, and a flashable ELF
- `docs/`: wiring, pinout and bench documentation
- `data/`: recorded runs; the two campaigns already here are described in `data/README.md`
