"""BLD-510B Modbus RTU telemetry poller: READY for when the MAX485 module arrives."""

POLES = 8
FAULT_BITS = {0x01: "locked_rotor", 0x02: "over_current",
              0x04: "hall_abnormal", 0x08: "bus_undervolt", 0x10: "bus_overvolt"}

REG_ACTUAL_SPEED = 0x8018
REG_FAULT_STATE = 0x8018 + 3   # $801BH


def decode_fault(bitmask):
    """Return list of active fault names from the $801BH bitmask."""
    return [name for bit, name in FAULT_BITS.items() if bitmask & bit]


def decode_speed(raw_value, poles=POLES):
    """Datasheet rule: actual_rpm = raw * 20 / poles."""
    return raw_value * 20 / poles


class ModbusTelemetry:
    """Thin wrapper; activates only when pymodbus + hardware are present."""
    def __init__(self, port="COM4", baud=9600, unit=1):
        self.port, self.baud, self.unit = port, baud, unit
        self.client = None
        self.available = False
        self.last_error = None

    def connect(self):
        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError:
            self.last_error = "pymodbus not installed (pip install pymodbus)"
            return False
        try:
            self.client = ModbusSerialClient(
                port=self.port, baudrate=self.baud,
                bytesize=8, parity="N", stopbits=1, timeout=0.2)
            self.available = self.client.connect()
            if not self.available:
                self.last_error = f"could not open {self.port}"
            return self.available
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def poll(self):
        """Return {'modbus_rpm', 'modbus_fault', 'faults'} or None if unavailable."""
        if not self.available or self.client is None:
            return None
        try:
            spd = self.client.read_holding_registers(REG_ACTUAL_SPEED, count=1, slave=self.unit)
            flt = self.client.read_holding_registers(REG_FAULT_STATE, count=1, slave=self.unit)
            if spd.isError() or flt.isError():
                return None
            raw_speed = spd.registers[0]
            fault_word = flt.registers[0] & 0xFF
            return {
                "modbus_rpm": round(decode_speed(raw_speed), 1),
                "modbus_fault": fault_word,
                "faults": decode_fault(fault_word),
            }
        except Exception as exc:
            self.last_error = str(exc)
            return None
