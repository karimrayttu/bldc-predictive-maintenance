"""NUCLEO-F401RE (MB1136) board truth: STM32F401RET6, LQFP64."""

# -
# Each entry: (port_pin, arduino_label, header, adc_channel, timers, buses, note)
#   adc_channel : ADC1 input number, or None
#   timers      : list of "TIMx_CHy" this pin can output PWM on
#   buses       : list of peripheral functions (I2C / USART / SPI)
#   note        : board-level caveat, or "" if the pin is unencumbered
# -
_PINS = [
    # CN9 digital
    ("PA3",  "D0",  "CN9",  3,    ["TIM2_CH4", "TIM5_CH4", "TIM9_CH2"], ["USART2_RX"],
     "ST-LINK virtual COM port RX; this is the app's data link"),
    ("PA2",  "D1",  "CN9",  2,    ["TIM2_CH3", "TIM5_CH3", "TIM9_CH1"], ["USART2_TX"],
     "ST-LINK virtual COM port TX; this is the app's data link"),
    ("PA10", "D2",  "CN9",  None, ["TIM1_CH3"], ["USART1_RX", "I2C_none"], ""),
    ("PB3",  "D3",  "CN9",  None, ["TIM2_CH2"], ["SPI1_SCK", "SPI3_SCK", "I2C2_SDA"], ""),
    ("PB5",  "D4",  "CN9",  None, ["TIM3_CH2"], ["SPI1_MOSI", "SPI3_MOSI"], ""),
    ("PB4",  "D5",  "CN9",  None, ["TIM3_CH1"], ["SPI1_MISO", "SPI3_MISO", "I2C3_SDA"], ""),
    ("PB10", "D6",  "CN9",  None, ["TIM2_CH3"], ["I2C2_SCL", "SPI2_SCK", "USART3_TX"], ""),
    ("PA8",  "D7",  "CN9",  None, ["TIM1_CH1"], ["I2C3_SCL", "USART1_CK"], ""),

    # CN5 digital
    ("PA9",  "D8",  "CN5",  None, ["TIM1_CH2"], ["USART1_TX"], ""),
    ("PC7",  "D9",  "CN5",  None, ["TIM3_CH2"], ["USART6_RX", "SPI2_SCK"], ""),
    ("PB6",  "D10", "CN5",  None, ["TIM4_CH1"], ["I2C1_SCL", "USART1_TX"], ""),
    ("PA7",  "D11", "CN5",  7,    ["TIM3_CH2", "TIM1_CH1N"], ["SPI1_MOSI"], ""),
    ("PA6",  "D12", "CN5",  6,    ["TIM3_CH1"], ["SPI1_MISO"], ""),
    ("PA5",  "D13", "CN5",  5,    ["TIM2_CH1"], ["SPI1_SCK"],
     "drives LD2, the green user LED: usable, but the LED follows the pin"),
    ("PB9",  "D14", "CN5",  None, ["TIM4_CH4", "TIM11_CH1"], ["I2C1_SDA", "SPI2_NSS"], ""),
    ("PB8",  "D15", "CN5",  None, ["TIM4_CH3", "TIM10_CH1"], ["I2C1_SCL"], ""),

    # CN8 analog
    ("PA0",  "A0",  "CN8",  0,    ["TIM2_CH1", "TIM5_CH1"], ["USART2_CTS"], ""),
    ("PA1",  "A1",  "CN8",  1,    ["TIM2_CH2", "TIM5_CH2"], ["USART2_RTS"], ""),
    ("PA4",  "A2",  "CN8",  4,    [], ["SPI1_NSS", "SPI3_NSS", "DAC_none"], ""),
    ("PB0",  "A3",  "CN8",  8,    ["TIM3_CH3", "TIM1_CH2N"], [], ""),
    ("PC1",  "A4",  "CN8",  11,   [], [], ""),
    ("PC0",  "A5",  "CN8",  10,   [], [], ""),

    # ST morpho only (CN7 / CN10)
    ("PB1",  "",    "morpho", 9,  ["TIM3_CH4", "TIM1_CH3N"], [], ""),
    ("PB2",  "",    "morpho", None, [], [], "BOOT1, avoid, affects boot mode"),
    ("PB7",  "",    "morpho", None, ["TIM4_CH2"], ["I2C1_SDA", "USART1_RX"], ""),
    ("PB12", "",    "morpho", None, [], ["SPI2_NSS", "I2C2_SMBA"], ""),
    ("PB13", "",    "morpho", None, ["TIM1_CH1N"], ["SPI2_SCK"], ""),
    ("PB14", "",    "morpho", None, ["TIM1_CH2N"], ["SPI2_MISO"], ""),
    ("PB15", "",    "morpho", None, ["TIM1_CH3N"], ["SPI2_MOSI"], ""),
    ("PC2",  "",    "morpho", 12,  [], ["SPI2_MISO"], ""),
    ("PC3",  "",    "morpho", 13,  [], ["SPI2_MOSI"], ""),
    ("PC4",  "",    "morpho", 14,  [], [], ""),
    ("PC5",  "",    "morpho", 15,  [], [], ""),
    ("PC6",  "",    "morpho", None, ["TIM3_CH1"], ["USART6_TX"], ""),
    ("PC8",  "",    "morpho", None, ["TIM3_CH3"], [], ""),
    ("PC9",  "",    "morpho", None, ["TIM3_CH4"], ["I2C3_SDA"], ""),
    ("PC10", "",    "morpho", None, [], ["SPI3_SCK", "USART3_TX"], ""),
    ("PC11", "",    "morpho", None, [], ["SPI3_MISO", "USART3_RX"], ""),
    ("PC12", "",    "morpho", None, [], ["SPI3_MOSI"], ""),
    ("PC13", "",    "morpho", None, [], [],
     "B1 blue USER button (pulled high, pressed = low)"),
    ("PC14", "",    "morpho", None, [], [], "OSC32_IN, 32 kHz crystal pads"),
    ("PC15", "",    "morpho", None, [], [], "OSC32_OUT, 32 kHz crystal pads"),
    ("PA11", "",    "morpho", None, ["TIM1_CH4"], ["USB_DM"], ""),
    ("PA12", "",    "morpho", None, [], ["USB_DP"], ""),
    ("PA13", "",    "morpho", None, [], ["SWDIO"],
     "SWDIO: debug/flash. Reassigning this bricks ST-LINK access"),
    ("PA14", "",    "morpho", None, [], ["SWCLK"],
     "SWCLK: debug/flash. Reassigning this bricks ST-LINK access"),
    ("PA15", "",    "morpho", None, ["TIM2_CH1"], ["SPI1_NSS", "SPI3_NSS"], ""),
    ("PD2",  "",    "morpho", None, [], ["SDIO_CMD"], ""),
    ("PH0",  "",    "morpho", None, [], [],
     "OSC_IN: 8 MHz MCO from ST-LINK, the system clock source"),
    ("PH1",  "",    "morpho", None, [], [], "OSC_OUT, clock"),
]

