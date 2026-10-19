"""Requeue behavior with real checkpoint-writing programs and simulated Slurm."""
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from caligo import manifest, runtime, scheduler, workflow
from caligo.common import CaligoError, read_json
from test_harness import bootstrap
from test_locations import create_workflow


CHECKPOINT_PROGRAM = '''import os, signal
from pathlib import Path
checkpoint = Path(os.environ["CALIGO_OUTPUTS"]) / "checkpoint"
local = Path("checkpoint")
if not checkpoint.exists():
    checkpoint.write_text("12000")
    local.write_text("workspace survives")
    print("checkpoint saved ✓", flush=True)
    os.kill(os.getpid(), signal.SIGTERM)
assert checkpoint.read_text() == "12000"
assert local.read_text() == "workspace survives"
checkpoint.write_text("12300")
print("resumed ✓", flush=True)
'''


@pytest.mark.parametrize("enabled", [True, False])
def test_requeue_manifest_and_submission(project, tmp_path, monkeypatch, enabled):
    path = project / "project.toml"
    path.write_text(path.read_text() + "\nrequeue = " + str(enabled).lower() + "\n")
    spec = manifest.load(path)
    commands = []
    monkeypatch.setattr(scheduler, "run", lambda argv: commands.append(argv) or "42")
    scheduler.submit(spec, tmp_path / "job.sh", tmp_path / "logs.txt", "test")
    assert ("--requeue" in commands[0]) is enabled
    assert ("--no-requeue" in commands[0]) is not enabled
    assert "--open-mode=append" in commands[0]


@pytest.mark.parametrize("exit_code", [0, 7])
def test_dev_waits_through_requeue_and_keeps_checkpoint(project, tmp_path, monkeypatch, exit_code):
    path = project / "project.toml"
    path.write_text(path.read_text() + "\nrequeue = true\n")
    flow, attempt = create_workflow(project, tmp_path, {"coding": "code", "dev": "dev", "experiments": "cluster"}, monkeypatch)
    coding = Path(attempt["workspace"])
    program = CHECKPOINT_PROGRAM.replace("os.kill(os.getpid(), signal.SIGTERM)", "__import__('time').sleep(60)")
    (coding / "executable/resume.py").write_text(program + "raise SystemExit(%d)\n" % exit_code)
    submissions = []
    polls = []

    def submit(spec, script, log, name):
        submissions.append((Path(script), Path(log)))
        assert spec["scheduler"]["requeue"] is True
        assert "/dev/" in str(script)
        (coding / "executable/resume.py").write_text("raise Exception('later coding edit')")
        return "42"

    def status(job):
        polls.append(job)
        script, log = submissions[0]
        if len(polls) == 2:
            raise CaligoError("temporary scheduler outage")
        restart = 0 if len(polls) == 1 else 1
        with log.open("ab") as output:
            with subprocess.Popen(["bash", str(script)], stdout=output, stderr=output, start_new_session=True,
                                  env=dict(os.environ, SLURM_RESTART_COUNT=str(restart))) as process:
                if restart == 0:
                    deadline = time.monotonic() + 10
                    try:
                        while b"checkpoint saved" not in log.read_bytes():
                            assert process.poll() is None and time.monotonic() < deadline, log.read_text()
                            threading.Event().wait(0.01)
                    finally:
                        # Kill the controller and command together: no finally block runs.
                        os.killpg(process.pid, signal.SIGKILL)
                result = process.wait(timeout=20)
        if restart == 0:
            assert result == -signal.SIGKILL
            return {"state": "PENDING", "source": "squeue"}
        assert result == exit_code
        return {"state": "COMPLETED" if exit_code == 0 else "FAILED", "source": "sacct"}

    monkeypatch.setattr(scheduler, "submit", submit)
    monkeypatch.setattr(scheduler, "status", status)
    monkeypatch.setattr(workflow.time, "sleep", lambda seconds: None)
    output = []
    assert flow.exec_command(attempt["id"], [sys.executable, "resume.py"], lambda channel, data: output.append(data)) == exit_code
    assert len(submissions) == 1
    transcript = b"".join(output).decode()
    assert "checkpoint saved ✓" in transcript and "resumed ✓" in transcript
    directory = submissions[0][0].parent
    assert (directory / "artifacts/checkpoint").read_text() == "12300"
    assert read_json(directory / "executions/0/outcome.json")["state"] == "RUNNING"
    assert read_json(directory / "executions/1/outcome.json")["exit_code"] == exit_code
    local = read_json(flow.directory / "records" / directory.name / "run.json")
    assert local["environment"] == attempt["id"]
    assert flow.status(directory.name, refresh=False)["restart_count"] == 1
    dev_record = read_json(next((flow.directory / "dev-sessions").glob("*.json")))
    assert dev_record["exit_code"] == exit_code
    assert not (Path(dev_record["workspace"]).parent.parent / "fixed/eval.py").exists()


def test_experiment_requeue_preserves_working_files_and_outputs(project, tmp_path, monkeypatch):
    path = project / "project.toml"
    path.write_text(path.read_text().replace('eval = "eval.py"\n', '') + "\nrequeue = true\n")
    (project / "executable/train.py").write_text(CHECKPOINT_PROGRAM)
    controller = bootstrap(project, tmp_path, monkeypatch)
    attempt = controller.create("human", "baseline")
    monkeypatch.setattr(scheduler, "submit", lambda *args: "42")
    record = controller.submit(attempt["id"], None, "/agent.pyz")

    def native(spec, image, workspace, argv, **options):
        return [sys.executable, "-c", "import os,sys; os.chdir(sys.argv[1]); os.environ['CALIGO_OUTPUTS']=sys.argv[2]; os.execv(sys.argv[3], sys.argv[3:])",
                str(workspace / spec["executable"]), str(options["outputs"]), sys.executable, argv[-1]]

    monkeypatch.setattr(runtime, "command", native)
    monkeypatch.setenv("SLURM_RESTART_COUNT", "0")
    assert controller.execute(record["id"]) < 0
    monkeypatch.setattr(scheduler, "status", lambda job: {"state": "PENDING", "source": "squeue"})
    pending = controller.status(record["id"])
    assert pending["state"] == "PENDING" and pending["exit_code"] is None
    assert "finished_at" not in pending
    (Path(attempt["workspace"]) / "executable/train.py").write_text("raise Exception('later edit')")
    monkeypatch.setenv("SLURM_RESTART_COUNT", "1")
    assert controller.execute(record["id"]) == 0
    directory = controller.run_dir(record["id"])
    assert (directory / "artifacts/checkpoint").read_text() == "12300"
    assert read_json(directory / "outcome.json")["restart_count"] == 1
    assert (directory / "inputs/executable/train.py").read_text() == CHECKPOINT_PROGRAM


def test_dev_interrupt_cancels_job_without_resubmitting(tmp_path, monkeypatch):
    flow = workflow.Workflow({"spec": {}}, tmp_path)
    class Endpoint:
        def rpc(self, action, **values):
            assert action == "tail"
            return {"data": "", "offset": 0}
    monkeypatch.setattr(flow, "run_endpoint", lambda run_id: Endpoint())
    monkeypatch.setattr(flow, "status", lambda run_id: {"scheduler": {"state": "PENDING"}})
    cancelled = []
    monkeypatch.setattr(flow, "cancel", cancelled.append)
    def interrupt(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(workflow.time, "sleep", interrupt)
    assert flow.wait_dev("dev-test") == 130
    assert cancelled == ["dev-test"]
