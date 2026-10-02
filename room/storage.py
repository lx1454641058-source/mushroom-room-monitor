import csv
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def csv_cell(value):
    # Keep sensor numbers numeric, but prevent device-supplied text becoming a formula.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class Store:
    def __init__(self, directory, mode):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.mode = mode
        self.lock = threading.Lock()
        self.db = sqlite3.connect(directory / "room.sqlite3", check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, at TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, text TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS photos (id TEXT PRIMARY KEY, at TEXT NOT NULL, mime TEXT NOT NULL, image BLOB NOT NULL)")
        self.db.commit()

    def sample(self, payload):
        with self.lock:
            self.db.execute("INSERT INTO samples(at,payload) VALUES (?,?)", (timestamp(), json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            self.db.commit()

    def event(self, text):
        with self.lock:
            self.db.execute("INSERT INTO events(at,text) VALUES (?,?)", (timestamp(), str(text)[:2000]))
            self.db.commit()

    def history(self, limit=300):
        with self.lock:
            rows = self.db.execute("SELECT id,at,payload FROM samples ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": i, "at": at, **json.loads(p)} for i, at, p in reversed(rows)]

    def events(self):
        with self.lock:
            rows = self.db.execute("SELECT id,at,text FROM events ORDER BY id DESC LIMIT 30").fetchall()
        return [{"id": i, "at": at, "text": text} for i, at, text in rows]

    def photos(self):
        with self.lock:
            rows = self.db.execute("SELECT id,at FROM photos ORDER BY at DESC, rowid DESC LIMIT 24").fetchall()
        return [{"id": i, "at": at, "url": "/photos/" + i} for i, at in rows]

    def photo(self, identifier):
        with self.lock:
            return self.db.execute("SELECT mime,image FROM photos WHERE id=?", (identifier,)).fetchone()

    def add_photo(self, identifier, mime, image):
        with self.lock:
            self.db.execute("INSERT INTO photos VALUES (?,?,?,?)", (identifier, timestamp(), mime, image))
            self.db.commit()

    def csv(self):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        liquid_fields = ("water_pump", "nutrient_pump", "water_source_ok", "water_room_ok", "nutrient_source_ok", "nutrient_room_ok", "remaining_ms", "fault")
        climate_fields = ("co2", "co2_ok", "enabled", "heater", "cooler", "ventilation", "fault")
        writer.writerow(["sample_id", "utc_time", "source", "temperature_C", "humidity_percent", "sensor_ok", "water_ok", "enabled", "mist_command", "fan_command", "fault"] + ["liquid_" + k for k in liquid_fields] + ["climate_" + k for k in climate_fields])
        for row in self.history(100000):
            writer.writerow([row["id"], row["at"], self.mode] + [csv_cell(row.get(k)) for k in ("temperature", "humidity", "sensor_ok", "water_ok", "enabled", "mist", "fan", "fault")] + [csv_cell((row.get("liquids") or {}).get(k)) for k in liquid_fields] + [csv_cell((row.get("climate") or {}).get(k)) for k in climate_fields])
        return stream.getvalue().encode("utf-8-sig")

    def close(self):
        with self.lock:
            self.db.close()
