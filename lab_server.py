#!/usr/bin/env python3
"""Local browser workspace for the Restate demo. Standard library only.

Run `docker compose up -d`, then `python3 lab_server.py` and open the printed
http://127.0.0.1 address. Only loopback requests are accepted.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
PAGE = (ROOT / "lab_page.html").read_bytes()
SCENARIOS = {
    "stable": ("1.1", "demo.py", "--agent", "naive", "--model", "stable"),
    "varying": ("1.2", "demo.py", "--agent", "naive", "--model", "varying"),
    "drifted": ("1.3", "demo.py", "--agent", "naive", "--model", "drifted", "--wait", "150"),
    "delayed": ("1.3 variation", "demo.py", "--agent", "naive", "--model", "drifted", "--wait", "150", "--restart-delay", "9"),
    "journaled": ("1.4", "demo.py", "--agent", "journaled", "--model", "varying"),
    "midcall": ("1.5", "demo.py", "--agent", "journaled", "--model", "stable", "--kill-during", "model"),
    "recovery": ("1.6", "recover.py"),
}
lock = threading.Lock()
current: subprocess.Popen | None = None
current_key = ""
current_log: Path | None = None
current_started = 0.0
prepared = False


def restate_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:19070/health", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def snapshot() -> dict:
    with lock:
        proc, key, log, started = current, current_key, current_log, current_started
    running = proc is not None and proc.poll() is None
    tail = ""
    if log and log.exists():
        with log.open("rb") as source:
            source.seek(max(0, log.stat().st_size - 40000))
            tail = source.read().decode("utf-8", errors="replace")
    return {"restate": restate_ready(), "prepared": prepared,
            "running": running, "scenario": key,
            "elapsed": round(time.time() - started) if running else None,
            "exitCode": None if running or proc is None else proc.returncode,
            "output": tail}


class Handler(BaseHTTPRequestHandler):
    def send_data(self, status: int, data: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status: int, value: dict) -> None:
        self.send_data(status, json.dumps(value).encode(), "application/json")

    def local_request(self) -> bool:
        host = self.headers.get("Host", "")
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            return False
        origin = self.headers.get("Origin")
        return not origin or urlsplit(origin).netloc == host and urlsplit(origin).scheme == "http"

    def do_GET(self) -> None:
        if not self.local_request():
            return self.send_json(403, {"error": "Open the printed local address."})
        if self.path in ("/", "/en", "/en/"):
            return self.send_data(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self.send_json(200, snapshot())
        self.send_data(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        global current, current_key, current_log, current_started, prepared
        if not self.local_request():
            return self.send_json(403, {"error": "Open the printed local address."})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024:
                raise ValueError("Request too large")
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            if self.path == "/api/prepare":
                with lock:
                    if current and current.poll() is None:
                        raise ValueError("Stop the current scenario before preparing again.")
                if sys.version_info < (3, 10):
                    raise ValueError("Python 3.10 or newer is required for the demo.")
                venv = ROOT / ".venv"
                if not (venv / "bin/python").exists():
                    subprocess.run([sys.executable, "-m", "venv", str(venv)], cwd=ROOT, check=True)
                install = subprocess.run([str(venv / "bin/python"), "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                                         cwd=ROOT, capture_output=True, text=True, timeout=240,
                                         env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
                if install.returncode:
                    raise RuntimeError("Package install failed: " + (install.stderr or install.stdout)[-2000:])
                prepared = True
                return self.send_json(200, {"message": "Ready. Python packages installed in .venv; Restate " + ("is ready." if restate_ready() else "is not running yet. Start Docker and refresh.")})
            if self.path == "/api/start":
                key = body.get("scenario")
                if key not in SCENARIOS:
                    raise ValueError("Choose a scenario card.")
                if not prepared and not (ROOT / ".venv/bin/python").exists():
                    raise ValueError("Click Prepare first.")
                if not restate_ready():
                    raise ValueError("Restate is not ready. In another terminal run: docker compose up -d")
                with lock:
                    if current and current.poll() is None:
                        raise ValueError(f"{current_key} is still running. Wait or stop it before starting another scenario.")
                    if any(port_open(port) for port in (8765, 9080, 9081)):
                        raise ValueError("A model stub or agent is still using port 8765, 9080, or 9081. Stop that run first.")
                    label, *args = SCENARIOS[key]
                    logs = ROOT / ".browser-runs"
                    logs.mkdir(exist_ok=True)
                    log = logs / f"{key}-{time.time_ns()}.log"
                    with log.open("wb") as output:
                        current = subprocess.Popen([str(ROOT / ".venv/bin/python"), *args], cwd=ROOT,
                            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True)
                    current_key, current_log, current_started = key, log, time.time()
                return self.send_json(200, {"message": f"Started exercise {label}. Output updates below."})
            if self.path == "/api/stop":
                with lock:
                    proc = current
                if not proc or proc.poll() is not None:
                    raise ValueError("No scenario is running.")
                os.killpg(proc.pid, signal.SIGINT)
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=3)
                return self.send_json(200, {"message": "Stopped the scenario and its child processes."})
            self.send_data(404, b"Not found", "text/plain")
        except (ValueError, RuntimeError, subprocess.SubprocessError, OSError) as error:
            self.send_json(400, {"error": str(error)})

    def log_message(self, *args) -> None:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3002, help="local port (default: 3002)")
    args = parser.parse_args()
    if port_open(args.port):
        parser.exit(1, f"Port {args.port} is in use. Run: python3 lab_server.py --port {args.port + 1}\n")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"lab      http://127.0.0.1:{args.port}/", flush=True)
    print(f"restate  http://127.0.0.1:19070/  ({'ready' if restate_ready() else 'not running: docker compose up -d'})", flush=True)
    print("Ctrl-C to stop the page. A running scenario will keep going until it finishes or you click Stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nLab page stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
