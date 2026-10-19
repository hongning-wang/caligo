"""Fork and delete preserve independent work and submitted experiment evidence."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from caligo.attempts import Project
from caligo.common import CaligoError, inventory, run
from caligo import runtime, scheduler


def git(attempt, *args):
    return run(
        [
            "git",
            "--git-dir",
            attempt["git_dir"],
            "--work-tree",
            attempt["workspace"],
            *args,
        ]
    )


def test_fork_preserves_dirty_git_state_permissions_and_pinned_revision(project):
    manager = Project(project / "project.toml")
    original = manager.create("baseline", "human")
    work = Path(original["workspace"])
    (work / "src/model.py").write_text("committed change\n")
    git(original, "add", "src/model.py")
    git(
        original,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@test",
        "commit",
        "-m",
        "Attempt work",
    )
    (work / "src/model.py").write_text("staged change\n")
    git(original, "add", "src/model.py")
    (work / "src/model.py").write_text("unstaged change\n")
    (work / "src/.gitignore").write_text("ignored.txt\n")
    (work / "src/ignored.txt").write_text("keep ignored content")
    (work / "src/untracked.txt").write_text("keep untracked content")
    (work / "src/empty").mkdir()
    expected = inventory(work)
    # Fork works against the source revision even if today's manifest is invalid.
    (project / "project.toml").write_text("invalid manifest")
    fork = manager.fork("baseline", "variant")
    assert fork["revision"] == original["revision"]
    assert fork["spec"] == original["spec"]
    assert fork["forked_from"] == original["id"]
    assert fork["workspace"] != original["workspace"]
    assert inventory(fork["workspace"]) == expected
    assert git(fork, "rev-parse", "HEAD") == git(original, "rev-parse", "HEAD")
    assert git(fork, "status", "--porcelain") == git(original, "status", "--porcelain")
    assert "staged change" in git(fork, "show", ":src/model.py")
    assert manager.read()["active_attempt"] == fork["id"]
    (Path(fork["workspace"]) / "src/model.py").write_text("independent")
    assert (work / "src/model.py").read_text() == "unstaged change\n"
    git(fork, "add", "src/model.py")
    assert "staged change" in git(original, "show", ":src/model.py")
    for name in ("baseline", "../escape"):
        with pytest.raises(CaligoError):
            manager.fork("baseline", name)
    assert len(manager.read()["attempts"]) == 2


def test_delete_preserves_runs_and_handles_empty_project(
    project, monkeypatch, tmp_path
):
    manifest = project / "project.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'outputs = "results"', 'outputs = "results"\nruntime = "docker"'
        )
    )
    manager = Project(project / "project.toml")
    first = manager.create("baseline", "human")
    second = manager.create("accident", "human")
    monkeypatch.setattr(runtime, "resolve_image", lambda *args: "sha256:test")
    monkeypatch.setattr(scheduler, "submit", lambda *args: "123")
    flow = manager.workflow(second["id"])
    record = flow.submit(second["id"], None)
    frozen = (
        flow.run_endpoint(record["id"]).controller.run_dir(record["id"])
        / "inputs/src/model.py"
    )
    contents = frozen.read_text()
    result = manager.delete("accident")
    assert result["runs_preserved"]
    assert not Path(second["workspace"]).exists()
    assert not Path(second["git_dir"]).parent.exists()
    assert manager.read()["active_attempt"] == first["id"]
    manager.delete("baseline")
    assert manager.read()["active_attempt"] is None
    assert manager.workflow().list_attempts()["attempts"] == []
    assert frozen.read_text() == contents
    assert manager.workflow().status(record["id"], refresh=False)["id"] == record["id"]
    with (tmp_path / "evidence.tar.gz").open("wb") as archive:
        manager.workflow().archive(record["id"], archive)
    with pytest.raises(CaligoError):
        manager.find("accident")
    recreated = manager.create("accident", "human")
    assert recreated["id"] != second["id"]
    assert manager.workflow().status(record["id"], refresh=False)["id"] == record["id"]


def test_concurrent_edits_reject_fork_without_registering_partial_attempt(
    project, monkeypatch
):
    from caligo import remote

    manager = Project(project / "project.toml")
    attempt = manager.create("baseline", "human")
    original_copy = remote.copy_tree

    def copy_then_edit(source, target):
        result = original_copy(source, target)
        if Path(source) == Path(attempt["workspace"]):
            (Path(source) / "src/model.py").write_text("concurrent edit")
        return result

    monkeypatch.setattr(remote, "copy_tree", copy_then_edit)
    with pytest.raises(CaligoError):
        manager.fork("baseline", "partial")
    assert [a["name"] for a in manager.read()["attempts"]] == ["baseline"]
    assert (
        Path(attempt["workspace"]) / "src/model.py"
    ).read_text() == "concurrent edit"


def cli(project, *args, input=None):
    return subprocess.run(
        [sys.executable, "-m", "caligo", *args],
        cwd=project,
        input=input,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_cli_help_project_option_and_delete_confirmation(project):
    manager = Project(project / "project.toml")
    manager.create("accident", "human")
    for args in ([], ["--help"], ["help"], ["help", "fork"], ["delete", "--help"]):
        result = cli(project, *args)
        assert result.returncode == 0, result.stderr
        assert "fork" in result.stdout or "delete" in result.stdout
    rejected = cli(project, "delete", "accident", input="y\n")
    assert rejected.returncode == 1
    assert manager.find("accident")
    forked = cli(
        project, "--project", str(manager.manifest), "fork", "accident", "copy"
    )
    assert forked.returncode == 0, forked.stderr
    assert json.loads(forked.stdout)["forked_from"] == manager.find("accident")["id"]
    deleted = cli(
        project, "delete", "copy", "--yes", "--project", str(manager.manifest)
    )
    assert deleted.returncode == 0, deleted.stderr
    assert json.loads(deleted.stdout)["deleted"] == "copy"
    cli(project, "delete", "accident", "--yes")
    assert cli(project, "list").returncode == 0
    for command in ("exec", "run"):
        assert cli(project, command).returncode == 1


def test_fork_and_delete_from_human_shell(project):
    from test_attempts import Terminal

    terminal = Terminal([sys.executable, "-m", "caligo", "create", "baseline"], project)
    try:
        terminal.until("caligo[baseline]")
        terminal.send("printf retained > ../src/marker", "caligo[baseline]")
        output = terminal.send("caligo fork baseline variant", "caligo[baseline]")
        assert "forked_from" in output
        manager = Project(project / "project.toml")
        assert (
            Path(manager.find("variant")["workspace"]) / "src/marker"
        ).read_text() == "retained"
        with pytest.raises(CaligoError):
            manager.delete("baseline")
        output = terminal.send(
            "caligo delete baseline --yes; echo RESULT:$?", "caligo[baseline]"
        )
        assert "RESULT:1" in output
        output = terminal.send(
            "caligo delete variant --yes; echo RESULT:$?", "caligo[baseline]"
        )
        assert "RESULT:0" in output
        assert [a["name"] for a in manager.read()["attempts"]] == ["baseline"]
    finally:
        terminal.close()
    manager.delete("baseline")
