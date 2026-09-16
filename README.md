# Your agent paid for that model call twice

A runnable demo of durable execution on [Restate](https://restate.dev) for AI agents. A small weather agent asks a model, calls a tool, and pauses. We kill the process mid-run with `kill -9`. When it comes back, Restate replays the invocation's journal. What happens next depends on one line: whether the model call goes through `ctx.run`.

This is an independent teaching sample by SciMigo. It is not affiliated with or endorsed by Restate.

## What's here

| File | Role |
|---|---|
| `agent.py` | The Restate service `WeatherAgent/run`. `AGENT_MODE=naive` calls the model with a plain HTTP request; `AGENT_MODE=journaled` wraps the call in `ctx.run_typed`. Nothing else differs. |
| `model_stub.py` | A stand-in for the paid model API and the weather API. It counts every call, and it runs in its own process so the counts survive the agent being killed. |
| `demo.py` | Runs one scenario end to end: start, invoke, kill, restart, then report the outcome, the call counts and the journal. |
| `docker-compose.yml` | Restate server, pinned to 1.7.10. |
| `observed/` | Output of each scenario, exactly as recorded. |

## Setup

Requires Docker and Python 3.10+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
docker compose up -d        # Restate: ingress on :18080, admin API and UI on :19070
```

The ports are moved off Restate's defaults (8080, 9070) to avoid clashing with anything already running. Point `demo.py` elsewhere with `RESTATE_INGRESS` and `RESTATE_ADMIN`. The Restate UI is at <http://localhost:19070/ui/>.

The server keeps no volume, so `docker compose down` resets it.

## The scenarios

In every run the agent first decides to call the tool, calls it, and starts a 6-second durable pause. `demo.py` kills the agent one second into that pause and restarts it two seconds later.

| Command | What happens | Model calls (a clean run needs 2) |
|---|---|---|
| `python demo.py --agent naive --model stable` | The replay re-runs the unjournaled model call. The model gives the same decision, so replay continues and the run completes. The weather tool is **not** called again, because its result is in the journal. | **3** |
| `python demo.py --agent naive --model varying` | The re-run model call returns a *different* decision (answer directly). That contradicts the journal, which says the agent called the tool: **RT0016 journal mismatch**. Restate retries; this stub alternates, so the next retry happens to agree again, and the run completes. | **4** |
| `python demo.py --agent naive --model drifted --wait 150` | The model changed its mind for good. Every retry re-calls the model and fails with RT0016, until the handler's retry policy (20 attempts) pauses the invocation. | **18**, then paused |
| `python demo.py --agent journaled --model varying` | The first model decision is recorded as `Run call model`. The replay returns the recorded decision without calling the model, and the run completes. | **2** |

Journal of the naive run: there is no entry for the model call, so nothing stops it from running again.

```
 0  Command: Input
 1  Command: Run  get_weather
 2  Notification: Run
 3  Command: Sleep  rate-limit pause
 4  Notification: Sleep
 5  Command: Output
```

Journal of the journaled run:

```
 0  Command: Input
 1  Command: Run  call model
 2  Notification: Run
 3  Command: Run  get_weather
 4  Notification: Run
 5  Command: Sleep  rate-limit pause
 6  Notification: Sleep
 7  Command: Run  call model
 8  Notification: Run
 9  Command: Output
```

The rule the demo teaches: **any value your handler decides with must come from the journal.** Wrap model calls, network calls, clocks and random numbers in `ctx.run`, or use the context's deterministic helpers (`ctx.random()`, `ctx.uuid()`, `ctx.time()`). See [Durable steps](https://docs.restate.dev/develop/python/durable-steps) and [RT0016](https://docs.restate.dev/references/errors).

## Measured on

- `docker.restate.dev/restatedev/restate:1.7.10`
- `restate-sdk` 1.0.5 and `hypercorn` 0.18.0 on Python 3.12.3
- Linux, 2026-09-16

Timings and attempt numbers will vary; the call counts and outcomes reproduced on every run.

## Observations to confirm

- **The mismatch message may name the two sides the wrong way round.** On the drifted run the SDK reports:

  ```
  [570 Journal mismatch] Found a mismatch between the code paths taken during the previous execution and the paths taken during this execution.
  This typically happens when some parts of the code are non-deterministic.
   - The previous execution ran and recorded the following: 'handler return' (index '1')
   - The current execution attempts to perform the following: 'run'
  ```

  Journal entry 1 is `Run get_weather`, and the traceback in `.demo/agent.log` shows the replay failing inside `sys_write_output_success`, so the handler's return came from *this* execution. Confirm before reporting it upstream.
- **Paused invocations lose their failure details.** Once paused, `sys_invocation.last_failure` and `last_failure_error_code` are empty, so `demo.py` keeps the last values it saw.
- **RT0016 was retried, not failed immediately.** It followed the handler's retry policy (`on_max_attempts="pause"`) on this server version.

## Recording it by hand

`demo.py` is for reproducing the result. For a screen recording, three terminals read better:

```bash
# 1: the model stub, showing "model call #n" lines
MODEL_ANSWERS=stable .venv/bin/python model_stub.py

# 2: the agent
AGENT_MODE=naive .venv/bin/python agent.py

# 3: register, invoke, then kill terminal 2 during the pause and start it again
curl -s localhost:19070/deployments -H 'content-type: application/json' \
  -d '{"uri": "http://host.docker.internal:9080", "force": true}'
curl -s localhost:18080/WeatherAgent/run/send -H 'content-type: application/json' \
  -d '"What is the weather in Berlin?"'
```

`force: true` overwrites the registered deployment when the code changes between scenarios. That is a demo shortcut; in production, register a new deployment version instead ([Versioning](https://docs.restate.dev/services/versioning)).

## License

MIT © 2026 SciMigo
