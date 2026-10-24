"""Opt-in live CPU integration test: python -m tests.integration_orcd [SSH_HOST]."""

import json
import sys
import tempfile
import time
from pathlib import Path

from caligo import config, jobs
from caligo.util import run
from caligo.workspace import Project


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "orcd"
    root = Path(tempfile.mkdtemp(prefix="caligo-orcd-"))
    project = Project(root)
    project.init()
    text = config.STARTER
    text = text.replace(
        "hosts = []",
        "hosts = ["
        + json.dumps(host)
        + ']\npartitions = ["mit_quicktest"]\ntime = "00:03:00"',
    )
    (root / "caligo.toml").write_text(text)
    (root / "experiments").mkdir()
    (root / "experiments/rate.txt").write_text("0.1")
    (root / "evaluation").mkdir()
    (root / "evaluation/run.py").write_text("""from pathlib import Path
import json, os
for name in ['/workspace/caligo.toml', '/workspace/evaluation/run.py', '/workspace/forbidden']:
    try:
        Path(name).write_text('changed')
    except OSError:
        pass
    else:
        raise AssertionError('Protected path was writable: ' + name)
assert Path.home() == Path('/home/worker') and not (Path.home()/'.ssh').exists(), 'Host home leaked'
result = {'rate': Path('/workspace/experiments/rate.txt').read_text(), 'evaluation_fixed': True}
Path('/outputs/result.json').write_text(json.dumps(result))
print('CALIGO_ORCD_OK ' + json.dumps(result), flush=True)
""")
    run(["git", "config", "user.name", "Caligo Integration"], cwd=root)
    run(["git", "config", "user.email", "caligo-test@localhost"], cwd=root)
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "baseline"], cwd=root)
    experiment = project.create("trial")
    experiment.set_goal("Verify isolated CPU execution on ORCD.")
    (experiment.worktree / "experiments/rate.txt").write_text("0.25")
    print("Project:", root, flush=True)
    job_id = jobs.submit(experiment, "run")
    print("Job:", job_id, flush=True)
    deadline = time.monotonic() + 600
    last = None
    while time.monotonic() < deadline:
        job = jobs.get_job(experiment, job_id)
        if job["state"] != last or job.get("monitor_error"):
            print(json.dumps(job), flush=True)
            last = job["state"]
        if job["state"] in jobs.TERMINAL:
            output = (experiment.state / "jobs" / job_id / "output.log").read_text()
            print(output, flush=True)
            assert job["state"] == "completed", job
            assert "CALIGO_ORCD_OK" in output
            assert '"rate": "0.25"' in output
            assert (experiment.state / "jobs" / job_id / "artifacts.tar.gz").exists()
            print(
                "PASS: real Slurm allocation, uncommitted snapshot, read-only evaluation, private home, artifacts."
            )
            return
        time.sleep(2)
    jobs.cancel(experiment, job_id)
    raise RuntimeError("Timed out waiting for ORCD; cancellation requested")


if __name__ == "__main__":
    main()
