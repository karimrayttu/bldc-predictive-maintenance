# Wiring & Hookups: current rig + full sensor suite (forward plan)

Hardware: NUCLEO-F401RE (MB1136) · BLD-510B driver · 42BL41.010 motor.
Power rule: **always connect/disconnect with power OFF** (driver datasheet: hot-plugging
cables damages the driver).

---

## 1. CURRENT RIG (verified, working today)

POWER
```
24 V bench (+) ──▶ driver V+          24 V (−) ──▶ driver GND
NUCLEO ◀── USB / ST-LINK (auto-detected COM port)
```
CONTROL (driver terminals)
```
EN  ──▶ driver GND      (V2.0 driver: EN-to-GND = run)   ← CONFIRM your driver version!
F/R ──▶ open            BK ──▶ open (do NOT use BK braking; coast/natural stop only)
R-SL pot ──▶ turned up
```
SPEED COMMAND
```
NUCLEO PB4 / D5 (TIM3_CH1, 1 kHz PWM) ──▶ driver SV
NUCLEO GND                                  ──▶ driver signal GND   ← REQUIRED common ground
```
HALL SENSE (tap motor Halls at the driver, high-impedance)
```
driver HA / HB / HC ──▶ NUCLEO PA10 / PB3 / PB5   (D2 / D3 / D4)
```
TELEMETRY  USART2 **PA2/PA3 → ST-LINK VCP @115200**  (this is the PC data link)
USER BUTTON  PC13 steps speed · LD2  PA5 = running

### Pins in use: do not reuse
| Pin | Function |
|---|---|
| PB4 | TIM3_CH1 PWM speed out (D5) |
| PA10, PB3, PB5 | Hall A/B/C in (D2/D3/D4) |
| PA5 | LD2 running LED |
| PC13 | user button |
| PA2, PA3 | USART2 TX/RX → ST-LINK VCP (PC telemetry/control) |
| TIM2 | internal 1 µs timebase |

---

## 2. SENSORS ON THE WAY: where each one lands

The app's CSV schema already reserves these columns; firmware + wiring activate them.

### A. RS-485 / Modbus telemetry (BLD-510B): cheapest, highest ROI
Add a **MAX485** (3.3 V TTL ↔ RS-485) module.
```
driver A+ / GND / B-  ──▶  MAX485 A / GND / B
MAX485 DI (data in)   ──▶  NUCLEO UART TX     ┐ pick a FREE uart; see warning
MAX485 RO (recv out)  ──▶  NUCLEO UART RX     ┘
MAX485 DE+RE (tied)   ──▶  NUCLEO GPIO  (high=transmit, low=receive)
```
> **Do NOT use USART2 (PA2/PA3) for this**; it is the ST-LINK VCP link. Use a
> different free UART and confirm its pins in CubeMX against the MB1136 pinout.

Gives: `modbus_rpm` ($8018H, cross-check of Hall rpm) and `modbus_fault` ($801BH), the
driver's own **datasheet-defined fault flags as free ground-truth labels**. Keep control
bit **NW=0** ($8000H) so analog SV speed control still works while you only READ. 8N1,
CRC init FFFFH, baud configurable ($8005H); `pip install pymodbus` then set `modbus.enabled`
in `app_config.yaml`.

### B. Driver PG speed pulse (optional)
```
driver PG ──(3k-10k pull-up to +5 V)──▶ NUCLEO timer input-capture pin
```
PG = 3 × pole-pairs = **12 pulses/rev**. NOTE: the existing Hall method already gives
**24 edges/rev (2× the resolution)**. PG is a redundant cross-check, not an upgrade.

### C. Driver ALM alarm → MCU (log only)
```
driver ALM ──▶ NUCLEO GPIO/EXTI   (drops on driver fault)
```
Use to log faults and command SV→0. **Not** the hard safety layer (see Appendix).

### D. Vibration: KX134-1211 tri-axial accelerometer (richest fault signal)
```
BIT-BANG I2C: SDA = PB9 / D14, SCL = PB8 / D15, addr 0x1F (F401 hardware-I2C
BUSY errata makes the peripheral unusable here, so bit-bang is deliberate)
```
**Stud-mount** (or thin epoxy) to the motor **bearing housing**, sensor axis radial.
Loose/tape mounting lowers the resonant frequency and clips high-frequency content. Sensor
mass < 10 % of motor mass. This is the channel that needs the rigid fixture most.

### E. Thermal: 10k NTC (motor). No ambient sensor is fitted.
```
10k NTC (beta 3550) + 10k series from 3V3 ──▶ PA1 / A1 / ADC1_IN1
```
NTC bonded to the motor casing. No ambient sensor is fitted. Lets you bin baselines by
temperature instead of relying on a fixed warm-up.

### F. Per-phase current / MCSA: needs X-NUCLEO-IHM08M1
```
3× low-side shunts + amps ──▶ ADC PA0 / PC0 / PC1
```
Per-phase current for true MCSA. Interim: a DC-bus shunt on the 24 V return → `i_dc_a` as a
load proxy. Blocked until the IHM08M1 power stage is on the bench.

---

## APPENDIX: datasheet-verified limits & the safety invariant
Verified 2026-06-20 against `BLD510B_DRIVER.pdf` and `Datasheet_42BL_MOTOR.pdf`.

MOTOR 42BL41.010: 8-pole (4 pp) · 24 V · 4000 rpm · **rated 1.79 A, peak 6.0 A** ·
no-load 0.20 A · Kt 0.035 Nm/A · L-L 1.5 Ω / 2.1 mH (phase ≈ half) · rotor inertia
24 g·cm² · Hall 120° · **MAX radial 28 N @10 mm / axial 10 N** ← imbalance-test ceiling.

DRIVER BLD-510B at 24 V: 8.3 A cont / ~15 A peak (motor is the limit). SV 0-5 V = 0-rated;
<0.3 V = stop (~150 rpm min). **EN logic version-dependent** (V2.0 vs V2.4); confirm.
BK braking datasheet-discouraged → coast. Modbus/PG/ALM as above.

**SAFETY INVARIANT (hardware-enforced, no firmware):** in this rig the STM32 only outputs
an analog SV setpoint; it does **not** commutate. The hard protection is the **BLD-510B's
own over-current / over-voltage / under-voltage / over-temp / stall / Hall-illegal**
shutdowns plus the **EN** line. The PHM severity (S0-S3) only *warns and throttles*; it is
never the thing that stops the motor in an emergency. (The timer-BKIN fast-shutdown path
only becomes relevant later with the X-NUCLEO-IHM08M1, where the STM32 drives the PWM.)

Regen note: this bare rotor stores ~0.04 J at 1700 rpm; coast-down won't pump the bus.
Over-voltage risk is real only with an added flywheel/inertial load or the BK pin.
