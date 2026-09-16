# Your agent paid for that model call twice

A runnable demo of durable execution on [Restate](https://restate.dev) for AI agents. A small weather agent asks a model, calls a tool, and pauses. We kill the process mid-run with `kill -9`. When it comes back, Restate replays the invocation's journal. What happens next depends on one line: whether the model call goes through `ctx.run`.

This is an independent teaching sample by SciMigo. It is not affiliated with or endorsed by Restate.

## What's here

| File | Role |
|---|---|
| `agent.py` | The Restate service `WeatherAgent/run`. `AGENT_MODE=naive` calls the model with a plain HTTP request; `AGENT_MODE=journaled` wraps the call in `ctx.run_typed`. Nothing else differs. |
| `model_stub.py` | A stand-in for the paid model API and the weather API. It counts every call, and it runs in its own process so the counts survive the agent being killed. |
| `demo.py` | Runs one scenario end to end: start, invoke, kill, restart, then report the outcome, the call counts and the journal. `--kill-during` and `--restart-delay` choose when the kill lands and how long the agent stays down. |
| `recover.py` | Takes the stuck invocation from scenario 3 and tries each way out: resume on a fixed deployment, restart-as-new, kill. |
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

In every run the agent first decides to call the tool, calls it, and starts a 6-second durable pause. By default `demo.py` kills the agent one second into that pause and restarts it two seconds later.

| Command | What happens | Model calls (a clean run needs 2) |
|---|---|---|
| `python demo.py --agent naive --model stable` | The replay re-runs the unjournaled model call. The model gives the same decision, so replay continues and the run completes. The weather tool is **not** called again, because its result is in the journal. | **3** |
| `python demo.py --agent naive --model varying` | The re-run model call returns a *different* decision (answer directly). That contradicts the journal, which says the agent called the tool: **RT0016 journal mismatch**. Restate retries; this stub alternates, so the next retry happens to agree again, and the run completes. | **4** |
| `python demo.py --agent naive --model drifted --wait 150` | The model changed its mind for good. Every retry re-calls the model and fails with RT0016, until the handler's retry policy (20 attempts) pauses the invocation. | **18** in our run, then paused; the count depends on timing ([below](#how-many-calls-scenario-3-costs)) |
| `python demo.py --agent journaled --model varying` | The first model decision is recorded as `Run call model`. The replay returns the recorded decision without calling the model, and the run completes. | **2** |
| `python demo.py --agent journaled --model stable --kill-during model` | The kill lands while the first model call is in flight: the model has answered, but the answer never reached the journal. The replay calls the model again. | **3** |

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

The rule the demo teaches: **a replay has to issue the same Restate operations, in the same order, with the same inputs.** Anything that can come out differently on another attempt (a model answer, an HTTP response, the clock, a random number) has to be recorded before the handler branches on it: wrap model calls and network calls in `ctx.run`, and use the context's deterministic helpers (`ctx.random()`, `ctx.uuid()`, `ctx.time()`). Values that cannot change between attempts need no recording: the input, which is journal entry 0, constants, and anything computed from them. Code counts as constant only while its deployment does not change, which is why the `force: true` re-registration below is a demo shortcut. See [Durable steps](https://docs.restate.dev/develop/python/durable-steps) and [RT0016](https://docs.restate.dev/references/errors).

## What `ctx.run` does not promise

Scenario 5 kills the journaled agent one second into its first model call. The stub has decided, but holds the answer back for four seconds. Recorded in `observed/6-journaled-killed-mid-call.txt`:

```
17:55:34  model call #1  ->  {"tool": "get_weather", "city": "Berlin"}
kill -9 agent (pid 2294216), before model call #1 returned its answer
agent restarted (pid 2294655); Restate retries and replays the journal
  attempt 3, last failure RT0010 (service unreachable)
17:55:38  model call #2  ->  {"tool": "get_weather", "city": "Berlin"}
17:55:38  weather call #1  get_weather(Berlin)
17:55:38  model call #1  answer not delivered: the caller is gone
17:55:44  model call #3  ->  {"answer": "Berlin right now: 18\u00b0C and cloudy."}

status: completed   model calls: 3   weather calls: 1
```

The journal is the same ten entries as scenario 4's; nothing in it shows that a call was paid for and lost. A step counts as done once its result is in Restate's log ([Architecture](https://docs.restate.dev/references/architecture)); an attempt that dies before then runs the step again. So `ctx.run` never repeats a call that finished, and it can repeat one that was in flight. For an effect that must not happen twice, such as a payment, send an idempotency key the API honours ([Sagas](https://docs.restate.dev/guides/sagas)).

## How many calls scenario 3 costs

The drifted run paused after 18 model calls. That is this machine's number, not the demo's. The retry policy allows 20 attempts, and every attempt Restate makes while the agent is down fails with RT0010 without calling the model. How many fall in that window depends on how long the agent takes to come back. Same scenario, changing only `--restart-delay` (recorded in `observed/7-naive-drifted-restart-delay.txt`):

| `--restart-delay` | 0.2 s | 2 s (default) | 5 s | 9 s |
|---|---|---|---|---|
| Model calls before the pause | 20 | 18 | 16 | 15 |

The other scenarios' counts don't depend on timing: their extra calls happen on the first replay that reaches the agent, however many attempts it took to get there.

## Measured on

- `docker.restate.dev/restatedev/restate:1.7.10`
- `restate-sdk` 1.0.5 and `hypercorn` 0.18.0 on Python 3.12.3
- Linux, 2026-09-16

Timings and attempt numbers will vary. The outcomes, and the call counts of scenarios 1, 2, 4 and 5, reproduced on every run; scenario 3's count depends on timing (see above).

## Recovering a stuck invocation

`python recover.py` recreates scenario 3's paused invocation, then deploys the fixed (journaled) agent as a second deployment on :9081. Recorded in `observed/5-recover.txt`:

| Step | Result |
|---|---|
| `PATCH /invocations/{id}/resume?deployment=<fixed deployment>` | Accepted (200), and the invocation is re-pinned to the fixed deployment. It still fails with RT0016, because the fixed code's first step is `Run call model` while the recorded journal has `Run get_weather` at index 1. No model calls are made. |
| `PATCH /invocations/{id}/restart-as-new` while paused | Refused: 409, "The invocation … is still running." |
| `PATCH /invocations/{id}/kill`, then `restart-as-new` | The original completes as a failure (`[409] killed`). The new invocation starts from the original input on the latest deployment and completes with 2 model calls. |

Resuming without changing deployments behaves like the stuck retries: every attempt calls the model again and fails with RT0016.

The error for the resumed invocation is a same-type mismatch, and it names the difference directly:

```
[570 Journal mismatch] Found a mismatch between the code paths taken during the previous execution and the paths taken during this execution.
This typically happens when some parts of the code are non-deterministic.
- The mismatch happened while executing 'run' (index '1')
- Difference:
   name: get_weather != call model
```

**What this means:** a resume can only rescue an invocation when the new code is *journal-compatible*, meaning its replay issues the operations already recorded, in the same order. [Versioning](https://docs.restate.dev/services/versioning) lists a bug fixed inside a `ctx.run` as safe, and adding, removing or reordering operations as unsafe. This fix adds `Run call model` in front of the recorded `Run get_weather`, so however correct it is, it cannot replay this journal. The way out here is to kill the invocation and restart it as new, which re-runs everything from the input, including the tool call. The cheaper fix is to put the model call in `ctx.run` before anything gets stuck.

## Observations

- **The type-mismatch message swaps its two labels.** On the drifted run the SDK reports:

  ```
  - The previous execution ran and recorded the following: 'handler return' (index '1')
  - The current execution attempts to perform the following: 'run'
  ```

  The recorded journal has `Run get_weather` at index 1; it was this attempt that tried to return. The source agrees, in `restatedev/sdk-shared-core` v7.0.3, the core `restate-sdk` 1.0.5 is built on:
  - **Replay:** `PopJournalEntry` (`src/vm/transitions/journal.rs`) pops the recorded command and calls it `actual`. The command the handler is issuing now is `expected`.
  - **The call:** when the types differ, `RawMessage::decode_to` (`src/service_protocol/encoding.rs:80`) builds `CommandTypeMismatchError::new(index, <recorded type>, <current type>)`, filling `actual` and `expected` that way.
  - **The formatter:** `Display` for `CommandTypeMismatchError` (`src/vm/errors.rs:201-213`) prints `expected` as "previous execution ran and recorded" and `actual` as "current execution attempts".

  `main` has the same code as of 2026-09-16. The same-type message above (`CommandMismatchError`) prints a diff instead and is not affected. This hasn't been reported upstream.
- **A paused invocation's failure moves to its journal events.** Once paused, `sys_invocation.last_failure` and `last_failure_error_code` are empty. The failure that caused the pause is kept on the invocation's `Paused` event in `sys_journal_events`, next to one `TransientError` event per failed attempt, and `demo.py` reads it from there.
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
