#!/usr/bin/env python3
"""Deterministic app-server protocol fixture. Never contacts a model provider."""

import json
import os
from pathlib import Path
import sys
import threading
import uuid

lock = threading.Lock()
active = None
identity = None


def write(message):
    with lock:
        print(json.dumps(message), flush=True)


def notify(method, params):
    write({"method": method, "params": params})


def complete(turn, text, stopped):
    notify("turn/started", {"turn": {"id": turn}})
    (Path(os.environ["CALIGO_WORKSPACE"]) / "src/agent-marker").write_text(text)
    notify("item/agentMessage/delta", {"delta": text})
    if text == "hold":
        stopped.wait(60)
    else:
        stopped.wait(0.1)
    notify(
        "turn/completed",
        {
            "turn": {
                "id": turn,
                "status": "interrupted" if stopped.is_set() else "completed",
            }
        },
    )


for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    params = message.get("params", {})
    result = {}
    if method in {"thread/start", "thread/resume"}:
        guide = (Path(os.environ["CALIGO_WORKSPACE"]) / "Agent.MD").read_text()
        assert guide in params["developerInstructions"]
        (Path.home() / "fixture-model").write_text(params.get("model") or "")
        saved = Path.home() / "fixture-thread"
        if method == "thread/resume":
            identity = saved.read_text()
            assert params["threadId"] == identity
        else:
            identity = "fixture-" + uuid.uuid4().hex
            saved.write_text(identity)
        result = {"thread": {"id": identity}}
    elif method == "turn/start":
        turn = "turn-" + uuid.uuid4().hex
        stopped = threading.Event()
        active = (
            stopped,
            threading.Thread(
                target=complete,
                args=(turn, params["input"][0]["text"], stopped),
                daemon=True,
            ),
        )
        active[1].start()
        result = {"turn": {"id": turn}}
    elif method == "turn/steer":
        notify(
            "item/agentMessage/delta",
            {"delta": "steered:" + params["input"][0]["text"]},
        )
        result = {"turnId": params["expectedTurnId"]}
    elif method == "turn/interrupt":
        if active:
            active[0].set()
            active[1].join()
    write({"id": message["id"], "result": result})
