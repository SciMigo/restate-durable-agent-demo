"""What can you do with an invocation stuck on RT0016?

    python recover.py

1. Recreates the stuck invocation from scenario 3 (naive agent, drifted model):
   it retries on journal mismatch until it pauses.
2. Deploys the fixed (journaled) agent as a second deployment on :9081 and
   resumes the paused invocation on it.
3. Tries restart-as-new, kills the invocation, and restarts it as new on the
   fixed deployment.

Needs the Restate server from docker-compose.yml and free ports 8765, 9080, 9081.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

from demo import (ADMIN, AGENT_URI_FOR_SERVER, HERE, INGRESS, QUESTION, STUB, port_open, say, sql,
                  start_agent, start_stub, ui_links, wait_for)

FIXED_URI = os.environ.get("FIXED_AGENT_URI", "http://host.docker.internal:9081")


def call(method: str, url: str, body=None):
    """Like demo.http, but returns (status, body) instead of raising on 4xx."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode()


def invocation(invocation_id: str) -> dict:
    rows = sql(
        "SELECT status, retry_count, last_failure_error_code, last_failure, pinned_deployment_id, "
        f"completion_result, completion_failure FROM sys_invocation WHERE id = '{invocation_id}'"
    )
    return rows[0] if rows else {}


def model_calls() -> int:
    return call("GET", f"{STUB}/stats")[1]["model_calls"]


def journal(invocation_id: str) -> None:
    for entry in sql(f"SELECT index, entry_type, name FROM sys_journal WHERE id = '{invocation_id}' ORDER BY index"):
        name = f"  {entry['name']}" if entry.get("name") else ""
        print(f"  {entry['index']:>2}  {entry['entry_type']}{name}", flush=True)


def stop(*procs) -> None:
    for proc in procs:
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def ok(status: int) -> str:
    """Green for a 2xx answer, red for a refusal."""
    return "green" if 200 <= status < 300 else "red"


def main() -> None:
    if call("GET", f"{ADMIN}/health")[0] != 200:
        sys.exit(f"Restate admin API not reachable at {ADMIN}. Run: docker compose up -d")
    for port in (8765, 9080, 9081):
        if port_open(port):
            sys.exit(f"port {port} is busy; stop the process using it first")

    os.makedirs(os.path.join(HERE, ".demo"), exist_ok=True)
    log = open(os.path.join(HERE, ".demo", "agent.log"), "a")
    stub = naive = fixed = None
    stuck = restarted = None
    try:
        say("== 1. a stuck invocation: naive agent, drifted model", "bold")
        stub = start_stub("drifted")
        naive = start_agent("naive", log)
        _, deployment_a = call("POST", f"{ADMIN}/deployments", {"uri": AGENT_URI_FOR_SERVER, "force": True})
        stuck = call("POST", f"{INGRESS}/WeatherAgent/run/send", QUESTION)[1]["invocationId"]
        say(f"invoked {stuck} on deployment {deployment_a['id']}")
        wait_for(lambda: call("GET", f"{STUB}/stats")[1]["weather_calls"] >= 1, 20)
        time.sleep(1)
        naive.send_signal(signal.SIGKILL)
        naive.wait()
        time.sleep(2)
        naive = start_agent("naive", log)
        say("kill -9 and restart; waiting for the retries to run out", "bold", "red")
        wait_for(lambda: invocation(stuck).get("status") == "paused", 150, 0.5)
        say(f"status: {invocation(stuck).get('status')}   model calls so far: {model_calls()}", "bold", "red")

        say()
        say("== 2. deploy the fix as a new deployment and resume the paused invocation on it", "bold")
        fixed = start_agent("journaled", log, port=9081)
        _, deployment_b = call("POST", f"{ADMIN}/deployments", {"uri": FIXED_URI, "force": True})
        before = model_calls()
        status, _ = call("PATCH", f"{ADMIN}/invocations/{stuck}/resume?deployment={deployment_b['id']}")
        say(f"PATCH /invocations/{stuck}/resume?deployment={deployment_b['id']}  ->  {status}", ok(status))
        failure = {}
        wait_for(lambda: failure.update(invocation(stuck)) or "Difference" in (failure.get("last_failure") or ""), 30, 0.5)
        say(f"pinned deployment: {failure.get('pinned_deployment_id')}   last failure {failure.get('last_failure_error_code')}:")
        print("  " + (failure.get("last_failure") or "").strip().replace("\n", "\n  "), flush=True)
        say(f"model calls since resume: {model_calls() - before}")
        status, _ = call("PATCH", f"{ADMIN}/invocations/{stuck}/pause")
        wait_for(lambda: invocation(stuck).get("status") == "paused", 20, 0.5)
        say(f"PATCH pause  ->  {status}; status: {invocation(stuck).get('status')}", ok(status))

        say()
        say("== 3. restart it as new on the fixed deployment", "bold")
        status, body = call("PATCH", f"{ADMIN}/invocations/{stuck}/restart-as-new")
        say(f"PATCH restart-as-new while paused  ->  {status} {body}", ok(status))
        status, _ = call("PATCH", f"{ADMIN}/invocations/{stuck}/kill")
        wait_for(lambda: invocation(stuck).get("status") == "completed", 20, 0.5)
        original = invocation(stuck)
        say(f"PATCH kill  ->  {status}; original: {original.get('completion_result')} {original.get('completion_failure')}", ok(status))
        call("POST", f"{STUB}/reset")
        status, body = call("PATCH", f"{ADMIN}/invocations/{stuck}/restart-as-new")
        restarted = body["new_invocation_id"]
        say(f"PATCH restart-as-new after kill  ->  {status}   new invocation {restarted}", ok(status))
        wait_for(lambda: invocation(restarted).get("status") == "completed", 60, 0.5)
        row = invocation(restarted)
        output = call("GET", f"{INGRESS}/restate/invocation/{restarted}/output")[1]
        say(f"status: {row.get('status')}   pinned deployment: {row.get('pinned_deployment_id')}   model calls: {model_calls()}",
            "bold", "green" if row.get("status") == "completed" else "red")
        say(f"result: {output}", "green")
        say("journal of the new invocation:", "bold")
        journal(restarted)
        ui_links(restarted)
        say(f"original:    {ADMIN}/ui/invocations/{stuck}", "blue")
    finally:
        # Stopped early (Ctrl-C, the lab page's Stop): an invocation still retrying would reach the
        # next scenario's agent and bill its stub. Kill it, as demo.py does; a paused one stays put.
        for leftover in (stuck, restarted):
            status = invocation(leftover).get("status") if leftover else None
            if status and status not in ("completed", "paused"):
                call("PATCH", f"{ADMIN}/invocations/{leftover}/kill")
                say(f"killed invocation {leftover}: it had not finished, so it cannot retry into the next run")
        stop(naive, fixed, stub)
        log.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)  # stopped on purpose; the finally block has already cleaned up
