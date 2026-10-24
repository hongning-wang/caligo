"""Snapshot submission, single-winner allocation, logs and cancellation."""

import base64
import hashlib
import json
import os
import shlex
import shutil
import sys
import time
import uuid
from pathlib import Path

from .transport import SSH
from .util import (
    Error,
    detached,
    identifier,
    locked,
    read_json,
    run,
    timestamp,
    write_json,
)

TERMINAL = {"completed", "failed", "cancelled"}
HELPER = Path(__file__).with_name("remote_job.py")


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
    output = SSH(candidate["host"]).command(shlex.join(args), timeout=timeout).stdout
    try:
        return json.loads(output)
    except json.JSONDecodeError as exc:
        raise Error(
            f"Invalid response from {candidate['host']}: {output[-1000:]}"
        ) from exc


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
                ssh = SSH(host)
                script = "python3 -c " + shlex.quote(
                    "import os; p=os.environ.get('CALIGO_SCRATCH') or os.path.expanduser('~/orcd/scratch'); "
                    "p=p if os.path.isdir(p) else os.path.expanduser('~/.cache'); "
                    "print(os.path.realpath(p))"
                )
                base = ssh.command(script).stdout.strip()
                if (
                    not base.startswith("/")
                    or "\n" in base
                    or any(c in base for c in ",:")
                ):
                    raise Error(f"Invalid scratch directory from {host}")
                host_id = hashlib.sha256(host.encode()).hexdigest()[:8]
                root = base + "/caligo/jobs/" + job_id + "-" + host_id
                candidate = {"host": host, "root": root, "offset": 0}
                ssh.command("mkdir -p " + shlex.quote(root))
                ssh.put(folder / "source.tar.gz", root + "/source.tar.gz")
                ssh.put(HELPER, root + "/remote_job.py")
                ssh.put(HELPER.with_name("image_cache.py"), root + "/image_cache.py")
                spec = {
                    "id": job_id,
                    "phase": phase,
                    "command": command,
                    "config": experiment.config,
                }
                write_json(folder / "spec.json", spec)
                ssh.put(folder / "spec.json", root + "/spec.json")
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


