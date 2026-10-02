"""Original AHT20 driver for calibrated modules; Aosong B2 (see SOURCES.md)."""
import time


def crc8(data):
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ (0x31 if crc & 0x80 else 0)) & 0xFF
    return crc


def decode(data):
    if len(data) != 7 or crc8(data[:6]) != data[6]:
        raise ValueError("AHT20 CRC error")
    if data[0] & 0x80 or data[0] & 0x18 != 0x18:
        raise ValueError("AHT20 busy or not calibrated")
    humidity = ((data[1] << 12) | (data[2] << 4) | (data[3] >> 4)) * 100 / 1048576
    temperature = (((data[3] & 15) << 16) | (data[4] << 8) | data[5]) * 200 / 1048576 - 50
    return round(temperature, 2), round(humidity, 2)


class AHT20:
    def __init__(self, i2c):
        self.i2c = i2c
        time.sleep_ms(100)
        # Reading address 0x38 generates address byte 0x71 on the I2C bus.
        # Do not guess vendor-specific calibration register writes on unknown modules.
        if self.i2c.readfrom(0x38, 1)[0] & 0x18 != 0x18:
            raise ValueError("AHT20 not calibrated; verify module revision")
        time.sleep_ms(10)

    def read(self):
        self.i2c.writeto(0x38, b"\xac\x33\x00")
        time.sleep_ms(85)
        return decode(self.i2c.readfrom(0x38, 7))