# Pins that must never be reassigned: doing so breaks flashing or the data link itself.
HARD_RESERVED = {
    "PA2":  "USART2_TX to the ST-LINK virtual COM port, the telemetry link",
    "PA3":  "USART2_RX from the ST-LINK virtual COM port, the telemetry link",
    "PA13": "SWDIO: reassigning loses debug/flash access to the board",
    "PA14": "SWCLK: reassigning loses debug/flash access to the board",
    "PH0":  "OSC_IN: 8 MHz clock in from ST-LINK MCO",
    "PH1":  "OSC_OUT: clock",
}

# Usable, but they collide with something on the board. Warn, do not block.
SOFT_WARN = {
    "PA5":  "also drives LD2, the green user LED",
    "PC13": "also the B1 blue user button",
    "PB2":  "BOOT1: influences boot mode",
    "PC14": "OSC32 crystal pad",
    "PC15": "OSC32 crystal pad",
    "PA11": "USB_DM on the morpho header",
    "PA12": "USB_DP on the morpho header",
}


class Pin(object):
    __slots__ = ("port_pin", "arduino", "header", "adc", "timers", "buses", "note")

    def __init__(self, port_pin, arduino, header, adc, timers, buses, note):
        self.port_pin = port_pin
        self.arduino = arduino
        self.header = header
        self.adc = adc
        self.timers = list(timers)
        self.buses = list(buses)
        self.note = note

    # capability tests used to filter the dropdowns
    @property
    def port(self):
        return self.port_pin[1]           # 'A' / 'B' / 'C' / 'D' / 'H'

    @property
    def number(self):
        return int(self.port_pin[2:])     # 0..15

    @property
    def exti_line(self):
        """EXTI lines are shared BY PIN NUMBER across every port: PA10 and PB10 both use
        EXTI10 and cannot both be interrupt sources. This is the classic STM32 trap that
        silently kills one Hall channel, so the editor validates it."""
        return self.number

    def can_adc(self):
        return self.adc is not None

    def can_pwm(self):
        return bool(self.timers)

    def can_gpio(self):
        return self.port_pin not in HARD_RESERVED

    def is_reserved(self):
        return self.port_pin in HARD_RESERVED

    def warning(self):
        return SOFT_WARN.get(self.port_pin, "")

    def label(self):
        """What the dropdown shows: Arduino name first (that is what he plugs into),
        then the GPIO, then the capability that matters."""
        head = f"{self.arduino} · {self.port_pin}" if self.arduino else f"{self.port_pin}"
        bits = []
        if self.adc is not None:
            bits.append(f"ADC1_IN{self.adc}")
        if self.timers:
            bits.append(self.timers[0])
        return f"{head}   ({', '.join(bits)})" if bits else head


