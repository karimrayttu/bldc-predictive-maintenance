# KX134 accelerometer: setup and calibration

I²C bring-up of a SparkFun KX13X breakout on an ST NUCLEO-F401RE, July 2026.

## Hardware

- **MCU**: ST NUCLEO-F401RE (STM32F401RET6, Cortex-M4F). Ran on the internal 16 MHz HSI
  for this test.
- **Sensor**: SparkFun KX13X breakout, identified as a **KX134** (`WHO_AM_I` = 0x46).
  Default range ±8 g → 4096 LSB/g.
- **Bus**: I²C1 at 100 kHz standard mode, 3.3 V logic.

## Wiring (final, working)

| KX134 pin | Nucleo pin | STM32 pin | Function |
|---|---|---|---|
| 3V3 | 3V3 (CN6-4) | n/a | Power |
| GND | GND | n/a | Ground |
| SDA | D14 | PB9 | I2C1_SDA |
| SCL | D15 | PB8 | I2C1_SCL |

**Key fix.** SDA/SCL were first wired to A4/A5 and the bus was dead. On a Nucleo, A4/A5 are
PC1/PC0, which are ADC inputs, not the I²C bus. (SDA/SCL on A4/A5 is an Arduino-Uno-only alias.) I2C1
physically lives on D14/D15 = PB9/PB8. Moving the two signal wires there fixed it.

## Firmware and live output

- **Firmware**: single-file bare-metal C, no HAL and no libc. Initialises I²C1 and USART2,
  scans the bus, reads `WHO_AM_I`, then streams X/Y/Z in milli-g.
- **Build and flash (headless, no IDE GUI)**: compiled with the STM32CubeCLT
  `arm-none-eabi-gcc`; flashed with `STM32_Programmer_CLI` over SWD.
- **Live view**: USART2 → ST-Link Virtual COM Port at 115200 8N1, read with a Python
  pyserial monitor. A self-healing probe reports each I²C line as `[OK]` / `[--]` and
  auto-starts the stream once both connect.

## Verification that the sensor is healthy

- At rest the vector magnitude |a| = √(x² + y² + z²) ≈ 1000 mg (1 g); it is genuinely
  measuring gravity.
- Per-axis noise is only 4-6 mg peak-to-peak over 5 s: stable, with no dropouts and no
  byte-tearing.
- Readings track motion; whichever axis points down reads ≈ ±1000 mg.

## Calibration against gravity

Gravity is a known 1000 mg reference. Point each axis straight up and straight down, then
per axis:

```
offset    = (up + down) / 2
scale     = 2000 / (up − down)
corrected = (raw − offset) × scale
```

- Capture each endpoint while the board is **still**. Moving it adds real acceleration
  beyond ±1 g, which corrupts the min/max; a still-gated auto-capture avoids this.
- Baseline before calibration: |a| ≈ 1054 mg, 5.4 % high and constant. A constant error is a
  gain/offset trim, not noise.
- For this project an absolute-g calibration is optional: the vibration and FFT monitoring
  uses changes and frequency content, not absolute magnitude.
