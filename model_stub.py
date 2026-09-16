"""A stand-in for a paid model API and a weather API, with call counters.

The agent service is killed and restarted during the demo, so the counters live
here, in a separate process: they are the evidence of what ran twice.

    MODEL_ANSWERS=stable   the model gives the same decision for the same messages
    MODEL_ANSWERS=varying  like a real model at temperature > 0: the same messages
                           get a different decision on alternate calls
    MODEL_ANSWERS=drifted  the model changes its mind once and stays changed: after
                           the first call, the same messages always get a direct answer

Endpoints:
    POST /model         {"messages": [...]} -> {"tool": "get_weather", "city": ...} or {"answer": ...}
    POST /weather       {"city": ...}       -> {"forecast": ...}
    GET  /stats         {"model_calls": n, "weather_calls": n}
    POST /reset         zero the counters
"""

import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = os.environ.get("MODEL_ANSWERS", "stable")
PORT = int(os.environ.get("MODEL_STUB_PORT", "8765"))

_lock = threading.Lock()
_counts = {"model_calls": 0, "weather_calls": 0}


def log(line: str) -> None:
    print(f"{datetime.now():%H:%M:%S}  {line}", flush=True)


def decide(messages: list[dict], call_number: int) -> dict:
    tool_result = next((m for m in messages if m.get("role") == "tool"), None)
    if tool_result is not None:
        return {"answer": f"Berlin right now: {tool_result['content']}."}
    changed_mind = (MODE == "varying" and call_number % 2 == 0) or (MODE == "drifted" and call_number > 1)
    if changed_mind:
        # Same question, different decision: skip the tool and answer directly.
        return {"answer": "Berlin is probably mild this time of year."}
    return {"tool": "get_weather", "city": "Berlin"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("content-length") or 0)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self) -> None:
        if self.path == "/stats":
            with _lock:
                return self._json(200, dict(_counts))
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self._body()
        if self.path == "/model":
            with _lock:
                _counts["model_calls"] += 1
                n = _counts["model_calls"]
            decision = decide(body.get("messages", []), n)
            log(f"model call #{n}  ->  {json.dumps(decision)}")
            return self._json(200, decision)
        if self.path == "/weather":
            with _lock:
                _counts["weather_calls"] += 1
                n = _counts["weather_calls"]
            log(f"weather call #{n}  get_weather({body.get('city')})")
            return self._json(200, {"forecast": "18°C and cloudy"})
        if self.path == "/reset":
            with _lock:
                _counts.update(model_calls=0, weather_calls=0)
            return self._json(200, dict(_counts))
        self._json(404, {"error": "not found"})

    def log_message(self, *args) -> None:  # silence the default access log
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"model stub on :{PORT}, MODEL_ANSWERS={MODE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
