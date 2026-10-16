from pathlib import Path
import json
import os
import sys
import tarfile

import pytest

from caligo import manifest, runtime, scheduler
from caligo.cli import prepare
from caligo.common import CaligoError, run
from caligo.remote import Controller
from caligo.session import session


def bootstrap(project, tmp_path, monkeypatch):
    spec = manifest.load(project / "project.toml")
    spec["runtime"] = "docker"
    stage = tmp_path / "stage"
    prepare(project / "project.toml", spec, stage)
    archive = tmp_path / "project.tar"
    with tarfile.open(archive, "w") as tar:
        for p in stage.iterdir():
            tar.add(p, arcname=p.name)
    monkeypatch.setattr(runtime, "resolve_image", lambda *args: "sha256:test-image")
    controller = Controller(tmp_path / "controller", "test")
    controller.bootstrap(archive)
    return controller


def test_real_worktrees_and_no_fixed_history(project, tmp_path, monkeypatch):
    # Even previously committed fixed setup must not enter the remote history.
    (project / "fixed/secret.txt").write_text("hidden-fixed-secret")
    run(["git", "init", str(project)])
    run(["git", "-C", str(project), "add", "."])
    run(["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@test", "commit", "-m", "private source history"])
    c = bootstrap(project, tmp_path, monkeypatch)
    a, b = c.create("supervisor", "setup"), c.create("user", "baseline")
    wa, wb = Path(a["workspace"]), Path(b["workspace"])
    assert {p.name for p in wa.iterdir()} == {"src", "data", "executable"}
    worktrees = run(c.git_command(a) + ["worktree", "list", "--porcelain"])
    assert str(wb) not in worktrees and worktrees.count("\nHEAD ") == 1
    assert a["git_dir"] != b["git_dir"]
    (wa / "src/model.py").write_text("modified")
    (wa / "data/values.json").write_text("[]")
    assert (wb / "src/model.py").read_text() != "modified"
    assert (wb / "data/values.json").read_text() != "[]"
    assert not (wa / "fixed").exists()
    history = run(["git", "--git-dir", str(c.project / "repo.git"), "log", "--all", "--format=", "--name-only"])
    assert "fixed" not in history
    assert "project.toml" not in history


def test_freezes_dirty_ignored_files_and_default_override(project, tmp_path, monkeypatch):
    c = bootstrap(project, tmp_path, monkeypatch)
    env = c.create("user", "baseline")
    work = Path(env["workspace"])
    (work / "src/model.py").write_text("dirty code")
    (work / "src/.gitignore").write_text("ignored.py\n")
    (work / "src/ignored.py").write_text("untracked but submitted")
    (work / "executable/other.py").write_text("print('override')")
    submissions = []
    monkeypatch.setattr(scheduler, "submit", lambda *args: submissions.append(args) or "123")
    # Exercise the command a user types inside a live sandbox session.
    with session(c, env["id"], "/agent.pyz") as directory:
        first = json.loads(run([sys.executable, str(directory / "bin/caligo"), "run"],
                               env={**os.environ, "CALIGO_SOCKET": str(directory / "control.sock")}))
    second = c.submit(env["id"], "executable/other.py", "/agent.pyz")
    assert first["id"] != second["id"]
    assert first["script"] == "executable/train.py"
    assert second["script"] == "executable/other.py"
    other = c.create("user", "other")
    c.submit(other["id"], None, "/agent.pyz")
    assert {item["id"] for item in c.list(env["id"])["runs"]} == {first["id"], second["id"]}
    assert first["worktree"] == str(work)
    assert first["git"]["branch"] == env["branch"]
    assert "src/model.py" in first["git"]["status"]
    (work / "src/model.py").write_text("subsequent edit")
    frozen = c.run_dir(first["id"])
    assert (frozen / "inputs/src/model.py").read_text() == "dirty code"
    assert (frozen / "inputs/src/ignored.py").read_text() == "untracked but submitted"
    assert not (frozen / "inputs/.git").exists()
    assert not (frozen / "fixed").exists()
    assert "dirty" not in run(c.git_command(env) + ["show", "HEAD:src/model.py"])


@pytest.mark.parametrize("gpu", [None, "h200:4"])
def test_failure_evidence_and_poll_error(project, tmp_path, monkeypatch, gpu):
    if gpu:
        path = project / "project.toml"
        path.write_text(path.read_text().replace("cpu = 1", 'cpu = 1\ngpu = "' + gpu + '"'))
    c = bootstrap(project, tmp_path, monkeypatch)
    env = c.create("user", "baseline")
    monkeypatch.setattr(scheduler, "submit", lambda *args: "42")
    result = c.submit(env["id"], None, "/agent.pyz")
    def command(*args, **kwargs):
        assert kwargs["gpu"] is bool(gpu)
        return ["sh", "-c", "exit 7"]
    monkeypatch.setattr(runtime, "command", command)
    assert c.execute(result["id"]) == 7
    def error(*args):
        raise CaligoError("temporary accounting failure")
    monkeypatch.setattr(scheduler, "status", error)
    status = c.status(result["id"])
    assert status["state"] == "FAILED"
    assert status["exit_code"] == 7
    assert "poll_error" in status
    assert (c.run_dir(result["id"]) / "inputs/executable/train.py").exists()
