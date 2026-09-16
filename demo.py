"""Run one scenario end to end: start the agent, crash it mid-run, watch the replay.

    python demo.py --agent naive     --model stable    # the model is paid twice
    python demo.py --agent naive     --model varying   # ...and the replay diverges: RT0016
    python demo.py --agent naive     --model drifted   # ...and it stays broken until it pauses
    python demo.py --agent journaled --model varying   # the fix: one call, clean replay

Needs the Restate server from docker-compose.yml (`docker compose up -d`).
The model stub's log lines ("model call #n") print live in this terminal.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
INGRESS = os.environ.get("RESTATE_INGRESS", "http://127.0.0.1:18080")
ADMIN = os.environ.get("RESTATE_ADMIN", "http://127.0.0.1:19070")
STUB = "http://127.0.0.1:8765"
AGENT_URI_FOR_SERVER = os.environ.get("AGENT_URI", "http://host.docker.internal:9080")
QUESTION = "What is the weather in Berlin?"


BOLD = sys.stdout.isatty()
FAILURES = {"RT0010": "service unreachable", "RT0016": "journal mismatch"}


def say(line: str = "") -> None:
    print(f"\033[1m{line}\033[0m" if BOLD and line else line, flush=True)


def http(method: str, url: str, body=None, timeout: float = 10):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def sql(query: str) -> list[dict]:
    return http("POST", f"{ADMIN}/query", {"query": query})["rows"]


def wait_for(check, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if check():
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(interval)
    return False


def port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_stub(model: str) -> subprocess.Popen:
    env = dict(os.environ, MODEL_ANSWERS=model)
    proc = subprocess.Popen([PY, os.path.join(HERE, "model_stub.py")], env=env)
    if not wait_for(lambda: http("GET", f"{STUB}/stats") is not None, 10):
        sys.exit("model stub did not start")
    return proc


def start_agent(mode: str, log, port: int = 9080) -> subprocess.Popen:
    env = dict(os.environ, AGENT_MODE=mode, AGENT_PORT=str(port))
    proc = subprocess.Popen([PY, os.path.join(HERE, "agent.py")], env=env, stdout=log, stderr=log)
    if not wait_for(lambda: port_open(port), 15):
        sys.exit("agent did not start; see .demo/agent.log")
    return proc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent", choices=["naive", "journaled"], required=True)
    parser.add_argument("--model", choices=["stable", "varying", "drifted"], default="stable")
    parser.add_argument("--wait", type=float, default=60, help="seconds to watch after the restart")
    args = parser.parse_args()

    try:
        http("GET", f"{ADMIN}/health")
    except (urllib.error.URLError, OSError):
        sys.exit(f"Restate admin API not reachable at {ADMIN}. Run: docker compose up -d")
    for port in (8765, 9080):
        if port_open(port):
            sys.exit(f"port {port} is busy; stop the process using it first")

    os.makedirs(os.path.join(HERE, ".demo"), exist_ok=True)
    agent_log = open(os.path.join(HERE, ".demo", "agent.log"), "a")
    stub = agent = None
    try:
        say(f"== agent: {args.agent}   model answers: {args.model}")
        stub = start_stub(args.model)
        agent = start_agent(args.agent, agent_log)
        # force: this demo re-registers the same address with different code per scenario
        http("POST", f"{ADMIN}/deployments", {"uri": AGENT_URI_FOR_SERVER, "force": True})

        sent = http("POST", f"{INGRESS}/WeatherAgent/run/send", QUESTION)
        invocation = sent["invocationId"]
        say(f"invoked WeatherAgent/run   {invocation}")

        if not wait_for(lambda: http("GET", f"{STUB}/stats")["weather_calls"] >= 1, 20):
            sys.exit("the agent never reached the weather tool")
        time.sleep(1)  # now inside the durable rate-limit pause

        agent.send_signal(signal.SIGKILL)
        agent.wait()
        say(f"kill -9 agent (pid {agent.pid})")
        time.sleep(2)
        agent = start_agent(args.agent, agent_log)
        say(f"agent restarted (pid {agent.pid}); Restate retries and replays the journal")

        seen, row, failure = None, {}, None
        deadline = time.time() + args.wait
        while time.time() < deadline:
            rows = sql(
                "SELECT status, retry_count, last_failure_error_code, last_failure "
                f"FROM sys_invocation WHERE id = '{invocation}'"
            )
            row = rows[0] if rows else {}
            if row.get("status") in ("completed", "paused"):
                break
            code = row.get("last_failure_error_code")
            if code:
                # A paused invocation drops these fields, so keep the last one seen.
                failure = (code, row.get("last_failure") or "")
            if code and (row.get("retry_count"), code) != seen:
                seen = (row.get("retry_count"), code)
                say(f"  attempt {seen[0]}, last failure {code} ({FAILURES.get(code, 'see UI')})")
            time.sleep(0.5)

        say()
        stats = http("GET", f"{STUB}/stats")
        say(f"status: {row.get('status')}   model calls: {stats['model_calls']}   weather calls: {stats['weather_calls']}")
        if row.get("status") == "completed":
            result = http("GET", f"{INGRESS}/restate/invocation/{invocation}/output")
            say(f"result: {result}")
        elif failure:
            say(f"last failure {failure[0]}:")
            print("  " + failure[1].strip().replace("\n", "\n  "), flush=True)

        say("journal:")
        for entry in sql(
            f"SELECT index, entry_type, name FROM sys_journal WHERE id = '{invocation}' ORDER BY index"
        ):
            name = f"  {entry['name']}" if entry.get("name") else ""
            print(f"  {entry['index']:>2}  {entry['entry_type']}{name}", flush=True)
        say(f"UI: {ADMIN}/ui/   invocation {invocation}")
    finally:
        for proc in (agent, stub):
            if proc and proc.poll() is None:
                # Hypercorn shuts down on SIGINT, not SIGTERM.
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        agent_log.close()


if __name__ == "__main__":
    main()
