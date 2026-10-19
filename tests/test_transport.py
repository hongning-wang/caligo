"""Run the real bootstrap/probe/archive code through a local SSH stand-in."""
from pathlib import Path
import base64
import os
import sys
import tarfile

import pytest

from caligo.manifest import load
from caligo.preparation import prepare
from caligo.transport import Transport
from caligo.workflow import Endpoint


@pytest.fixture
def remote(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    ssh = tmp_path / "ssh"
    ssh.write_text("#!" + sys.executable + "\nimport os, sys\nos.execv('/bin/sh', ['sh', '-c', sys.argv[-1]])\n")
    ssh.chmod(0o755)
    # Simulate the version reported by an old system interpreter, while running
    # the actual preflight script (no mock of subprocess or the probe result).
    old = binaries / "python3"
    old.write_text("#!" + sys.executable + "\nimport sys\nsys.version_info = (3, 6, 8)\nsys.version = '3.6.8'\nexec(sys.argv[2])\n")
    old.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binaries))
    transport = Transport("test-host")
    transport.ssh = [str(ssh)]
    return transport, home, binaries


def test_old_default_selects_modern_python_and_runs_shipped_agent(remote):
    transport, home, binaries = remote
    modern = binaries / "python3.12"
    modern.symlink_to(sys.executable)
    assert transport.install() == str(home)
    assert transport.python == str(modern)
    assert Path(transport.agent).is_file()
    # RPC remains usable after the default discovery name disappears.
    (binaries / "python3").unlink()
    result = transport.rpc({"action": "probe"})
    assert result["home"] == str(home)
    assert result["python"] == sys.version


@pytest.mark.parametrize("use_ssh", [False, True], ids=["local", "shipped-rpc"])
def test_project_snapshot_and_evidence_through_controller_api(
    remote, project, tmp_path, monkeypatch, use_ssh
):
    transport, _, binaries = remote
    root = str(tmp_path / "controller")
    if use_ssh:
        (binaries / "python3.12").symlink_to(sys.executable)
        transport.install()

        def rpc(action, **values):
            return transport.rpc(
                dict(root=root, project="rpc-test", action=action, **values)
            )
    else:
        rpc = Endpoint("local", root, "rpc-test", {}).rpc
    monkeypatch.setenv("PATH", os.defpath)

    spec = load(project / "project.toml")
    stage = tmp_path / "payload"
    prepare(project / "project.toml", spec, stage)
    archive = tmp_path / "project.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for item in stage.iterdir():
            tar.add(item, arcname=item.name)
    rpc("bootstrap", archive=str(archive), development=True)
    attempt = rpc("create", mode="human", name="baseline")
    assert rpc("environment", environment=attempt["id"]) == attempt

    workspace = Path(attempt["workspace"])
    (workspace / "src/model.py").write_text("edited before submission\n")
    snapshot = rpc("snapshot", environment=attempt["id"])
    run_id = snapshot["id"]
    status = rpc("status", run=run_id)
    assert status["state"] == "PREPARED"
    assert status["script"] == spec["execute"]
    assert status["inputs_sha256"] == snapshot["inputs_sha256"]
    directory = Path(status["location"])
    (workspace / "src/model.py").write_text("edited after submission\n")
    assert (directory / "inputs/src/model.py").read_text() == "edited before submission\n"

    assert rpc("logs", run=run_id) == {"logs": ""}
    (directory / "logs.txt").write_text("experiment output\n")
    assert rpc("logs", run=run_id) == {"logs": "experiment output\n"}
    tail = rpc("tail", run=run_id)
    assert base64.b64decode(tail["data"]) == b"experiment output\n"
    assert rpc("tail", run=run_id, offset=tail["offset"])["data"] == ""
    assert rpc("publish", run=run_id)["state"] == "PREPARED"

    fork = rpc("fork", environment=attempt["id"], name="variant")
    assert (Path(fork["workspace"]) / "src/model.py").read_text() == "edited after submission\n"
    rpc("delete", environment=attempt["id"])
    assert not workspace.exists()
    assert rpc("logs", run=run_id) == {"logs": "experiment output\n"}
    assert (directory / "inputs/src/model.py").read_text() == "edited before submission\n"
    assert Path(fork["workspace"]).is_dir()
