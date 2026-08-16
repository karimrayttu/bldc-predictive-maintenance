# PINOUT: NUCLEO-F401RE (MB1136)

**STM32F401RET6 · LQFP64 · HSI 16 MHz (no PLL configured)**

`bldc_phm/board_f401re.py` is the single source of truth for this map; this page is
a hand-maintained transcription of it. There is no generator: if you change a pin,
change `board_f401re.py` and then edit this table to match. `tools/e2e_check.py`
checks that the two agree on the SV pin, and the Pinout page in the app shows a
MISMATCH badge whenever the saved map and the compiled firmware disagree.

| Signal | GPIO | Arduino | Function | Notes |
|---|---|---|---|---|
| Hall A | **PA10** | D2 | EXTI10 | Motor Hall sensor A. Needs a unique EXTI line. |
| Hall B | **PB3** | D3 | EXTI3 | Motor Hall sensor B. Needs a unique EXTI line. |
| Hall C | **PB5** | D4 | EXTI5 | Motor Hall sensor C. Needs a unique EXTI line. |
| SV speed command | **PB4** | D5 | TIM3_CH1 | 1 kHz PWM to the BLD-510B SV input. Needs a timer channel. |
| Driver EN | **PA8** | D7 | GPIO (open-drain) | Sunk to COM = enabled; released = output stage off. Replaces the EN-COM strap that left ~10 mA parked bias in hall sectors 2/4. Wired 2026-07-30. |
| Current sense | **PA0** | A0 | ADC1_IN0 | INA240A2 output. Needs an ADC1 channel. |
| Motor temp (NTC) | **PA1** | A1 | ADC1_IN1 | 10k NTC divider node. Needs an ADC1 channel. |
| KX134 SDA | **PB9** | D14 | n/a | Bit-bang I2C data. Any GPIO (open-drain in firmware). |
| KX134 SCL | **PB8** | D15 | n/a | Bit-bang I2C clock. Any GPIO (open-drain in firmware). |
| KX134 INT1 | **PA9** | D8 | n/a | Data-ready interrupt. Currently parked and unused. |

## Why the Halls are on EXTI 10 / 3 / 5

STM32 selects the EXTI line by **pin number, not port**. Two Hall pins sharing a number
(PA10 and PB10, say) would fight over one interrupt line and one channel would go silent;
the board would still boot and stream, just with two of three Halls. 10 / 3 / 5 are
distinct, which is what lets the firmware observe all six Hall codes and prove every line
is alive. The Pinout page enforces this.

## Never reassign

| Pin | Why |
|---|---|
| PA2 | USART2_TX to the ST-LINK virtual COM port; the telemetry link |
| PA3 | USART2_RX from the ST-LINK virtual COM port; the telemetry link |
| PA13 | SWDIO; reassigning loses debug/flash access to the board |
| PA14 | SWCLK; reassigning loses debug/flash access to the board |
| PH0 | OSC_IN, 8 MHz clock in from ST-LINK MCO |
| PH1 | OSC_OUT, clock |

## Speed ceiling

The BLD-510B SV input is 5 V-referenced; the board drives 3.3 V PWM, so duty scales
x0.66 and firmware fullscale is **2310 rpm**. Anything commanded above that is
unreachable. 2310 rpm is the highest setpoint anywhere in the recorded data (four
commissioning runs, measured means 2267 to 2299 rpm); plan speeds stop at 2100.

## Closed gap: per-axis gravity

The KX134 used to stream only a vibration magnitude, and gravity's magnitude is
orientation-invariant, so a rotated motor was undetectable. The firmware now streams
`accel_x_mg / accel_y_mg / accel_z_mg` as part of the 44-column frame
(`firmware/main.c`, `bldc_phm/schema.py`).

That change landed part-way through the mounted campaign, which is why only 30 of the
60 recorded runs carry per-axis data, and why the gravity-derived features leak the
label. See "Why there are two numbers" in the top-level README before training on them.
