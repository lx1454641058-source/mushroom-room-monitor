"""Copy main.py, controller.py, liquids.py, climate.py, aht20.py, scd4x.py to ESP32-WROOM.
GPIO outputs are ACTIVE HIGH. Do not attach unverified relay/driver modules.
"""
from machine import Pin, I2C, WDT
import sys
import select
import time
import json
import os
import binascii
from controller import Controller
from aht20 import AHT20
from scd4x import SCD4x

mist_pin = Pin(25, Pin.OUT, value=0)
fan_pin = Pin(26, Pin.OUT, value=0)
water_pump_pin = Pin(32, Pin.OUT, value=0)
nutrient_pump_pin = Pin(33, Pin.OUT, value=0)
heater_pin = Pin(16, Pin.OUT, value=0)
cooler_pin = Pin(17, Pin.OUT, value=0)
liquid_pins = [Pin(n, Pin.IN, Pin.PULL_UP) for n in (14, 13, 23, 19)]
liquid_since = [None, None, None, None]
water_pin = Pin(27, Pin.IN, Pin.PULL_UP)
control = Controller()
boot_id = binascii.hexlify(os.urandom(8)).decode()
poll = select.poll()
poll.register(sys.stdin, select.POLLIN)
buffer = ""
discard = False
recent = []
now = 0
previous = time.ticks_ms()
sample_due = status_due = 0
seq = 0
sensor = None
co2_sensor = None
co2_due = 0
i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=100000)
water_since = None
wdt = WDT(timeout=8000)


def emit(value):
    value.update({"v": 1, "boot_id": boot_id})
    print(json.dumps(value))


def outputs():
    mist_pin.value(int(control.mist))
    fan_pin.value(int(control.fan))
    water_pump_pin.value(int(control.liquids.active == "water"))
    nutrient_pump_pin.value(int(control.liquids.active == "nutrient"))
    heater_pin.value(int(control.climate.heater))
    cooler_pin.value(int(control.climate.cooler))


def receive(message):
    if message.get("v") != 1 or message.get("boot_id") != boot_id:
        return
    if message.get("type") == "heartbeat":
        control.heartbeat(now)
        return
    if message.get("type") != "command":
        return
    command_id = message.get("id")
    if not isinstance(command_id, str) or not 1 <= len(command_id) <= 64:
        return
    # Retries may return an old acknowledgement, but never repeat an actuator action.
    for identifier, ack in recent:
        if identifier == command_id:
            emit(ack)
            return
    ack = {"type": "ack", "id": command_id, "ok": False}
    try:
        expires = message.get("expires_ms")
        if type(expires) not in (int, float) or not now <= expires <= now + 6000:
            raise ValueError("命令已过期或时钟不匹配")
        control.command(message.get("action"), message.get("values"), now)
        outputs()
        ack["ok"] = True
    except (ValueError, TypeError) as exc:
        ack["error"] = str(exc)
    recent.append((command_id, ack))
    if len(recent) > 16:
        recent.pop(0)
    emit(ack)


try:
    while True:
        current = time.ticks_ms()
        now += time.ticks_diff(current, previous)
        previous = current
        liquid_values = []
        for index, pin in enumerate(liquid_pins):
            if pin.value():
                liquid_since[index] = None
                liquid_values.append(False)
            else:
                if liquid_since[index] is None:
                    liquid_since[index] = now
                liquid_values.append(now - liquid_since[index] >= 1000)
        control.liquids.sample(liquid_values[0], liquid_values[1], liquid_values[2], liquid_values[3], now)
        # Opening/broken float circuit cuts output immediately; recovery debounces 1 s.
        if water_pin.value():
            water_since = None
            control.water_ok = False
        else:
            if water_since is None:
                water_since = now
            control.water_ok = now - water_since >= 1000
        if now >= sample_due:
            sample_due = now + 2000
            try:
                if sensor is None:
                    sensor = AHT20(i2c)
                temperature, humidity = sensor.read()
                control.sample(temperature, humidity, control.water_ok, now)
            except Exception:
                sensor = None
                control.sample(None, None, control.water_ok, now)
        if now >= co2_due:
            co2_due = now + 1000
            try:
                if co2_sensor is None:
                    co2_sensor = SCD4x(i2c)
                co2 = co2_sensor.read()
                if co2 is not None:
                    control.climate.sample(co2, now)
            except Exception:
                co2_sensor = None
                co2_due = now + 5000
                control.climate.sample(None, now)
        # Bound input processing so a stream cannot starve interlocks and the watchdog.
        for _ in range(512):
            if not poll.poll(0):
                break
            char = sys.stdin.read(1)
            if char == "\n":
                if not discard:
                    try:
                        value = json.loads(buffer)
                        if isinstance(value, dict):
                            receive(value)
                    except (ValueError, TypeError):
                        pass
                buffer, discard = "", False
            elif not discard:
                buffer += char
                if len(buffer) > 1024:
                    buffer, discard = "", True
        control.step(now)
        outputs()
        if now >= status_due:
            status_due = now + 1000
            seq += 1
            status = control.status(now)
            status.update({"type": "status", "seq": seq, "uptime_ms": now})
            emit(status)
        wdt.feed()
        time.sleep_ms(20)
finally:
    mist_pin.value(0)
    fan_pin.value(0)
    water_pump_pin.value(0)
    nutrient_pump_pin.value(0)
    heater_pin.value(0)
    cooler_pin.value(0)
