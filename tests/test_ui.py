"""Browser API backed by real project files, HTTP services, and a subprocess agent."""

import io
import json
import os
import re
import sys
import tarfile
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from caligo.attempts import Project
from caligo.common import identifier, now, write_json
from caligo.service import Service, http_server, service_directory
from caligo.ui import create_server
from test_agent_service import FixtureRuntime, wait_for
from test_transport import remote as remote


def json_request(url, path, values=None, headers=None):
    req = Request(
        url + path,
        data=json.dumps(values).encode() if values is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urlopen(req, timeout=10) as response:
        return json.load(response)


@pytest.fixture
def ui(project):
    manifest = project / "project.toml"
    manifest.write_text(manifest.read_text() + '\n[agent]\nimage = "fixture"\n')
    manager = Project(manifest)
    attempt = manager.create("baseline", "agent")
    servers, services = [], []

    def register(attempt):
        workflow = manager.workflow(attempt["id"])
        controller = workflow.coding.controller
        state = dict(workflow.state, client_directory=str(workflow.directory))
        service = Service(controller, state, runtime_factory=FixtureRuntime)
        server = http_server(service, "test-token")
        directory = service_directory(controller)
        write_json(directory / "config.json", state)
        write_json(
            directory / "address.json",
            {"url": "http://127.0.0.1:%d" % server.server_port},
        )
        (directory / "token").write_text("test-token")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        services.append(service)
        return service

    service = register(attempt)
    server = create_server(manager)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d" % server.server_port
    yield manager, service, attempt, server, url, register
    server.closing.set()
    for service in services:
        service.close()
    for server in [server, *servers]:
        server.shutdown()
        server.server_close()


def test_create_steer_replay_and_close_viewer(ui):
    manager, service, _, server, url, _ = ui
    with urlopen(url + "/") as response:
        assert response.headers["Content-Type"].startswith("text/html")
        html = response.read().decode()
    assets = set(re.findall(r'(?:src|href)="(/[^\"]+\.(?:js|css)(?:\?[^\"]*)?)"', html))
    for path in assets:
        with urlopen(url + path) as response:
            assert response.read()
    created = json_request(
        url,
        "/api/agents",
        {
            "name": "search",
            "backend": "codex",
            "goal": "hold",
            "continuous": False,
            "model": "custom-model",
        },
    )
    thread_id = created["thread"]["id"]
    assert created["thread"]["model"] == "custom-model"
    from caligo.store import Store

    assert Store(service.store.path).thread(thread_id)["model"] == "custom-model"
    base = "/api/agents/%s/threads/%s" % (created["attempt"], thread_id)
    wait_for(lambda: service.store.thread(thread_id)["state"] == "RUNNING")
    agents = json_request(url, "/api/agents")
    assert {agent["name"] for agent in agents["agents"]} >= {"baseline", "search"}
    assert agents["capabilities"]["codex"]["steer"]
    instruction = {
        "text": "try regularization",
        "delivery": "steer",
        "id": "ui-instruction",
    }
    json_request(url, base + "/messages", instruction)
    json_request(url, base + "/messages", instruction)
    wait_for(
        lambda: any(
            event["data"].get("text") == "steered:try regularization"
            for event in service.store.events(thread_id)
        )
    )
    events = service.store.events(thread_id)
    accepted = [
        event
        for event in events
        if event["type"] == "message.accepted"
        and event["data"]["text"] == instruction["text"]
    ]
    assert len(accepted) == 1
    cursor = accepted[0]["seq"]
    req = Request(url + base + "/events", headers={"Last-Event-ID": str(cursor)})
    with urlopen(req, timeout=10) as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        while line := response.readline():
            if line.startswith(b"id:"):
                assert int(line[3:]) > cursor
                break
    json_request(url, base + "/pause", {})
    wait_for(lambda: service.store.thread(thread_id)["state"] == "PAUSED")
    json_request(url, base + "/resume", {})
    wait_for(lambda: service.store.thread(thread_id)["state"] == "WAITING")
    server.closing.set()
    server.shutdown()
    assert service.store.thread(thread_id)["state"] == "WAITING"
    assert manager.find("search")["id"] == created["attempt"]


def test_agents_from_multiple_revisions_and_new_threads(ui, project):
    manager, original, attempt, _, url, register = ui
    old = original.create(attempt["id"], "codex", "first revision", continuous=False)
    wait_for(lambda: original.store.thread(old["id"])["state"] == "WAITING")
    original.control(old["id"], "stop")
    wait_for(lambda: original.store.thread(old["id"])["state"] == "STOPPED")
    restarted = json_request(
        url,
        "/api/agents",
        {"attempt": attempt["id"], "goal": "another thread", "continuous": False},
    )
    assert restarted["attempt"] == attempt["id"]
    assert restarted["thread"]["id"] != old["id"]
    (project / "src/model.py").write_text("def predict(x): return x * 3\n")
    other = manager.create("new-revision", "agent")
    assert other["controller_project"] != attempt["controller_project"]
    second = register(other)
    current = second.create(other["id"], "claude", "second revision", continuous=False)
    listing = {
        agent["id"]: agent for agent in json_request(url, "/api/agents")["agents"]
    }
    assert {t["id"] for t in listing[attempt["id"]]["threads"]} >= {
        old["id"],
        restarted["thread"]["id"],
    }
    assert current["id"] in {t["id"] for t in listing[other["id"]]["threads"]}
    base = "/api/agents/%s/threads/%s" % (other["id"], current["id"])
    with pytest.raises(HTTPError) as error:
        json_request(url, base + "/messages", {"text": "steer", "delivery": "steer"})
    assert error.value.code == 400


@pytest.mark.parametrize("use_ssh", [False, True], ids=["local", "ssh-archive"])
def test_run_logs_and_download_use_coding_host_routes(
    ui, request, monkeypatch, use_ssh
):
    manager, service, attempt, _, url, _ = ui
    thread = service.create(attempt["id"], "codex", "hold", continuous=False)
    base = "/api/agents/%s/threads/%s" % (attempt["id"], thread["id"])
    controller = manager.workflow(attempt["id"]).coding.controller
    run_id = identifier("run-")
    directory = controller.record_base(controller.spec) / run_id
    directory.mkdir(parents=True)
    record = {
        "id": run_id,
        "environment": attempt["id"],
        "attempt": attempt["name"],
        "thread": thread["id"],
        "state": "FAILED",
        "script": "executable/train.py",
        "created_at": now(),
        "worktree": attempt["workspace"],
        "spec": controller.spec,
    }
    write_json(directory / "run.json", record)
    write_json(
        controller.project / "runs" / (run_id + ".json"), {"path": str(directory)}
    )
    write_json(service.workflow.run_dir(run_id) / "run.json", record)
    write_json(
        service.workflow.run_dir(run_id) / "route.json",
        {"location": "local", "project": controller.project.name},
    )
    write_json(directory / "evaluation/metrics.json", {"accuracy": 0.91})
    (directory / "logs.txt").write_text("retained output\n")
    assert json_request(url, base + "/runs")["runs"][0]["id"] == run_id
    assert json_request(url, base + "/runs/" + run_id)["state"] == "FAILED"
    assert (
        json_request(url, base + "/runs/" + run_id + "/logs")["logs"]
        == "retained output\n"
    )
    if use_ssh:
        transport, _, binaries = request.getfixturevalue("remote")
        (binaries / "python3.12").symlink_to(sys.executable)
        transport.install()
        monkeypatch.setenv("PATH", os.defpath)
        workflow = manager.workflow(attempt["id"])
        endpoint = workflow.coding
        endpoint.transport = transport
        workflow.coding_override = endpoint
        monkeypatch.setattr(manager, "workflow", lambda selected=None: workflow)
    with urlopen(url + base + "/runs/" + run_id + "/download") as response:
        assert response.headers["Content-Type"] == "application/gzip"
        with tarfile.open(fileobj=io.BytesIO(response.read()), mode="r:gz") as archive:
            assert json.load(
                archive.extractfile(run_id + "/evaluation/metrics.json")
            ) == {"accuracy": 0.91}
