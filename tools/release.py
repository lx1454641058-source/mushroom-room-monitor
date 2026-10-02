"""Build a checked source archive from an isolated copy. No hardware or external services."""
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT_FILES = {".gitignore", "README.md", "run.ps1", "start-demo.cmd", "start-agent.cmd",
              "check-system.cmd", "backup-demo.cmd", "requirements-hardware.txt"}
ROOT_DIRS = {"room", "firmware", "static", "tests", "docs", "tools"}
EXCLUDE = {"__pycache__", ".data", ".runtime", ".tmp", ".git", "work"}
REQUIRED = {"room/__main__.py", "room/agent.py", "static/index.html",
            "firmware/main.py", "README.md", "run.ps1", "docs/DELIVERY.md"}


def collect(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if set(relative.parts) & EXCLUDE:
            continue
        if relative.parts[0] not in ROOT_DIRS and relative.as_posix() not in ROOT_FILES:
            continue
        if path.is_symlink():
            raise ValueError("Do not package symbolic links: " + str(relative))
        if path.is_file() and path.suffix not in (".pyc", ".log"):
            result[relative.as_posix()] = path.read_bytes()
    if not REQUIRED <= set(result):
        raise ValueError("Missing required source: " + ", ".join(sorted(REQUIRED - set(result))))
    return result


def hashes(files):
    return {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            for name, data in files.items()}


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Archive CRC failed")
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate archive member")
        manifest = json.loads(archive.read("mushroom-room/RELEASE.json"))
        expected = {"mushroom-room/" + name for name in manifest["files"]}
        if set(names) != expected | {"mushroom-room/RELEASE.json"}:
            raise ValueError("Unexpected archive members")
        for name, record in manifest["files"].items():
            if "\\" in name or name.startswith("/") or ":" in name or ".." in name.split("/"):
                raise ValueError("Invalid archive path")
            data = archive.read("mushroom-room/" + name)
            if len(data) != record["bytes"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("File checksum failed: " + name)
        return manifest


def request(base, path, body=None):
    headers = {"X-Room-Client": "local-dashboard"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(base + path, data=data, headers=headers), timeout=5) as response:
        content = response.read()
        if "application/json" in response.headers.get("Content-Type", ""):
            return json.loads(content)
        return content


def smoke(root, env):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = "http://127.0.0.1:" + str(port)
    log = root.parent / "server.log"
    with log.open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "room", "--mode", "demo", "--port", str(port),
             "--data-dir", str(root.parent / "demo-data")],
            cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Isolated server exited: " + log.read_text(encoding="utf-8", errors="replace"))
                try:
                    state = request(base, "/api/status")
                    if state["connected"]:
                        break
                except (OSError, ValueError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Isolated demo did not become ready")
                time.sleep(0.15)
            assert state["mode"] == "demo" and state["status"]["enabled"] is False
            assert "蘑菇房智能体" in request(base, "/").decode()
            def ask(message):
                return request(base, "/api/agent", {"id": uuid.uuid4().hex, "message": message})
            result = ask("把湿度下限设为65，上限设为75，然后启动系统")
            assert result["ok"] and len(result["trace"]) == 2
            state = request(base, "/api/status")["status"]
            assert state["enabled"] and state["config"]["humidity_high"] == 75
            request(base, "/api/scenario", {"name": "dry"})
            assert request(base, "/api/status")["status"]["enabled"] is False
            assert ask("启动系统")["ok"] is False
            assert ask("全部停止")["ok"] is True
            assert "缺水" in ask("为什么不出雾")["text"]
            assert ask("抽水2秒")["ok"]
            assert request(base, "/api/status")["status"]["liquids"]["water_pump"]
            request(base, "/api/scenario", {"name": "water_target_full"})
            assert not request(base, "/api/status")["status"]["liquids"]["water_pump"]
            request(base, "/api/scenario", {"name": "restore"})
            assert ask("释放营养液1秒")["ok"]
            deadline = time.monotonic() + 4
            while request(base, "/api/status")["status"]["liquids"]["nutrient_pump"]:
                if time.monotonic() > deadline:
                    raise RuntimeError("Nutrient timer did not stop")
                time.sleep(0.15)
            assert ask("全部停止")["ok"]
            assert ask("把温度下限设为18，上限设为24")["ok"]
            assert ask("启动系统")["ok"]
            assert ask("启用温控与二氧化碳联动")["ok"]
            request(base, "/api/scenario", {"name": "co2_high"})
            request(base, "/api/control", {"action": "fan", "values": "off"})
            assert request(base, "/api/status")["status"]["fan"]
            request(base, "/api/scenario", {"name": "co2_fault"})
            status = request(base, "/api/status")["status"]
            assert not status["enabled"] and not status["climate"]["heater"] and not status["climate"]["cooler"]
            assert b"renderClimate" in request(base, "/extensions.js")
            request(base, "/api/scenario", {"name": "restore"})
            assert b"demo" in request(base, "/api/export.csv")
            assert len(request(base, "/api/report.txt")) > 100
            backup_result = ask("备份数据")
            assert backup_result["ok"]
            assert list((root.parent / "demo-data" / "backups").glob("*.sqlite3"))
            return ["initial_outputs_off", "dashboard", "agent_parameters_and_start",
                    "dry_shutdown", "dry_restart_rejected", "stop", "grounded_status",
                    "water_pump", "water_full_stop", "nutrient_timed_stop", "climate_settings", "climate_enable",
                    "co2_fan_override", "co2_fault_stop", "extension_script", "csv", "text_report", "database_backup"]
        finally:
            # This process belongs only to the isolated simulator created above.
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def build(root, destination):
    files = collect(root)
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
                "scope": "Software simulation verified; physical acceptance and final project scope remain open. See docs/DELIVERY.md.",
                "files": hashes(files)}
    with tempfile.TemporaryDirectory(prefix="mushroom-release-") as temporary:
        staged = Path(temporary) / "mushroom-room"
        for name, data in files.items():
            target = staged / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        env = dict(os.environ)
        # No workspace .runtime or parent packages may satisfy imports in the isolated run.
        env.pop("PYTHONPATH", None)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONNOUSERSITE"] = "1"
        command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]
        result = subprocess.run(command, cwd=staged, env=env, capture_output=True,
                                text=True, encoding="utf-8", timeout=120)
        log = result.stdout + result.stderr
        if result.returncode:
            raise RuntimeError("Isolated tests failed:\n" + log)
        count = re.search(r"Ran (\d+) tests?", log)
        if not count:
            raise RuntimeError("Test count missing")
        print("Isolated tests passed: " + count.group(1), flush=True)
        manifest["validation"] = {"unit_tests": int(count.group(1)), "unit_test_output": log,
                                  "smoke_checks": smoke(staged, env)}
        # Hashes cover the exact staged source bytes, never the generated simulator database.
    destination.mkdir(parents=True, exist_ok=True)
    name = "mushroom-room-checked-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6] + ".zip"
    path = destination / name
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr("mushroom-room/" + name, data)
        archive.writestr("mushroom-room/RELEASE.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    verify_archive(path)
    return path


def main():
    if not __debug__:
        raise RuntimeError("Do not use Python -O: release assertions must run")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path, help="Only check an existing archive")
    args = parser.parse_args()
    if args.verify:
        manifest = verify_archive(args.verify)
        print("Checksums passed for %s files" % len(manifest["files"]))
    else:
        root = Path(__file__).resolve().parents[1]
        path = build(root, args.output or root.parent / "output" / "mushroom-room")
        print("Verified source archive: " + str(path))


if __name__ == "__main__":
    main()
