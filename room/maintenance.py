"""Read-only checks and consistent SQLite backups; no hardware writes."""
import argparse
import importlib.util
import json
import sqlite3
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DATA = Path(__file__).resolve().parent.parent / ".data"


def open_readonly(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def database_info(connection):
    check = connection.execute("PRAGMA quick_check").fetchall()
    if check != [("ok",)]:
        raise ValueError("数据库完整性检查未通过，请保留原文件并排查")
    return {name: connection.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in ("samples", "events", "photos")}


def backup(data_dir, mode):
    source = data_dir / mode / "room.sqlite3"
    if not source.is_file():
        raise ValueError("尚无该模式的数据库，请先采集数据")
    # Capture immutable vision records before the DB snapshot, so all referenced
    # photos have already been committed when SQLite's backup is taken.
    vision_records = {}
    for path in sorted((data_dir / mode / "vision").glob("*.json")):
        if path.is_symlink():
            raise ValueError("视觉记录目录不能包含链接文件")
        content = path.read_bytes()
        json.loads(content)
        vision_records[path.name] = content
    destination_dir = data_dir / "backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_dir / ("%s-%s-%s.sqlite3" % (mode, stamp, uuid.uuid4().hex[:12]))
    # Reserve a fresh file exclusively. Never overwrite an existing experiment record.
    with destination.open("xb"):
        pass
    try:
        with open_readonly(source) as origin:
            with sqlite3.connect(destination) as target:
                origin.backup(target, pages=128)
                counts = database_info(target)
    except Exception as exc:
        raise RuntimeError("备份未通过校验，原数据未改动；保留的失败文件：%s；%s" % (destination, exc)) from exc
    finally:
        # sqlite3's context manager commits but does not close the connection.
        if "origin" in locals():
            origin.close()
        if "target" in locals():
            target.close()
    vision_path = None
    if vision_records:
        archive_path = destination.with_suffix('.vision.zip')
        with zipfile.ZipFile(archive_path, 'x', zipfile.ZIP_DEFLATED) as archive:
            for name, content in vision_records.items():
                archive.writestr(name, content)
        with zipfile.ZipFile(archive_path) as archive:
            if archive.testzip() is not None:
                raise ValueError("视觉记录备份校验失败，原记录保留")
        vision_path = str(archive_path)
    return {"path": str(destination), "mode": mode, "counts": counts,
            "vision_path": vision_path, "vision_records": len(vision_records)}


def check(data_dir):
    results = {"python": sys.version.split()[0], "executable": sys.executable,
               "python_supported": sys.version_info >= (3, 10),
               "hardware_dependency_installed": importlib.util.find_spec("serial") is not None,
               "databases": {}, "hardware_tested": False}
    for mode in ("demo", "serial"):
        path = data_dir / mode / "room.sqlite3"
        if not path.is_file():
            results["databases"][mode] = {"exists": False}
            continue
        connection = open_readonly(path)
        try:
            results["databases"][mode] = {"exists": True, "counts": database_info(connection)}
        finally:
            connection.close()
    return results


def main():
    parser = argparse.ArgumentParser(description="蘑菇房软件自检 / 数据备份")
    parser.add_argument("action", choices=("check", "backup"))
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--mode", choices=("demo", "serial"), default="demo")
    args = parser.parse_args()
    try:
        if args.action == "backup":
            result = backup(args.data_dir, args.mode)
            print("备份成功：" + result["path"])
            print("记录条数：" + json.dumps(result["counts"], ensure_ascii=False))
            if result["vision_path"]:
                print("视觉记录备份：" + result["vision_path"])
        else:
            result = check(args.data_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            if not result["python_supported"]:
                raise ValueError("需要 Python 3.10 或更新版本")
            print("软件自检通过。串口依赖为可选项；未连接或驱动任何硬件。")
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
        print("未完成：" + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
