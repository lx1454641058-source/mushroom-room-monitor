import argparse
import threading
import webbrowser
from pathlib import Path
from .runtime import Runtime
from .storage import Store
from .server import make_server


def main():
    parser = argparse.ArgumentParser(description="蘑菇房监测与控制毕设原型")
    parser.add_argument("--mode", choices=("demo", "serial"), default="demo")
    parser.add_argument("--serial-port", help="实物串口，例如 COM5")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent.parent / ".data")
    parser.add_argument("--ollama-model", help="可选：已安装的本机模型，例如 qwen3:4b；端口固定 11435")
    parser.add_argument("--open-browser", action="store_true")
    parser.add_argument("--vision-model", help="可选：本机视觉模型，例如qwen3-vl:2b；端口11435")
    args = parser.parse_args()
    if args.mode == "serial" and not args.serial_port:
        parser.error("实物模式必须明确指定 --serial-port；不会自动回退模拟模式")
    if not 1024 <= args.port <= 65535:
        parser.error("端口应为 1024～65535")
    store = Store(args.data_dir / args.mode, args.mode)
    runtime = Runtime(args.mode, store, args.serial_port)
    server = None
    try:
        server = make_server(runtime, store, args.port, args.ollama_model, args.vision_model)
        runtime.start()
        print("Mushroom Room: http://127.0.0.1:%s | mode=%s | outputs OFF until Start" % (args.port, args.mode), flush=True)
        if args.open_browser:
            threading.Timer(0.8, webbrowser.open, args=("http://127.0.0.1:%s" % args.port,)).start()
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        if runtime.thread.is_alive():
            runtime.close()
        if server:
            server.server_close()
        store.close()


if __name__ == "__main__":
    main()
