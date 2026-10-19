"""Real container boundaries, scoped broker, and native backend handshakes."""

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

import pytest

from caligo import containers, runtime
from caligo.common import run
from caligo.service import Service
from caligo.session import session
from test_agent_service import wait_for, events
from test_harness import bootstrap

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CALIGO_TEST_DOCKER") != "1", reason="set CALIGO_TEST_DOCKER=1"
    ),
]

SCRIPT = r"""
import json, os, subprocess, sys
from pathlib import Path

def emit(kind,data):
    print(json.dumps({'type':kind,'data':data}),flush=True)

workspace=Path(os.environ['CALIGO_WORKSPACE'])
assert not Path(os.environ['HOST_SENTINEL']).exists()
assert not Path('/fixed').exists()
assert not Path('/var/run/docker.sock').exists()
for path in (workspace/'data/values.json',workspace/'undeclared',workspace/'src/locked.txt'):
    try:
        path.write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('Writable protected path '+str(path))
(workspace/'src/from-agent.txt').write_text('persistent')
subprocess.run(['git','status','--short'],check=True,stdout=subprocess.DEVNULL)
emit('agent.ready',{'backend_id':'container-fixture','capabilities':{'steer':True}})
for line in sys.stdin:
    command=json.loads(line)
    if command['action']=='input':
        emit('turn.started',{'id':'container-turn'})
        if command['text']=='dev':
            program="from pathlib import Path; import os; w=Path(os.environ['CALIGO_WORKSPACE']); assert not Path(%r).exists(); assert not Path('/fixed').exists(); print('DEV_ISOLATED',flush=True); raise SystemExit(7)" % os.environ['HOST_SENTINEL']
            result=subprocess.run([os.environ['CALIGO_CLIENT'],'exec','--','python3','-c',program],capture_output=True,text=True)
            assert result.returncode==7,(result.stdout,result.stderr)
            emit('agent.text',{'text':result.stdout})
        elif command['text']=='scope':
            for action in (['create','escape','--mode','human'],['switch','other'],['fork','search','escape'],['delete','search','--yes']):
                result=subprocess.run([os.environ['CALIGO_CLIENT'],*action],capture_output=True,text=True)
                assert result.returncode!=0
            emit('agent.text',{'text':'SCOPED'})
        else:
            emit('agent.text',{'text':'BOUNDARY_OK'})
        emit('turn.completed',{'status':'completed'})
    emit('command.ack',{'id':command['id']})
"""


class DockerFixture(containers.Container):
    def start(self):
        hidden = self.controller.root.parent / "host-secret"
        broker = json.loads((self.directory / "broker.json").read_text())
        environment = {
            "HOST_SENTINEL": str(hidden),
            "CALIGO_CLIENT": "/control/bin/caligo",
            "CALIGO_BROKER_URL": broker["url"],
            "CALIGO_BROKER_TOKEN": broker["token"],
        }
        argv = containers.command(
            self.controller.spec,
            self.attempt,
            self.resolved,
            ["python3", "-u", "-c", SCRIPT],
            [(self.directory, "/control", True)],
            environment,
            self.name,
        )
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return self.process


@pytest.fixture
def setup(project, tmp_path, monkeypatch):
    (project / "src/locked.txt").write_text("protected")
    (project / "src/locked.txt").chmod(0o444)
    path = project / "project.toml"
    path.write_text(path.read_text() + '\n[agent]\nimage="python:3.12-bookworm"\n')
    controller = bootstrap(project, tmp_path, monkeypatch)
    resolved = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "python:3.12-bookworm"]
    )
    monkeypatch.setattr(runtime, "resolve_image", lambda *args: resolved)
    (tmp_path / "host-secret").write_text("never mounted")
    attempt = controller.create("agent", "search")
    state = {
        "project": "test",
        "spec": controller.spec,
        "attempts": [attempt],
        "active_attempt": attempt["id"],
    }
    return controller, attempt, state


def test_real_boundary_and_containerized_dev_broker(setup):
    controller, attempt, state = setup
    service = Service(controller, state, runtime_factory=DockerFixture)
    try:
        thread = service.create(attempt["id"], "codex", "dev", continuous=False)["id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if events(service, thread, "turn.completed"):
                break
            if service.store.thread(thread)["state"] == "FAILED":
                pytest.fail(json.dumps(service.store.events(thread), indent=2))
            time.sleep(0.1)
        assert events(service, thread, "turn.completed"), json.dumps(
            service.store.events(thread), indent=2
        )
        assert "DEV_ISOLATED" in "".join(
            event["data"]["text"] for event in events(service, thread, "agent.text")
        )
        assert (
            Path(attempt["workspace"]) / "src/from-agent.txt"
        ).read_text() == "persistent"
        service.message(thread, {"text": "scope"})
        wait_for(
            lambda: any(
                e["data"].get("text") == "SCOPED"
                for e in events(service, thread, "agent.text")
            )
        )
    finally:
        service.close()
        for worker in service.workers.values():
            worker.join(timeout=15)


@pytest.mark.parametrize("backend", ["codex", "claude"])
def test_native_backend_handshake_without_inference(setup, backend):
    controller, attempt, state = setup
    if subprocess.run(
        ["docker", "image", "inspect", "caligo-agent:local"], capture_output=True
    ).returncode:
        pytest.skip("Build caligo/Agent.Dockerfile as caligo-agent:local")
    spec = controller.spec
    spec["agent"]["image"] = "caligo-agent:local"
    from caligo.common import write_json

    write_json(controller.project / "project.json", spec)
    service = Service(controller, state)
    wf = service.scoped_workflow({"id": "thread-handshake", "attempt": attempt["id"]})
    with session(wf, attempt["id"], None) as directory:
        process = containers.Container(
            controller,
            attempt,
            {"id": "thread-handshake", "backend": backend},
            directory,
        )
        try:
            child = process.start()
            ready = False
            output = []
            inbox = queue.Queue()

            def receive():
                for line in child.stdout:
                    inbox.put(json.loads(line))

            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    event = inbox.get(timeout=0.2)
                except queue.Empty:
                    continue
                output.append(event)
                if event["type"] == "agent.ready":
                    ready = True
                    break
                if event["type"] == "agent.error":
                    break
            if not ready:
                process.stop()
            assert ready, (output, child.stderr.read() if not ready else "")
            process.send({"action": "stop", "id": "stop-handshake"})
            child.wait(timeout=10)
            assert child.returncode == 0, child.stderr.read()
        finally:
            process.stop()
