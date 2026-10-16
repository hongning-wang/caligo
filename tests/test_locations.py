"""Exercise workspaces and execution across independently selected hosts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from caligo import manifest, runtime, scheduler, workflow
from caligo.cli import init, prepare
from caligo.common import CaligoError, read_json, run


def create_workflow(project, tmp_path, locations, monkeypatch):
    spec = manifest.load(project / "project.toml")
    spec["locations"] = locations
    spec["runtime"] = "docker"
    state = {"version": 2, "project": "project-test", "spec": spec, "attempts": []}
    # Separate real filesystem roots emulate the hosts; only SSH transport is
    # replaced. Worktrees, snapshots, dev programs and transfers run for real.
    endpoint = workflow.Endpoint
    class FilesystemHost(endpoint):
        def __init__(self, location, root, project_id, remote):
            super().__init__("local", tmp_path / "hosts" / location, project_id, remote)
            self.location = location
    monkeypatch.setattr(workflow, "Endpoint", FilesystemHost)
    flow = workflow.Workflow(state, tmp_path / "client")
    stage = tmp_path / "stage"
    prepare(project / "project.toml", spec, stage)
    archive = tmp_path / "project.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in stage.iterdir():
            tar.add(path, arcname=path.name)
    env = flow.create(archive, "user", name="baseline")
    state["attempts"].append(env)
    state["active_attempt"] = env["id"]
    return flow, env


def test_dev_copy_and_experiment_route_are_independent(project, tmp_path, monkeypatch):
    flow, attempt = create_workflow(project, tmp_path, {"coding": "code", "dev": "dev", "experiments": "cluster"}, monkeypatch)
    coding = Path(attempt["workspace"])
    (coding / "src/model.py").write_text("def predict(x): return x * 4\n")
    submissions = []
    monkeypatch.setattr(runtime, "resolve_image", lambda *args: "sha256:cluster-image")
    monkeypatch.setattr(scheduler, "submit", lambda spec, script, log, name: submissions.append(Path(script)) or "123")
    output = {"stdout": b"", "stderr": b""}
    def emit(channel, data):
        output[channel] += data
    command = ["bash", "-c", "pwd; python3 train.py; echo dev-edit > ../src/model.py; echo failure >&2; exit 7"]
    assert flow.exec_command(attempt["id"], command, emit) == 7
    assert b"/dev/" in output["stdout"]
    assert b"[4, 8, 12]" in output["stdout"] and b"failure" in output["stderr"]
    assert (coding / "src/model.py").read_text() == "def predict(x): return x * 4\n"
    assert submissions == []
    dev_record = read_json(next((flow.directory / "dev-sessions").glob("*.json")))
    assert dev_record["exit_code"] == 7
    assert not (Path(dev_record["workspace"]).parent.parent / "fixed/eval.py").exists()
    run_record = flow.submit(attempt["id"], None)
    assert len(submissions) == 1 and "/cluster/" in str(submissions[0])
    assert run_record["attempt"] == "baseline"
    assert run_record["project"] == flow.spec["name"]
    frozen = submissions[0].parent / "inputs/src/model.py"
    (coding / "src/model.py").write_text("later edit")
    assert "x * 4" in frozen.read_text()


def test_dev_copy_is_separate_even_on_same_host(project, tmp_path, monkeypatch):
    flow, attempt = create_workflow(project, tmp_path, {"coding": "same", "dev": "same", "experiments": "cluster"}, monkeypatch)
    coding = Path(attempt["workspace"])
    original = (coding / "src/model.py").read_text()
    assert flow.exec_command(attempt["id"], ["bash", "-c", "echo changed > ../src/model.py"]) == 0
    assert (coding / "src/model.py").read_text() == original


def test_locations_must_all_be_chosen_before_creation(tmp_path):
    init(tmp_path)
    path = tmp_path / "project.toml"
    with pytest.raises(CaligoError, match="explicitly specify"):
        manifest.load(path)
    text = path.read_text().replace('# coding = "local"', 'coding = "local"').replace('# dev = "local"', 'dev = "local"')
    path.write_text(text)
    with pytest.raises(CaligoError, match="explicitly specify"):
        manifest.load(path)
