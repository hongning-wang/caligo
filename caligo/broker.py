"""An experiment-scoped file mailbox: no host socket or SSH keys in containers.

Only the two mailbox directories are mounted into the sandbox. Neither the
experiment's controller state nor its parent directory is exposed.
"""

import json
import os
import stat
import sys
import threading
import time
import uuid
from pathlib import Path

from .util import locked, read_json, write_json
from .process import detached
from .errors import Error


def request(operation, *, timeout=60, **arguments):
    root = Path("/run/caligo")
    message_id = uuid.uuid4().hex
    destination = root / "responses" / (message_id + ".json")
    write_json(
        root / "requests" / (message_id + ".json"),
        {"operation": operation, "arguments": arguments},
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = read_json(destination)
        if result is not None:
            destination.unlink(missing_ok=True)
            if "error" in result:
                raise Error(result["error"])
            return result["result"]
        time.sleep(0.1)
    raise Error(
        "Caligo controller did not respond. Reattach from the host to restart it."
    )


def dispatch(experiment, operation, arguments):
    from . import jobs

    if operation == "ping":
        return "pong"
    if operation == "submit":
        return jobs.submit(experiment, arguments["phase"], arguments.get("command"))
    if operation == "jobs":
        return jobs.list_jobs(experiment)
    if operation == "job":
        return jobs.get_job(experiment, arguments["job_id"])
    if operation == "logs":
        return jobs.read_log(
            experiment, arguments["job_id"], arguments.get("offset", 0)
        )
    if operation == "cancel":
        return jobs.cancel(experiment, arguments["job_id"])
    raise Error(f"Operation not allowed in an experiment sandbox: {operation}")


def serve(experiment):
    root = experiment.state / "mailbox"
    with locked(experiment.state / "controller.lock"):
        write_json(experiment.state / "controller.json", {"pid": os.getpid()})
        slots = threading.BoundedSemaphore(8)

        def handle(path):
            try:
                # Never follow a sandbox-created link into the host filesystem.
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd) as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise Error("Mailbox messages must be regular files.")
                    message = json.loads(stream.read(1024 * 1024))
                result = {
                    "result": dispatch(
                        experiment, message["operation"], message["arguments"]
                    )
                }
            except (Error, ValueError, KeyError, TypeError, OSError) as exc:
                result = {"error": str(exc)}
            finally:
                path.unlink(missing_ok=True)
            write_json(root / "responses" / path.name, result)
            slots.release()

        active = set()
        while True:
            for path in (root / "requests").glob("*.json"):
                if not path.stem.isalnum() or path.name in active:
                    continue
                if slots.acquire(blocking=False):
                    active.add(path.name)
                    # UUID filenames are never reused; prune names when requests disappear.
                    threading.Thread(target=handle, args=(path,), daemon=True).start()
            active.intersection_update(
                path.name for path in (root / "requests").glob("*.json")
            )
            if (experiment.state / "controller-stop.json").exists() and not active:
                return
            time.sleep(0.1)


def ensure(experiment):
    with locked(experiment.state / "controller-start.lock"):
        (experiment.state / "controller-stop.json").unlink(missing_ok=True)
        marker = read_json(experiment.state / "controller.json", {})
        healthy = False
        if marker:
            try:
                os.kill(marker["pid"], 0)
                # A PID alone is not evidence of the right live controller.
                token = uuid.uuid4().hex
                target = experiment.state / "mailbox/responses" / (token + ".json")
                write_json(
                    experiment.state / "mailbox/requests" / (token + ".json"),
                    {"operation": "ping", "arguments": {}},
                )
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not target.exists():
                    time.sleep(0.05)
                healthy = read_json(target, {}).get("result") == "pong"
                target.unlink(missing_ok=True)
            except ProcessLookupError:
                pass
        if healthy:
            return
        # Refuse to start a second controller if a live one holds the lock.
        import fcntl

        with open(experiment.state / "controller.lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error(
                    "The experiment controller is busy or unresponsive; inspect controller.log."
                )
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        with open(experiment.state / "controller.log", "a") as log:
            process = detached(
                [
                    sys.executable,
                    "-m",
                    "caligo",
                    "--project",
                    str(experiment.project.root),
                    "--experiment",
                    experiment.name,
                    "_serve",
                ],
                log,
                env,
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (
                read_json(experiment.state / "controller.json", {}).get("pid")
                == process.pid
            ):
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise Error(
            "Could not start the controller; inspect "
            + str(experiment.state / "controller.log")
        )
