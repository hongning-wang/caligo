"""Stage validated snapshots and submit candidate allocations."""

import hashlib
import shlex
import uuid
from pathlib import Path

from ..transport import SSH
from ..util import locked, timestamp, write_json
from ..errors import Error

from .common import remote, ensure_watcher

HELPER = Path(__file__).resolve().parent.parent / "remote_job.py"


def submit(experiment, phase, command=None):
    if phase not in ("dev", "run"):
        raise Error("Only dev and run jobs may be submitted.")
    section = experiment.config[phase]
    if not section["hosts"]:
        raise Error(f"Configure {phase}.hosts before submitting jobs.")
    command = experiment.config["run"]["command"] if phase == "run" else command
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(arg, str) or "\0" in arg for arg in command)
    ):
        raise Error("Provide an executable and arguments for the dev job.")
    job_id = "job-" + uuid.uuid4().hex[:12]
    with locked(experiment.state / "snapshot.lock"):
        folder = experiment.snapshot(job_id)
    job = {
        "id": job_id,
        "experiment": experiment.name,
        "phase": phase,
        "command": command,
        "state": "submitting",
        "created": timestamp(),
        "candidates": [],
    }
    with locked(folder / "submission.lock"):
        write_json(folder / "job.json", job)
        (folder / "output.log").touch()
        try:
            for host in dict.fromkeys(section["hosts"]):
                candidate = stage_candidate(
                    experiment, folder, job_id, phase, command, host
                )
                job["candidates"].append(candidate)
                write_json(folder / "job.json", job)
                candidate.update(remote(candidate, "prepare", timeout=900))
                write_json(folder / "job.json", job)
            job["state"] = "queued"
            write_json(folder / "job.json", job)
            ensure_watcher(experiment, job_id)
            return job_id
        except Exception as exc:
            for candidate in job["candidates"]:
                try:
                    remote(candidate, "cancel")
                except Error as cancel_error:
                    job.setdefault("cleanup_errors", []).append(str(cancel_error))
            job.update(state="failed", error=str(exc), finished=timestamp())
            write_json(folder / "job.json", job)
            raise


def stage_candidate(experiment, folder, job_id, phase, command, host):
    """Upload source, standalone helpers, and job specification to one host."""
    ssh = SSH(host)
    script = "python3 -c " + shlex.quote(
        "import os; p=os.environ.get('CALIGO_SCRATCH') or os.path.expanduser('~/orcd/scratch'); "
        "p=p if os.path.isdir(p) else os.path.expanduser('~/.cache'); "
        "print(os.path.realpath(p))"
    )
    base = ssh.script(script).stdout.strip()
    if not base.startswith("/") or "\n" in base or any(c in base for c in ",:"):
        raise Error(f"Invalid scratch directory from {host}")
    host_id = hashlib.sha256(host.encode()).hexdigest()[:8]
    root = base + "/caligo/jobs/" + job_id + "-" + host_id
    candidate = {"host": host, "root": root, "offset": 0}
    ssh.script("mkdir -p " + shlex.quote(root))
    ssh.put(folder / "source.tar.gz", root + "/source.tar.gz")
    ssh.put(HELPER, root + "/remote_job.py")
    for name in ("image_cache.py", "apptainer.py"):
        ssh.put(HELPER.with_name(name), root + "/" + name)
    spec = {
        "id": job_id,
        "phase": phase,
        "command": command,
        "config": experiment.config,
    }
    write_json(folder / "spec.json", spec)
    ssh.put(folder / "spec.json", root + "/spec.json")
    return candidate
