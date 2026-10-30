"""Single-winner allocation, monitoring, recovery, and cancellation."""

import base64
import os
import time

from ..transport import SSH
from ..util import locked, timestamp, write_json
from ..errors import Error

from .common import TERMINAL, location, get_job, remote
from .publishing import publish_logs


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
                observations = [
                    observe(candidate, folder) for candidate in job["candidates"]
                ]
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
                        collect_artifacts(winner, folder)
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


def observe(candidate, folder):
    """Read the next log block and advance this allocation's byte offset."""
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
    if observation["log"]:
        with open(folder / "output.log", "ab") as log:
            log.write(base64.b64decode(observation["log"]))
    candidate["offset"] = observation["offset"]
    return observation


def collect_artifacts(winner, folder):
    # Drain output before collecting artifacts and declaring completion.
    while observe(winner, folder)["log"]:
        pass
    archive = remote(winner, "artifacts")["path"]
    SSH(winner["host"]).get(archive, folder / "artifacts.tar.gz")
