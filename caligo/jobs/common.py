"""Controller job support."""

import json
import os
import sys
import time
from pathlib import Path

from ..transport import SSH
from ..util import identifier, locked, read_json, timestamp, write_json
from ..process import detached
from ..errors import Error

TERMINAL = {"completed", "failed", "cancelled"}


def location(experiment, job_id):
    return experiment.state / "jobs" / identifier(job_id)


def get_job(experiment, job_id):
    job = read_json(location(experiment, job_id) / "job.json")
    if job is None:
        raise Error(f"Unknown job in experiment {experiment.name}: {job_id}")
    return job


def list_jobs(experiment):
    result = []
    for path in sorted((experiment.state / "jobs").glob("*/job.json")):
        job = read_json(path)
        if job["state"] not in TERMINAL:
            ensure_watcher(experiment, job["id"])
        result.append(job)
    return result


def remote(candidate, operation, *arguments, timeout=120):
    args = [
        "python3",
        candidate["root"] + "/remote_job.py",
        operation,
        candidate["root"],
        *map(str, arguments),
    ]
    output = SSH(candidate["host"]).run(args, timeout=timeout).stdout
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise Error(
            f"Invalid response from {candidate['host']}: {output[-1000:]}"
        ) from exc


def ensure_watcher(experiment, job_id):
    import fcntl

    folder = location(experiment, job_id)
    with locked(folder / "watcher-start.lock"):
        with open(folder / "watcher.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        with open(folder / "watcher.log", "a") as log:
            process = detached(
                [
                    sys.executable,
                    "-m",
                    "caligo",
                    "--project",
                    str(experiment.project.root),
                    "--experiment",
                    experiment.name,
                    "_watch",
                    job_id,
                ],
                log,
                env,
            )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            marker = read_json(folder / "watcher.json", {})
            if marker.get("pid") == process.pid:
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise Error(
            "Could not start job monitor; inspect " + str(folder / "watcher.log")
        )


def cancel(experiment, job_id):
    job = get_job(experiment, job_id)
    if job["state"] in TERMINAL:
        return job["state"]
    folder = location(experiment, job_id)
    with locked(folder / "decision.lock"):
        write_json(folder / "cancel.json", {"time": timestamp()})
    ensure_watcher(experiment, job_id)
    return "cancellation requested"


def read_log(experiment, job_id, offset=0):
    if type(offset) is not int or offset < 0:
        raise Error("Log offset must be a nonnegative integer.")
    job = get_job(experiment, job_id)
    with open(location(experiment, job_id) / "output.log", "rb") as stream:
        stream.seek(offset)
        content = stream.read(256 * 1024)
    return {
        "text": content.decode(errors="replace"),
        "offset": offset + len(content),
        "state": job["state"],
        "returncode": job.get("returncode", 1),
    }
