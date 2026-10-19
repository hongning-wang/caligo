"""Durable agent control using a deterministic subprocess, without inference."""

import json
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from caligo.common import CaligoError
from caligo.service import Service, http_server
from test_harness import bootstrap


SCRIPT = r"""
import json, sys, threading, time
lock = threading.Lock()
active = None

def emit(kind, data):
    with lock:
        print(json.dumps({'type': kind, 'data': data}), flush=True)

def turn(text, stopped):
    emit('turn.started', {'id': 'fixture-turn'})
    emit('agent.text', {'text': text})
    if text == 'hold':
        stopped.wait(10)
    else:
        stopped.wait(0.1)
    emit('turn.completed', {'status': 'interrupted' if stopped.is_set() else 'completed'})

emit('agent.ready', {'backend_id': 'fixture-session', 'capabilities': {'steer': True}})
for line in sys.stdin:
    command = json.loads(line)
    if command['action'] == 'interrupt':
        if active:
            active[0].set()
            active[1].join()
    elif command['action'] == 'input':
        if command.get('delivery') == 'steer':
            emit('agent.text', {'text': 'steered:'+command['text']})
        else:
            stopped = threading.Event()
            thread = threading.Thread(target=turn, args=(command['text'], stopped), daemon=True)
            active = stopped, thread
            thread.start()
    emit('command.ack', {'id': command['id']})
"""


class FixtureRuntime:
    def __init__(self, controller, attempt, thread, directory):
        self.resolved = "fixture-image"
        self.thread = thread
        self.directory = directory

    def start(self):
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return self.process

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def stop(self):
        if hasattr(self, "process") and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=5)


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for agent state")


@pytest.fixture
def service(project, tmp_path, monkeypatch):
    controller = bootstrap(project, tmp_path, monkeypatch)
    attempt = controller.create("agent", "search")
    state = {
        "project": "test",
        "spec": controller.spec,
        "attempts": [attempt],
        "active_attempt": attempt["id"],
    }
    instance = Service(controller, state, runtime_factory=FixtureRuntime)
    yield instance, attempt
    instance.close()
    for worker in instance.workers.values():
        worker.join(timeout=5)


def events(service, thread, kind):
    return [event for event in service.store.events(thread) if event["type"] == kind]


def test_stream_steering_idempotency_interrupt_and_resume(service):
    service, attempt = service
    thread = service.create(attempt["id"], "codex", "hold", continuous=False)["id"]
    wait_for(lambda: service.store.thread(thread)["state"] == "RUNNING")
    first = service.message(thread, {"text": "next", "id": "same-message"})
    assert (
        service.message(thread, {"text": "next", "id": "same-message"})["id"]
        == first["id"]
    )
    with pytest.raises(CaligoError):
        service.message(thread, {"text": "different", "id": "same-message"})
    service.message(thread, {"text": "course correction", "delivery": "steer"})
    # This is delivered during the running tool, before the queued next turn.
    # Older queued input must not prevent an explicit active steering message.
    wait_for(
        lambda: any(
            "steered:" in e["data"]["text"]
            for e in events(service, thread, "agent.text")
        )
    )
    service.control(thread, "interrupt")
    wait_for(
        lambda: (
            events(service, thread, "command.ack")
            and service.store.thread(thread)["state"] == "PAUSED"
        )
    )
    service.control(thread, "resume")
    wait_for(
        lambda: any(
            e["data"].get("text") == "next"
            for e in events(service, thread, "agent.text")
        )
    )
    wait_for(lambda: service.store.thread(thread)["state"] == "WAITING")
    emitted = service.store.events(thread)
    assert len({e["seq"] for e in emitted}) == len(emitted)
    assert all(
        e["seq"] > emitted[0]["seq"]
        for e in service.store.events(thread, emitted[0]["seq"])
    )
    assert service.store.thread(thread)["backend_id"] == "fixture-session"
    service.control(thread, "stop")
    wait_for(lambda: service.store.thread(thread)["state"] == "STOPPED")


