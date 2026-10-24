"""Opt-in failure/cancel/GPU checks against an existing integration fixture.

python -m tests.integration_lifecycle /path/printed/by/integration_orcd [--gpu]
"""

import sys
import time

from caligo import jobs
from caligo.util import run
from caligo.workspace import Project


def wait(experiment, job_id, *, running=False):
    deadline = time.monotonic() + 600
    previous = None
    while time.monotonic() < deadline:
        job = jobs.get_job(experiment, job_id)
        if job["state"] != previous:
            print(job_id, job["state"], flush=True)
            previous = job["state"]
        if job["state"] in jobs.TERMINAL or (running and job["state"] == "running"):
            return job
        time.sleep(1)
    jobs.cancel(experiment, job_id)
    raise RuntimeError("Timed out; cancellation requested")


def main():
    project = Project(sys.argv[1])
    experiment = project.experiment("trial")
    job_id = jobs.submit(
        experiment,
        "dev",
        ["python3", "-c", "import sys; print('EXPECTED_FAILURE'); sys.exit(7)"],
    )
    result = wait(experiment, job_id)
    assert result["state"] == "failed" and result["returncode"] == 7, result
    print("PASS: nonzero workload status preserved", flush=True)
    job_id = jobs.submit(
        experiment,
        "dev",
        ["python3", "-c", "import time; print('STARTED', flush=True); time.sleep(120)"],
    )
    result = wait(experiment, job_id, running=True)
    assert result["state"] == "running", result
    jobs.cancel(experiment, job_id)
    result = wait(experiment, job_id)
    assert result["state"] == "cancelled", result
    print("PASS: running Slurm workload cancelled", flush=True)
    if "--gpu" in sys.argv:
        text = (project.root / "caligo.toml").read_text()
        text = text.replace(
            'partitions = ["mit_quicktest"]', 'partitions = ["mit_preemptable"]'
        )
        text = text.replace("gpus = 0", 'gpus = 1\ngpu_type = "a100"')
        (project.root / "caligo.toml").write_text(text)
        run(["git", "add", "caligo.toml"], cwd=project.root)
        run(["git", "commit", "-m", "GPU test policy"], cwd=project.root)
        gpu = project.create("gpu-" + str(int(time.time())))
        job_id = jobs.submit(
            gpu,
            "dev",
            [
                "python3",
                "-c",
                (
                    "import os,subprocess; print('CUDA_VISIBLE_DEVICES='+os.environ.get('CUDA_VISIBLE_DEVICES','')); "
                    "assert os.environ.get('CUDA_VISIBLE_DEVICES'); "
                    "subprocess.run(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],check=True)"
                ),
            ],
        )
        result = wait(gpu, job_id)
        print((gpu.state / "jobs" / job_id / "output.log").read_text(), flush=True)
        assert result["state"] == "completed", result
        print("PASS: real GPU allocation and NVIDIA device access", flush=True)


if __name__ == "__main__":
    main()
