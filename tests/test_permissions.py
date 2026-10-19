"""Human write boundaries and immutable project revisions."""

from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from caligo.attempts import Project
from caligo.permissions import human_command


@pytest.mark.skipif(
    sys.platform.startswith("linux") and not shutil.which("bwrap"),
    reason="Install bwrap for live human write enforcement",
)
def test_human_keeps_host_access_but_cannot_replace_protected_files(project, tmp_path):
    locked = project / "src/locked.txt"
    locked.write_text("keep")
    locked.chmod(0o444)
    executable = project / "src/tool.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o750)
    host = tmp_path / "normal-host-file"
    host.write_text("host access")
    manager = Project(project / "project.toml")
    attempt = manager.create("baseline", "human")
    workspace = Path(attempt["workspace"])
    assert stat.S_IMODE((workspace / "src/locked.txt").stat().st_mode) == 0o444
    assert stat.S_IMODE((workspace / "src/tool.sh").stat().st_mode) == 0o750
    probe = r"""
from pathlib import Path
import sys
w=Path(sys.argv[1]); host=Path(sys.argv[2])
assert host.read_text()=='host access'
host.write_text('ordinary host writes')
for path in (w/'data/values.json',w/'src/locked.txt'):
    for operation in (lambda:path.write_text('bad'),lambda:path.unlink(),lambda:path.chmod(0o644)):
        try: operation()
        except OSError: pass
        else: raise AssertionError('Changed protected path '+str(path))
replacement=w/'src/replacement'
replacement.write_text('bad')
try: replacement.replace(w/'src/locked.txt')
except OSError: pass
else: raise AssertionError('Replaced read-only file')
try: (w/'data').rename(w/'old-data')
except OSError: pass
else: raise AssertionError('Renamed read-only folder')
(w/'src/normal.txt').write_text('allowed')
"""
    command = human_command(
        workspace,
        attempt["spec"],
        [sys.executable, "-c", probe, str(workspace), str(host)],
    )
    subprocess.run(command, check=True)
    assert host.read_text() == "ordinary host writes"
    assert (workspace / "src/locked.txt").read_text() == "keep"


def test_new_revision_does_not_change_existing_attempt(project):
    (project / "src/empty-folder").mkdir(mode=0o750)
    manager = Project(project / "project.toml")
    first = manager.create("first", "human")
    assert (
        stat.S_IMODE((Path(first["workspace"]) / "src/empty-folder").stat().st_mode)
        == 0o750
    )
    first_flow = manager.workflow(first["id"])
    first_fixed = first_flow.coding.controller.project / "fixed/eval.py"
    original = first_fixed.read_text()
    (project / "fixed/eval.py").write_text('print("new evaluator")\n')
    (project / "src/model.py").write_text("new code")
    second = manager.create("second", "human")
    assert first["revision"] != second["revision"]
    assert first["controller_project"] != second["controller_project"]
    assert first_fixed.read_text() == original
    assert (Path(first["workspace"]) / "src/model.py").read_text() != "new code"
    assert manager.workflow(first["id"]).coding.project == first["controller_project"]
    assert manager.workflow(second["id"]).coding.project == second["controller_project"]


def test_run_route_survives_service_shutdown_and_active_revision_change(
    project, tmp_path, monkeypatch
):
    from caligo.common import write_json
    from caligo.service import service_directory
    from caligo import workflow

    manager = Project(project / "project.toml")
    first = manager.create("first", "human")
    original = manager.workflow(first["id"])
    registry = tmp_path / "offline-agent-registry"
    directory = service_directory(original.coding.controller)
    write_json(
        directory / "config.json", dict(original.state, client_directory=str(registry))
    )
    old_spec = dict(original.spec, remote={"root": "/old-shared-storage"})
    write_json(
        registry / "records/run-offline/run.json",
        {"id": "run-offline", "spec": old_spec},
    )
    write_json(
        registry / "records/run-offline/route.json",
        {"location": "cluster", "project": "old-runs"},
    )
    (project / "src/model.py").write_text("next revision")
    manager.create("second", "human")
    endpoint = workflow.Endpoint

    class RecordingEndpoint(endpoint):
        def __init__(self, location, root, project_id, remote):
            super().__init__("local", root, project_id, remote)
            self.location = location
            self.requested_remote = remote

    monkeypatch.setattr(workflow, "Endpoint", RecordingEndpoint)
    current = manager.workflow()
    selected = current.run_endpoint("run-offline")
    assert selected.project == "old-runs"
    assert selected.requested_remote["root"] == "/old-shared-storage"
    # No daemon was needed to recover the persistent route.
    assert not (directory / "address.json").exists()