def test_writer_lease_and_restart_preserve_thread(service):
    service, attempt = service
    thread = service.create(
        attempt["id"], "claude", "first", continuous=False, model="saved-model"
    )["id"]
    wait_for(
        lambda: (
            service.store.thread(thread)["state"] == "WAITING"
            and events(service, thread, "turn.completed")
        )
    )
    with pytest.raises(CaligoError):
        service.create(attempt["id"], "codex", "other")
    with pytest.raises(CaligoError):
        service.message(thread, {"text": "change", "delivery": "steer"})
    service.close()
    service.workers[thread].join(timeout=5)
    recovered = Service(
        service.controller, service.state, runtime_factory=FixtureRuntime
    )
    try:
        assert recovered.store.thread(thread)["backend_id"] == "fixture-session"
        assert recovered.store.thread(thread)["model"] == "saved-model"
        recovered.message(thread, {"text": "second"})
        recovered.control(thread, "resume")
        wait_for(
            lambda: any(
                e["data"].get("text") == "second"
                for e in events(recovered, thread, "agent.text")
            )
        )
        assert len(events(recovered, thread, "thread.created")) == 1
    finally:
        recovered.close()
        for worker in recovered.workers.values():
            worker.join(timeout=5)


def test_http_auth_and_replayable_sse(service):
    service, attempt = service
    server = http_server(service, "test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.server_port)
    try:
        with pytest.raises(HTTPError) as rejected:
            urlopen(base + "/threads")
        assert rejected.value.code == 401
        created = service.create(attempt["id"], "codex", "first", continuous=False)[
            "id"
        ]
        wait_for(lambda: events(service, created, "agent.text"))
        all_events = service.store.events(created)
        cursor = all_events[0]["seq"]
        request = Request(
            base + "/threads/" + created + "/events",
            headers={
                "Authorization": "Bearer test-token",
                "Accept": "text/event-stream",
                "Last-Event-ID": str(cursor),
            },
        )
        with urlopen(request, timeout=5) as response:
            line = response.readline().decode()
            assert line.startswith("id: ")
            assert int(line.split(":")[1]) > cursor
        assert service.store.thread(created)["state"] != "STOPPED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_continuation_pauses_and_terminal_results_are_delivered_once(service):
    service, attempt = service
    thread = service.create(attempt["id"], "codex", "first", continuous=True)["id"]
    wait_for(lambda: len(events(service, thread, "turn.completed")) >= 2)
    service.control(thread, "pause")
    wait_for(lambda: service.store.thread(thread)["state"] == "PAUSED")
    count = len(events(service, thread, "turn.started"))
    service.store.observe_run(thread, "run-result", {"state": "SUCCEEDED"}, True)
    service.store.observe_run(thread, "run-result", {"state": "SUCCEEDED"}, True)
    service.store.observe_run(
        thread, "run-result", {"state": "SUCCEEDED", "exit_code": 0}, True
    )
    notices = [
        e
        for e in events(service, thread, "message.accepted")
        if e["data"].get("author") == "scheduler"
    ]
    assert len(notices) == 1
    time.sleep(2.1)
    assert len(events(service, thread, "turn.started")) == count
    service.control(thread, "resume")
    wait_for(
        lambda: any(
            "run-result" in e["data"]["text"]
            for e in events(service, thread, "agent.text")
        )
    )


def test_delivery_ambiguity_is_recorded_without_replay(tmp_path):
    from caligo.store import Store

    store = Store(tmp_path / "threads.sqlite3")
    thread = store.create("attempt-one", "codex", "goal", continuous=False)["id"]
    message = store.enqueue(thread, "do this once", message_id="message-once")
    store.message_state(thread, message["id"], "SENDING")
    store.recover(thread)
    assert store.pending(thread) == []
    with store.connect() as db:
        row = db.execute(
            "SELECT state FROM messages WHERE id=?", (message["id"],)
        ).fetchone()
    assert row["state"] == "DELIVERY_UNKNOWN"


def test_transient_runtime_disconnect_retries_with_saved_identity(service):
    original, attempt = service
    seen = []

    class DisconnectOnce(FixtureRuntime):
        def start(self):
            seen.append(self.thread.get("backend_id"))
            if len(seen) > 1:
                return super().start()
            script = "import json,sys; print(json.dumps({'type':'agent.ready','data':{'backend_id':'fixture-session'}}),flush=True); c=json.loads(sys.stdin.readline()); print(json.dumps({'type':'command.ack','data':{'id':c['id']}}),flush=True); print(json.dumps({'type':'agent.disconnected','data':{'retryable':True}}),flush=True)"
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-c", script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            return self.process

    instance = Service(
        original.controller, original.state, runtime_factory=DisconnectOnce
    )
    try:
        thread = instance.create(attempt["id"], "codex", "first", continuous=False)[
            "id"
        ]
        wait_for(
            lambda: (
                len(events(instance, thread, "runtime.started")) == 2
                and instance.store.thread(thread)["state"] == "WAITING"
            )
        )
        assert seen == [None, "fixture-session"]
        assert len(events(instance, thread, "runtime.retry")) == 1
        assert len(events(instance, thread, "message.accepted")) == 1
    finally:
        instance.close()