def ensure_watcher(experiment, job_id):
    import fcntl

    folder = location(experiment, job_id)
    with locked(folder / "watcher-start.lock"):
        with open(folder / "watcher.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
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


def watch(experiment, job_id):
    folder = location(experiment, job_id)
    with locked(folder / "watcher.lock"):
        write_json(folder / "watcher.json", {"pid": os.getpid()})
        with locked(folder / "submission.lock"):
            job = get_job(experiment, job_id)
            if job["state"] == "submitting":
                for candidate in job["candidates"]:
                    remote(candidate, "cancel")
                job.update(
                    state="failed",
                    error="Submission was interrupted; allocations cancelled.",
                    returncode=1,
                    finished=timestamp(),
                )
                write_json(folder / "job.json", job)
        if job["state"] in TERMINAL:
            return
        finished_since = None
        while job["state"] not in TERMINAL:
            try:
                if (folder / "cancel.json").exists():
                    for candidate in job["candidates"]:
                        remote(candidate, "cancel")
                    job.update(state="cancelled", finished=timestamp(), returncode=130)
                    break
                observations = []
                for candidate in job["candidates"]:
                    observation = (
                        {
                            "ready": False,
                            "state": "FINISHED",
                            "log": "",
                            "offset": candidate["offset"],
                        }
                        if candidate.get("cancelled")
                        else remote(candidate, "status", candidate["offset"])
                    )
                    observations.append(observation)
                    if observation["log"]:
                        with open(folder / "output.log", "ab") as log:
                            log.write(base64.b64decode(observation["log"]))
                    candidate["offset"] = observation["offset"]
                with locked(folder / "decision.lock"):
                    if (folder / "cancel.json").exists():
                        continue
                    if "winner" not in job:
                        ready = [
                            index
                            for index, obs in enumerate(observations)
                            if obs["ready"]
                            and "exit" not in obs
                            and obs.get("state") != "FINISHED"
                        ]
                        if ready:
                            job["winner"] = ready[0]
                            job["state"] = "running"
                            # Persist the choice before granting it; recovery never chooses twice.
                            write_json(folder / "job.json", job)
                    if "winner" in job and not job.get("granted"):
                        remote(job["candidates"][job["winner"]], "grant")
                        job["granted"] = True
                        write_json(folder / "job.json", job)
                if "winner" in job:
                    for index, candidate in enumerate(job["candidates"]):
                        if index != job["winner"] and not candidate.get("cancelled"):
                            remote(candidate, "cancel")
                            candidate["cancelled"] = True
                    observation = observations[job["winner"]]
                    if "exit" in observation:
                        result = observation["exit"]
                        job.update(result)
                        winner = job["candidates"][job["winner"]]
                        # Drain output before collecting artifacts and declaring completion.
                        while True:
                            final = remote(winner, "status", winner["offset"])
                            if not final["log"]:
                                break
                            with open(folder / "output.log", "ab") as log:
                                log.write(base64.b64decode(final["log"]))
                            winner["offset"] = final["offset"]
                        archive = remote(winner, "artifacts")["path"]
                        SSH(winner["host"]).get(archive, folder / "artifacts.tar.gz")
                        job.update(
                            state="completed"
                            if result["returncode"] == 0
                            else "failed",
                            finished=timestamp(),
                        )
                    elif observation.get("state") == "FINISHED":
                        finished_since = finished_since or time.monotonic()
                        if time.monotonic() - finished_since > 10:
                            job.update(
                                state="failed",
                                finished=timestamp(),
                                returncode=1,
                                error="Allocation ended without a workload result: "
                                + observation.get("accounting", "unknown Slurm status"),
                            )
                elif all(
                    obs.get("state") == "FINISHED" or "exit" in obs
                    for obs in observations
                ):
                    job.update(
                        state="failed",
                        finished=timestamp(),
                        returncode=1,
                        error="All allocations ended before execution was granted.",
                    )
                job.pop("monitor_error", None)
                write_json(folder / "job.json", job)
            except Error as exc:
                # A dropped SSH connection is not a finished job. Retry the same allocation.
                job["monitor_error"] = str(exc)
                write_json(folder / "job.json", job)
                time.sleep(2)
                continue
            if job["state"] not in TERMINAL:
                time.sleep(1)
        write_json(folder / "job.json", job)
        try:
            publish_logs(experiment, folder)
        except (Error, OSError) as exc:
            job["log_error"] = str(exc)
            write_json(folder / "job.json", job)


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


def publish_logs(experiment, folder):
    target = experiment.config["logs"]
    suffix = experiment.name + "/" + folder.name
    files = [
        folder / name
        for name in ("output.log", "snapshot.json", "job.json", "artifacts.tar.gz")
        if (folder / name).exists()
    ]
    if target.startswith("s3://"):
        for path in files:
            run(
                [
                    "aws",
                    "s3",
                    "cp",
                    path,
                    target.rstrip("/") + "/" + suffix + "/" + path.name,
                ]
            )
    elif ":" in target and not target.startswith("/"):
        host, directory = target.split(":", 1)
        destination = directory.rstrip("/") + "/" + suffix
        ssh = SSH(host)
        ssh.command("mkdir -p " + shlex.quote(destination))
        for path in files:
            ssh.put(path, destination + "/" + path.name)
    else:
        destination = Path(target).expanduser()
        if not destination.is_absolute():
            destination = experiment.project.root / destination
        destination = destination / suffix
        destination.mkdir(parents=True, exist_ok=True)
        for path in files:
            if path.resolve() != (destination / path.name).resolve():
                shutil.copyfile(path, destination / path.name)
