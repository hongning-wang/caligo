"""Exercise objective reviews through the same scoped CLI available to an agent."""

import json
import os
import subprocess
import sys
import time

import pytest

from caligo.common import CaligoError, write_json
from caligo.service import Service
from caligo.store import Store
from test_agent_service import SCRIPT, FixtureRuntime, events, wait_for

pytest_plugins = ("test_agent_service",)


@pytest.fixture
def reviewing(service):
    instance, attempt = service
    instance.controller.spec.setdefault("agent", {})["continuation_delay"] = 0.02
    runtimes = {}

    class ReviewRuntime(FixtureRuntime):
        def start(self):
            runtimes[self.thread["id"]] = self
            script = SCRIPT.replace(
                "stopped.wait(10)",
                "while not stopped.wait(0.01) and not __import__('os').path.exists(__import__('os').environ['TURN_GATE']): pass",
            ).replace("stopped.wait(0.1)", "stopped.wait(0.4)")
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-c", script],
                env={**os.environ, "TURN_GATE": str(self.directory / "finish-turn")},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            return self.process

    instance.runtime_factory = ReviewRuntime
    yield instance, attempt, runtimes


def start(reviewing, continuous=True):
    instance, attempt, runtimes = reviewing
    thread = instance.create(attempt["id"], "codex", "hold", continuous=continuous)[
        "id"
    ]
    wait_for(lambda: instance.store.thread(thread)["state"] == "RUNNING")
    return instance, thread, runtimes[thread]


def review(runtime, decision, summary="Verified the requested result", ok=True):
    result = subprocess.run(
        [
            sys.executable,
            str(runtime.directory / "bin/caligo"),
            "review",
            decision,
            summary,
        ],
        env={"CALIGO_SOCKET": str(runtime.directory / "control.sock")},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert (result.returncode == 0) == ok, result.stderr
    return json.loads(result.stdout) if ok else result.stderr


def finish(runtime):
    (runtime.directory / "finish-turn").touch()


def test_completion_waits_for_turn_stops_runtime_and_releases_writer(reviewing):
    instance, thread, runtime = start(reviewing)
    review(runtime, "complete")
    assert instance.store.thread(thread)["state"] == "RUNNING"
    assert not events(instance, thread, "goal.completed")
    finish(runtime)
    wait_for(lambda: instance.store.thread(thread)["state"] == "COMPLETED")
    instance.workers[thread].join(timeout=5)
    assert runtime.process.poll() is not None
    assert (
        events(instance, thread, "goal.completed")[0]["data"]["summary"]
        == "Verified the requested result"
    )
    assert not any(
        e["data"].get("author") == "caligo"
        for e in events(instance, thread, "message.accepted")
    )
    with pytest.raises(CaligoError):
        instance.message(thread, {"text": "late instruction"})
    with pytest.raises(CaligoError):
        instance.control(thread, "resume")
    recovered = Service(
        instance.controller, instance.state, runtime_factory=FixtureRuntime
    )
    try:
        recovered.recover()
        assert thread not in recovered.workers
        next_thread = recovered.create(
            runtime.thread["attempt"], "codex", "next", continuous=False
        )
        assert next_thread["id"] != thread
        assert Store(instance.store.path).thread(thread)["state"] == "COMPLETED"
    finally:
        recovered.close()


def test_wait_parks_agent_until_experiment_result_and_rejects_early_completion(
    reviewing, monkeypatch
):
    instance, thread, runtime = start(reviewing)
    scope = instance.scopes[thread]
    write_json(
        scope.directory / "records/run-pending/run.json",
        {"id": "run-pending", "thread": thread},
    )
    status = {"state": "RUNNING"}
    monkeypatch.setattr(scope, "status", lambda run: dict(status))
    review(runtime, "complete", ok=False)
    review(runtime, "wait", "Waiting for run-pending")
    finish(runtime)
    wait_for(lambda: instance.store.thread(thread)["state"] == "WAITING")
    time.sleep(0.3)
    assert len(events(instance, thread, "turn.started")) == 1
    status["state"] = "SUCCEEDED"
    wait_for(
        lambda: any(
            "run-pending" in e["data"].get("text", "")
            for e in events(instance, thread, "agent.text")
        )
    )
    notices = [
        e
        for e in events(instance, thread, "message.accepted")
        if e["data"].get("author") == "scheduler"
    ]
    assert len(notices) == 1


def test_queued_instruction_invalidates_completion(reviewing):
    instance, thread, runtime = start(reviewing, continuous=False)
    review(runtime, "complete")
    instance.message(thread, {"text": "Check this additional case"})
    review(runtime, "complete", ok=False)
    finish(runtime)
    wait_for(
        lambda: (
            len(events(instance, thread, "turn.completed")) == 2
            and instance.store.thread(thread)["state"] == "WAITING"
        )
    )
    assert not events(instance, thread, "goal.completed")


def test_interruption_discards_completion_review(reviewing):
    instance, thread, runtime = start(reviewing, continuous=False)
    review(runtime, "complete")
    instance.control(thread, "pause")
    wait_for(lambda: instance.store.thread(thread)["state"] == "PAUSED")
    instance.control(thread, "resume")
    time.sleep(0.2)
    assert instance.store.thread(thread)["state"] == "WAITING"
    assert not events(instance, thread, "goal.completed")


def test_blocked_review_can_resume_and_continue(reviewing):
    instance, thread, runtime = start(reviewing)
    review(runtime, "blocked", "Need a target accuracy")
    finish(runtime)
    wait_for(lambda: events(instance, thread, "goal.blocked"))
    assert (
        events(instance, thread, "goal.blocked")[-1]["data"]["summary"]
        == "Need a target accuracy"
    )
    instance.control(thread, "resume")
    wait_for(lambda: len(events(instance, thread, "turn.started")) >= 2)
    review(runtime, "continue", "Compare the final measurements")
    wait_for(lambda: len(events(instance, thread, "turn.started")) >= 3)
    instance.control(thread, "pause")
    wait_for(lambda: instance.store.thread(thread)["state"] == "PAUSED")


def test_missing_review_eventually_pauses(reviewing):
    instance, thread, runtime = start(reviewing)
    finish(runtime)
    wait_for(lambda: events(instance, thread, "goal.blocked"))
    count = len(events(instance, thread, "turn.started"))
    time.sleep(0.2)
    assert len(events(instance, thread, "turn.started")) == count
