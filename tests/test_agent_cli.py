"""CLI -> detached service -> real container -> app-server protocol fixture."""

import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import time

import pytest

from caligo.attempts import Project
from caligo.service import service_directory
from test_agent_service import wait_for

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CALIGO_TEST_DOCKER") != "1", reason="set CALIGO_TEST_DOCKER=1"
    ),
]


def test_detached_cli_steering_dev_and_service_restart(project, tmp_path):
    if subprocess.run(
        ["docker", "image", "inspect", "caligo-agent:local"], capture_output=True
    ).returncode:
        pytest.skip("Build caligo-agent:local first")
    context = tmp_path / "image"
    context.mkdir()
    source = Path(__file__).parent / "fixtures/codex_server.py"
    (context / "codex").write_bytes(source.read_bytes())
    (context / "Dockerfile").write_text(
        "FROM caligo-agent:local\nRUN rm /usr/local/bin/codex\nCOPY --chmod=755 codex /usr/local/bin/codex\n"
    )
    subprocess.run(
        ["docker", "build", "--quiet", "-t", "caligo-test-agent:local", str(context)],
        check=True,
        capture_output=True,
    )
    path = project / "project.toml"
    path.write_text(
        path.read_text().replace(
            'outputs = "results"', 'outputs = "results"\nruntime = "docker"'
        )
        + '\n[agent]\nimage="caligo-test-agent:local"\nmodel="project-model"\n'
    )
    manager = Project(path)

    def cli(*args):
        result = subprocess.run(
            [sys.executable, "-m", "caligo", *args],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, (args, result.stdout, result.stderr)
        return result

    try:
        created = json.loads(
            cli(
                "create", "search", "--mode", "agent", "--goal", "hold", "--once"
            ).stdout
        )
        thread = created["id"]
        assert created["model"] == "project-model"

        def state():
            return manager.service("search", "GET", "/threads/" + thread)

        wait_for(lambda: state()["state"] == "RUNNING")
        before = state()["backend_id"]
        assert thread in {
            item["id"]
            for item in json.loads(cli("agent", "threads", "search").stdout)["threads"]
        }
        replay = cli("agent", "attach", "search", "--json", "--no-follow")
        assert "agent.text" in replay.stdout
        assert state()["state"] == "RUNNING"
        cli("agent", "steer", "search", "--delivery", "steer", "new direction")
        wait_for(
            lambda: (
                "steered:new direction"
                in cli("agent", "attach", "search", "--json", "--no-follow").stdout
            )
        )
        cli("agent", "interrupt", "search", "next instruction")
        work = Path(manager.find("search")["workspace"])
        wait_for(lambda: (work / "src/agent-marker").read_text() == "next instruction")
        wait_for(lambda: state()["state"] == "WAITING")
        assert (work / "src/agent-marker").read_text() == "next instruction"
        dev = subprocess.run(
            [
                sys.executable,
                "-m",
                "caligo",
                "exec",
                "--",
                "sh",
                "-c",
                "echo DEV_OUT; echo DEV_ERR >&2; exit 7",
            ],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert dev.returncode == 7, (dev.stdout, dev.stderr)
        assert "DEV_OUT" in dev.stdout and "DEV_ERR" in dev.stderr
        # Service shutdown pauses the thread, preserving both backend ID and files.
        cli("agent", "service", "stop", "search")
        controller = manager.workflow().coding.controller
        address = service_directory(controller) / "address.json"
        pid = json.loads(address.read_text())["pid"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        cli("agent", "resume", "search")
        wait_for(lambda: state()["state"] == "WAITING")
        assert state()["backend_id"] == before
        assert (work / "src/agent-marker").read_text() == "next instruction"
        cli("agent", "steer", "search", "after restart")
        wait_for(lambda: (work / "src/agent-marker").read_text() == "after restart")
        wait_for(lambda: state()["state"] == "WAITING")
        # Abrupt daemon death leaves recovery to reconcile its orphan container.
        pid = json.loads(address.read_text())["pid"]
        os.kill(pid, signal.SIGKILL)
        cli("agent", "attach", "search", "--json", "--no-follow")
        wait_for(lambda: state()["state"] == "WAITING")
        assert state()["backend_id"] == before
        assert (
            "runtime.recovered"
            in cli("agent", "attach", "search", "--json", "--no-follow").stdout
        )
        cli("agent", "stop", "search")
        wait_for(lambda: state()["state"] == "STOPPED")
        thread = json.loads(
            cli(
                "agent", "start", "search", "--goal", "new conversation", "--once"
            ).stdout
        )["id"]
        wait_for(lambda: state()["state"] == "WAITING")
        cli("agent", "stop", "search")
        wait_for(lambda: state()["state"] == "STOPPED")
        selected = manager.start_agent(
            "search",
            "codex",
            "selected model",
            continuous=False,
            model="selected-model",
        )
        thread = selected["id"]
        wait_for(lambda: state()["state"] == "WAITING")
        model_record = (
            Path(manager.find("search")["git_dir"]).parent / "agent-home/fixture-model"
        )
        assert model_record.read_text() == "selected-model"
        assert state()["model"] == "selected-model"
        cli("agent", "stop", "search")
        wait_for(lambda: state()["state"] == "STOPPED")
    finally:
        try:
            manager.service("search", "POST", "/shutdown", {})
        except Exception:
            pass
