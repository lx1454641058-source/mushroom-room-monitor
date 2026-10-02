import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from .images import validate_png
from .report import build_report, explain_report
from .agent import RoomAgent
from .vision import VisionService

STATIC = Path(__file__).resolve().parent.parent / "static"


def make_server(runtime, store, port=8770, model=None, vision_model=None):
    analysis_lock = threading.Lock()
    agent = RoomAgent(runtime, store, model)
    vision = VisionService(store, vision_model)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, fmt, *args):
            pass

        def trusted(self, mutation=False):
            hosts = ("127.0.0.1:%s" % self.server.server_port, "localhost:%s" % self.server.server_port)
            if self.headers.get("Host") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ["http://" + host for host in hosts]:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            return not mutation or self.headers.get("X-Room-Client") == "local-dashboard"

        def send(self, data, mime="application/json; charset=utf-8", status=200, extra=None):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Permissions-Policy", "camera=(self), microphone=()")
            if extra:
                for key, value in extra.items():
                    self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

        def do_GET(self):
            if not self.trusted():
                self.send({"error": "仅允许本机页面访问"}, status=403)
                return
            parts = urlsplit(self.path)
            path = parts.path
            try:
                if path == "/api/status":
                    self.send({**runtime.snapshot(), "model": model, "vision_model": vision_model, "events": store.events()})
                elif path == "/api/history":
                    limit = int(parse_qs(parts.query).get("limit", ["300"])[0])
                    self.send({"rows": store.history(max(1, min(3600, limit)))})
                elif path == "/api/report":
                    self.send(build_report(runtime, store))
                elif path == "/api/report.txt":
                    self.send(build_report(runtime, store)["text"].encode("utf-8-sig"), "text/plain; charset=utf-8",
                              extra={"Content-Disposition": 'attachment; filename="mushroom-report-%s.txt"' % runtime.mode})
                elif path == "/api/export.csv":
                    self.send(store.csv(), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="mushroom-%s.csv"' % runtime.mode})
                elif path == "/api/photos":
                    self.send({"photos": store.photos()})
                elif re.fullmatch(r"/photos/[a-f0-9]{32}", path):
                    photo = store.photo(path.split("/")[-1])
                    if photo:
                        self.send(photo[1], photo[0])
                    else:
                        self.send({"error": "照片不存在"}, status=404)
                elif path in ("/", "/app.js", "/extensions.js", "/style.css"):
                    file = {"/": "index.html", "/app.js": "app.js", "/extensions.js": "extensions.js", "/style.css": "style.css"}[path]
                    mime = {"/": "text/html; charset=utf-8", "/app.js": "text/javascript; charset=utf-8", "/extensions.js": "text/javascript; charset=utf-8", "/style.css": "text/css; charset=utf-8"}[path]
                    self.send((STATIC / file).read_bytes(), mime)
                else:
                    self.send({"error": "页面不存在"}, status=404)
            except (ValueError, TypeError) as exc:
                self.send({"error": str(exc)}, status=400)

        def do_POST(self):
            if not self.trusted(mutation=True):
                self.send({"error": "请求来源或页面标识不正确"}, status=403)
                return
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("不支持分块请求")
                length = int(self.headers.get("Content-Length", "0"))
                limit = 4 * 1024 * 1024 if self.path == "/api/photos" else 4096
                if not 0 < length <= limit:
                    raise ValueError("请求大小超出限制")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("请求不完整")
                if self.path == "/api/photos":
                    if self.headers.get("Content-Type") != "image/png":
                        raise ValueError("只接受PNG照片")
                    image = validate_png(body)
                    identifier = uuid.uuid4().hex
                    store.add_photo(identifier, "image/png", image)
                    store.event("保存照片：" + identifier)
                    self.send({"ok": True, "id": identifier})
                    return
                if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                    raise ValueError("请求必须使用 JSON")
                value = json.loads(body)
                if not isinstance(value, dict):
                    raise ValueError("请求必须为对象")
                if self.path == "/api/control":
                    self.send(runtime.command(value.get("action"), value.get("values")))
                elif self.path == "/api/scenario":
                    runtime.scenario(value.get("name"))
                    self.send({"ok": True})
                elif self.path == "/api/analysis":
                    if not analysis_lock.acquire(blocking=False):
                        self.send({"error": "模型正在分析，请稍后再试"}, status=409)
                        return
                    try:
                        self.send(explain_report(build_report(runtime, store), model))
                    finally:
                        analysis_lock.release()
                elif self.path == "/api/agent":
                    self.send(agent.handle(value.get("id"), value.get("message")))
                elif self.path == "/api/vision":
                    self.send(vision.analyze(value.get("photo_id"), value.get("species")))
                elif self.path == "/api/agent/client-result":
                    self.send(agent.complete_client(value.get("id"), value.get("action"), value.get("ok"), value.get("detail")))
                else:
                    self.send({"error": "接口不存在"}, status=404)
            except (ValueError, TypeError, UnicodeError, OSError) as exc:
                self.send({"error": str(exc)}, status=400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