PINS = [Pin(*row) for row in _PINS]
BY_NAME = {p.port_pin: p for p in PINS}
BY_ARDUINO = {p.arduino: p for p in PINS if p.arduino}


def pins_for(capability):
    """Dropdown contents for a signal that needs `capability`."""
    if capability == "adc":
        out = [p for p in PINS if p.can_adc()]
    elif capability == "pwm":
        out = [p for p in PINS if p.can_pwm()]
    else:                                  # 'gpio' / 'exti', any real GPIO
        out = list(PINS)
    out = [p for p in out if not p.is_reserved()]
    # Arduino header pins first (that is what is physically reachable), then morpho
    order = {"CN8": 0, "CN9": 1, "CN5": 2, "morpho": 3}
    return sorted(out, key=lambda p: (order.get(p.header, 9), p.port, p.number))


# -
#  SIGNALS; what this bench actually needs wired, and what each one demands
#  of a pin. Order is display order on the Pinout page.
# -
SIGNALS = [
    ("hall_a",   "Hall A",            "exti", "Motor Hall sensor A. Needs a unique EXTI line."),
    ("hall_b",   "Hall B",            "exti", "Motor Hall sensor B. Needs a unique EXTI line."),
    ("hall_c",   "Hall C",            "exti", "Motor Hall sensor C. Needs a unique EXTI line."),
    ("sv_pwm",   "SV speed command",  "pwm",  "1 kHz PWM to the BLD-510B SV input. Needs a timer channel."),
    ("drv_en",   "Driver EN",         "gpio", "Open-drain to BLD-510B EN. Sunk to COM = enabled; released = output stage off. Replaces the EN-COM strap that left ~10 mA parked bias in hall sectors 2/4."),
    ("i_sense",  "Current sense",     "adc",  "INA240A2 output. Needs an ADC1 channel."),
    ("ntc",      "Motor temp (NTC)",  "adc",  "10k NTC divider node. Needs an ADC1 channel."),
    ("kx_sda",   "KX134 SDA",         "gpio", "Bit-bang I2C data. Any GPIO (open-drain in firmware)."),
    ("kx_scl",   "KX134 SCL",         "gpio", "Bit-bang I2C clock. Any GPIO (open-drain in firmware)."),
    ("kx_int1",  "KX134 INT1",        "gpio", "Data-ready interrupt. Currently parked and unused."),
]

