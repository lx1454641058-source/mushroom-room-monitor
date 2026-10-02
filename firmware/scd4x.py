"""Original minimal SCD41 driver from Sensirion's published command protocol."""
import time
try:
    from .aht20 import crc8
except ImportError:
    from aht20 import crc8


def words(data):
    if not data or len(data) % 3:
        raise ValueError("SCD4x length error")
    result = []
    for i in range(0, len(data), 3):
        if crc8(data[i:i + 2]) != data[i + 2]:
            raise ValueError("SCD4x CRC error")
        result.append(data[i] * 256 + data[i + 1])
    return result


class SCD4x:
    def __init__(self, i2c):
        self.i2c = i2c
        self.send(0x3F86)
        time.sleep_ms(500)
        # A cultivation box may never see weekly fresh-air baseline. Disable ASC
        # in RAM only; calibration against a reference remains a physical test.
        self.i2c.writeto(0x62, b"\x24\x16\x00\x00\x81")
        time.sleep_ms(2)
        self.send(0x21B1)

    def send(self, command):
        self.i2c.writeto(0x62, bytes((command >> 8, command & 255)))

    def read(self):
        self.send(0xE4B8)
        time.sleep_ms(2)
        if not words(self.i2c.readfrom(0x62, 3))[0] & 0x7FF:
            return None
        self.send(0xEC05)
        time.sleep_ms(2)
        co2 = words(self.i2c.readfrom(0x62, 9))[0]
        if not 0 < co2 <= 40000:
            raise ValueError("SCD4x CO2 invalid")
        return co2
