"""A small weather agent on Restate: ask the model, maybe call a tool, answer.

    AGENT_MODE=naive      the model call is a plain HTTP request inside the handler
    AGENT_MODE=journaled  the model call goes through ctx.run_typed, so its result
                          is recorded in the invocation's journal

Everything else is identical. After the tool call the agent waits a few seconds
(a durable ctx.sleep, standing in for a rate-limit pause); the demo kills this
process during that wait, and Restate replays the handler when it comes back.
"""

import asyncio
import json
import os
import urllib.request
from datetime import timedelta

import restate
from restate.retry_policy import InvocationRetryPolicy

AGENT_MODE = os.environ.get("AGENT_MODE", "journaled")
STUB = os.environ.get("MODEL_STUB_URL", "http://127.0.0.1:8765")
PAUSE = timedelta(seconds=float(os.environ.get("AGENT_PAUSE_SECONDS", "6")))
PORT = int(os.environ.get("AGENT_PORT", "9080"))


def post(path: str, body: dict) -> dict:
    request = urllib.request.Request(
        STUB + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


async def ask_model(messages: list[dict]) -> dict:
    return await asyncio.to_thread(post, "/model", {"messages": messages})


async def get_weather(city: str) -> str:
    result = await asyncio.to_thread(post, "/weather", {"city": city})
    return result["forecast"]


agent = restate.Service(
    "WeatherAgent",
    # Retry quickly once the process is back, so the replay is easy to watch.
    invocation_retry_policy=InvocationRetryPolicy(
        initial_interval=timedelta(milliseconds=500),
        exponentiation_factor=1.5,
        max_interval=timedelta(seconds=2),
        max_attempts=20,
        on_max_attempts="pause",
    ),
)


@agent.handler()
async def run(ctx: restate.Context, question: str) -> str:
    messages = [{"role": "user", "content": question}]

    for _ in range(4):
        if AGENT_MODE == "naive":
            decision = await ask_model(messages)  # not journaled: re-runs on every replay
        else:
            decision = await ctx.run_typed("call model", ask_model, messages=messages)

        if "answer" in decision:
            return decision["answer"]

        forecast = await ctx.run_typed("get_weather", get_weather, city=decision["city"])
        messages.append({"role": "tool", "content": forecast})
        await ctx.sleep(PAUSE, name="rate-limit pause")

    raise restate.TerminalError("agent did not finish in 4 steps")


app = restate.app(services=[agent])

if __name__ == "__main__":
    import hypercorn.asyncio
    import hypercorn.config

    config = hypercorn.config.Config()
    config.bind = [f"0.0.0.0:{PORT}"]
    print(f"WeatherAgent on :{PORT}, AGENT_MODE={AGENT_MODE}", flush=True)
    asyncio.run(hypercorn.asyncio.serve(app, config))