# What the firmware is compiled with today (verified 2026-07-28 by the pin X-ray build).
# The Pinout page diffs the saved assignment against this and flags any disagreement.
FIRMWARE_PINS = {
    "hall_a":  "PA10",
    "hall_b":  "PB3",
    "hall_c":  "PB5",
    "sv_pwm":  "PB4",
    "drv_en":  "PA8",
    "i_sense": "PA0",
    "ntc":     "PA1",
    "kx_sda":  "PB9",
    "kx_scl":  "PB8",
    "kx_int1": "PA9",
}


def validate(assignment):
    """Check a {signal_key: port_pin} map. Returns a list of (level, message).

    level is 'error' (physically will not work) or 'warn' (works, but bites later)."""
    problems = []
    sig_meta = {k: (lbl, cap) for k, lbl, cap, _ in SIGNALS}

    # 1. two signals on one pin
    seen = {}
    for key, pin_name in assignment.items():
        if not pin_name:
            continue
        if pin_name in seen:
            problems.append(("error",
                f"{pin_name} is assigned to both {sig_meta.get(seen[pin_name], (seen[pin_name],))[0]} "
                f"and {sig_meta.get(key, (key,))[0]}; a pin can only carry one signal."))
        seen[pin_name] = key

    for key, pin_name in assignment.items():
        if not pin_name:
            problems.append(("error", f"{sig_meta.get(key, (key,))[0]} has no pin assigned."))
            continue
        p = BY_NAME.get(pin_name)
        if p is None:
            problems.append(("error", f"{pin_name} is not a pin on the STM32F401RE."))
            continue
        label, cap = sig_meta.get(key, (key, "gpio"))

        # 2. hard-reserved
        if p.is_reserved():
            problems.append(("error",
                f"{label} → {pin_name}: {HARD_RESERVED[p.port_pin]}"))

        # 3. capability
        if cap == "adc" and not p.can_adc():
            problems.append(("error",
                f"{label} → {pin_name} has no ADC input. Pick an analog-capable pin "
                f"(A0-A5, or D11/D12/D13)."))
        if cap == "pwm" and not p.can_pwm():
            problems.append(("error",
                f"{label} → {pin_name} has no timer channel, so it cannot generate PWM."))

        # 4. board-level caution
        if p.warning():
            problems.append(("warn", f"{label} → {pin_name}: {p.warning()}."))

    # 5. EXTI line collision: the silent killer
    exti_keys = [k for k, _, cap, _ in SIGNALS if cap == "exti"]
    lines = {}
    for key in exti_keys:
        pin_name = assignment.get(key)
        p = BY_NAME.get(pin_name or "")
        if p is None:
            continue
        lines.setdefault(p.exti_line, []).append((key, pin_name))
    for line, users in lines.items():
        if len(users) > 1:
            who = ", ".join(f"{sig_meta.get(k, (k,))[0]} ({pn})" for k, pn in users)
            problems.append(("error",
                f"EXTI{line} collision: {who}. On STM32 the EXTI line is chosen by PIN "
                f"NUMBER, not port; two pins ending in {line} cannot both raise interrupts. "
                f"One Hall channel would go dead."))

    # 6. ADC channel collision is fine (same ADC, different channels) but same channel is not
    adc_keys = [k for k, _, cap, _ in SIGNALS if cap == "adc"]
    chans = {}
    for key in adc_keys:
        p = BY_NAME.get(assignment.get(key) or "")
        if p is None or p.adc is None:
            continue
        chans.setdefault(p.adc, []).append(key)
    for ch, users in chans.items():
        if len(users) > 1:
            problems.append(("error",
                f"ADC1_IN{ch} is requested by {' and '.join(users)}; one channel, one signal."))

    return problems


def firmware_diff(assignment):
    """[(signal_key, saved_pin, firmware_pin, agrees)] so the page can show drift."""
    out = []
    for key, _lbl, _cap, _help in SIGNALS:
        saved = assignment.get(key) or ""
        fw = FIRMWARE_PINS.get(key, "")
        out.append((key, saved, fw, saved == fw))
    return out
