"""Explicit coding, dev execution, and experiment locations."""

import base64
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import nullcontext

from . import scheduler
from .common import (
    CaligoError,
    archive_records,
    identifier,
    read_json,
    write_json,
    stream_command,
)
from .controller_api import dispatch
from .remote import Controller, unpack
from .transport import Transport


class Endpoint:
    def __init__(self, location, root, project, remote):
        self.location, self.project = location, project
        self.transport = None
        if location == "local":
            self.root = str(Path(root).resolve())
            self.controller = Controller(self.root, project)
        else:
            self.transport = Transport(location, remote.get("python"))
            home = self.transport.install()
            self.root = remote.get("root", home + "/.local/share/caligo")
            if self.root.startswith("~/"):
                self.root = home + self.root[1:]
            if not self.root.startswith("/"):
                raise CaligoError("remote.root must be absolute or start with ~/")

    def rpc(self, action, **values):
        if self.transport:
            return self.transport.rpc(
                dict(root=self.root, project=self.project, action=action, **values)
            )
        result = dispatch(
            self.controller, action, values, Path(__file__).with_name("worker.py")
        )
        if result is NotImplemented:
            raise CaligoError("Unsupported operation: " + action)
        return result

    def upload(self, action, archive, **values):
        if not self.transport:
            return self.rpc(action, archive=str(archive), **values)
        uploaded = self.transport.upload(archive)
        try:
            return self.rpc(action, archive=uploaded, **values)
        finally:
            self.transport.remove_upload(uploaded)

    def download(self, kind, item, archive):
        with Path(archive).open("wb") as output:
            if self.transport:
                t = self.transport
                command = [
                    t.python,
                    t.agent,
                    "_remote-" + kind,
                    self.root,
                    self.project,
                    item,
                ]
                result = subprocess.run(
                    t.ssh + [t.host, shlex.join(command)],
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
                if result.returncode:
                    raise CaligoError(result.stderr.decode(errors="replace"))
            elif kind == "service-archive":
                from .service import archive_run

                archive_run(self.controller, item, output)
            elif kind in {"workspace", "dev-workspace"}:
                self.controller.environment_archive(
                    item, output, fixed=kind == "workspace"
                )
            elif kind == "snapshot":
                self.controller.snapshot_archive(item, output)
            else:
                path = self.controller.run_dir(item)
                archive_records(path, output)

    def shell(self, environment, argv, directory, tty, coding=False, emit=None):
        if not self.transport:
            return self.controller.native_shell(
                environment, argv, directory, coding=coding, emit=emit
            )
        t = self.transport
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "session.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name in ("bin", "bashrc"):
                    tar.add(directory / name, arcname=name)
            uploaded = t.upload(archive)
            remote_dir = str(Path(uploaded).parent)
            try:
                t.checked(
                    t.ssh
                    + [t.host, shlex.join(["tar", "-xzf", uploaded, "-C", remote_dir])]
                )
                command = [
                    t.python,
                    t.agent,
                    "_remote-coding-shell" if coding else "_remote-native-shell",
                    self.root,
                    self.project,
                    environment,
                    remote_dir,
                ] + argv
                forwarding = (
                    [
                        "-o",
                        "ExitOnForwardFailure=yes",
                        "-R",
                        remote_dir + "/control.sock:" + str(directory / "control.sock"),
                    ]
                    if coding
                    else []
                )
                invocation = (
                    t.ssh
                    + (["-t"] if tty else [])
                    + forwarding
                    + [t.host, shlex.join(command)]
                )
                return (
                    stream_command(invocation, emit)
                    if emit
                    else subprocess.call(invocation)
                )
            finally:
                t.checked(t.ssh + [t.host, shlex.join(["rm", "-rf", "--", remote_dir])])


class Workflow:
    def __init__(
        self,
        state,
        directory,
        *,
        coding_override=None,
        agent_scope=False,
        thread_id=None,
        event_sink=None,
    ):
        self.state = state
        self.directory = Path(directory)
        self.spec = state["spec"]
        self.endpoints = {}
        self.project_manager = None
        self.pending_attempt = None
        self.coding_override = coding_override
        self.agent_scope = agent_scope
        self.thread_id = thread_id
        self.turn_id = None
        self.event_sink = event_sink
        self.freeze = None
        self.goal_review = None

    def endpoint(self, location, project=None):
        project = project or self.state["project"]
        key = (location, project)
        if key not in self.endpoints:
            self.endpoints[key] = Endpoint(
                location,
                self.directory / "hosts" / "local",
                project,
                self.spec["remote"],
            )
        return self.endpoints[key]

    @property
    def coding(self):
        if self.coding_override is not None:
            return self.coding_override
        return self.endpoint(
            self.spec["locations"]["coding"], self.state.get("controller_project")
        )

    def create(self, archive, mode, name, addons=None):
        self.coding.upload("bootstrap", archive, development=True)
        result = self.coding.rpc("create", mode=mode, name=name, addons=addons or [])
        result["location"] = self.spec["locations"]["coding"]
        return result

    def run_dir(self, run_id):
        from .common import valid_id

        return self.directory / "records" / valid_id(run_id)

    def run_endpoint(self, run_id):
        if not (self.run_dir(run_id) / "route.json").exists():
            candidates = [self.coding]
            if self.project_manager:
                seen = {(self.coding.location, self.coding.project)}
                for attempt in self.state["attempts"]:
                    workflow = self.project_manager.workflow(attempt["id"])
                    endpoint = workflow.coding
                    if (endpoint.location, endpoint.project) not in seen:
                        seen.add((endpoint.location, endpoint.project))
                        candidates.append(endpoint)
            imported = None
            for candidate in candidates:
                try:
                    imported = candidate.rpc("service-run", run=run_id)
                    break
                except (CaligoError, OSError):
                    continue
            if imported is None:
                raise CaligoError("Unknown run in this project: " + run_id)
            write_json(self.run_dir(run_id) / "run.json", imported["record"])
            write_json(self.run_dir(run_id) / "route.json", imported["route"])
        route = read_json(self.run_dir(run_id) / "route.json")
        record = read_json(self.run_dir(run_id) / "run.json")
        remote = record.get("spec", self.spec).get("remote", self.spec["remote"])
        from .common import digest

        key = ("run", route["location"], route["project"], digest(remote))
        if key not in self.endpoints:
            self.endpoints[key] = Endpoint(
                route["location"],
                self.directory / "hosts/local",
                route["project"],
                remote,
            )
        return self.endpoints[key]

    def submit(self, environment, selected, agent=None):
        # Keep the session submission interface shared with Controller.submit.
        # Workflow chooses each destination's worker through Endpoint instead.
        source, source_env = self.coding, environment
        with self.frozen_source(environment):
            record = source.rpc("snapshot", environment=source_env, script=selected)
        run_id = record["id"]
        record["environment"] = environment
        attempt = next(e for e in self.state["attempts"] if e["id"] == environment)
        record["attempt"] = attempt["name"]
        record["project"] = self.spec["name"]
        record["kind"] = "experiments"
        location = self.spec["locations"]["experiments"]
        record["execution_location"] = location
        record["spec"] = self.spec
        if self.thread_id:
            record.update(thread=self.thread_id, turn=self.turn_id)
        directory = self.run_dir(run_id)
        write_json(directory / "run.json", record)
        project = self.state.get("controller_project", self.state["project"]) + "-runs"
        write_json(directory / "route.json", {"location": location, "project": project})
        try:
            with tempfile.TemporaryDirectory() as tmp:
                archive = Path(tmp) / "run.tar.gz"
                source.download("snapshot", run_id, archive)
                stage = Path(tmp) / "payload"
                stage.mkdir()
                unpack(archive, stage)
                write_json(stage / "run.json", record)
                with tarfile.open(archive, "w:gz") as tar:
                    for item in stage.iterdir():
                        tar.add(item, arcname=item.name)
                destination = self.endpoint(location, project)
                record["state"] = "SUBMITTING"
                write_json(directory / "run.json", record)
                result = destination.upload("receive", archive)
                write_json(directory / "status.json", result)
                if self.event_sink:
                    self.event_sink(
                        "run.submitted",
                        {
                            "id": run_id,
                            "thread": self.thread_id,
                            "turn": record.get("turn"),
                        },
                    )
                return result
        except Exception as error:
            record.update(
                state="SUBMISSION_UNKNOWN"
                if record["state"] == "SUBMITTING"
                else "PREPARATION_FAILED",
                error=str(error),
            )
            write_json(directory / "run.json", record)
            raise CaligoError(
                "Run %s retained at %s: %s" % (run_id, directory, error)
            ) from error

    def status(self, run_id, refresh=True):
        directory = self.run_dir(run_id)
        if refresh:
            try:
                result = self.run_endpoint(run_id).rpc("status", run=run_id)
                write_json(directory / "status.json", result)
                return result
            except (CaligoError, OSError) as error:
                result = self.status(run_id, refresh=False)
                result["poll_error"] = str(error)
                return result
        path = directory / "status.json"
        return read_json(path if path.exists() else directory / "run.json")

    def logs(self, run_id):
        return self.run_endpoint(run_id).rpc("logs", run=run_id)

    def cancel(self, run_id):
        return self.run_endpoint(run_id).rpc("cancel", run=run_id)

    def publish(self, run_id):
        return self.run_endpoint(run_id).rpc("publish", run=run_id)

    def archive(self, run_id, output):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.tar.gz"
            self.run_endpoint(run_id).download("archive", run_id, path)
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, output)

    def list(self, environment=None):
        environments = self.state["attempts"]
        if environment:
            environments = [env for env in environments if env["id"] == environment]
        runs = [
            self.status(path.parent.name, refresh=False)
            for path in sorted((self.directory / "records").glob("*/run.json"))
        ]
        if environment:
            runs = [record for record in runs if record["environment"] == environment]
        return {"attempts": environments, "runs": runs}

    def list_attempts(self, current=None):
        state = self.project_manager.read() if self.project_manager else self.state
        current = current or state.get("active_attempt")
        return {
            "project": self.spec["name"],
            "attempts": [
                {"name": a["name"], "mode": a["mode"], "current": a["id"] == current}
                for a in state["attempts"]
            ],
        }

    def request_switch(
        self, name, mode=None, addons=None, goal=None, backend="codex", continuous=True
    ):
        if not self.project_manager:
            raise CaligoError("Attempt switching requires a project controller")
        if mode is not None:
            if mode == "agent" and not goal:
                raise CaligoError("Agent creation requires --goal TEXT")
            attempt = self.project_manager.create(name, mode, addons=addons)
            if mode == "agent":
                return self.project_manager.start_agent(name, backend, goal, continuous)
        else:
            attempt = self.project_manager.find(name)
        self.pending_attempt = attempt["id"]
        return {"switch": attempt["name"]}

    def coding_shell(self, environment, argv, tty=False):
        from .session import session

        with session(self, environment, None) as directory:
            return self.coding.shell(environment, argv, directory, tty, coding=True)

    def exec_command(self, environment, argv, emit=None):
        from .session import session

        if not argv or not all(isinstance(arg, str) for arg in argv):
            raise CaligoError("caligo exec requires a command after --")
        if self.event_sink:
            forward = emit

            def emit(channel, data):
                self.event_sink(
                    "dev.output",
                    {"channel": channel, "data": base64.b64encode(data).decode()},
                )
                if forward:
                    forward(channel, data)

        source = self.coding
        location = self.spec["locations"]["dev"]
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "workspace.tar.gz"
            with self.frozen_source(environment):
                source.download("dev-workspace", environment, archive)
            target = self.endpoint(location, identifier("dev-"))
            target.upload("bootstrap", archive, development=True)
            mode = source.rpc("environment", environment=environment)["mode"]
            dev = target.rpc("create", mode=mode, name="dev")
        record = {
            "attempt_id": environment,
            "location": location,
            "project": target.project,
            "workspace": dev["workspace"],
            "spec": self.spec,
            "mode": mode,
        }
        if self.thread_id:
            record.update(thread=self.thread_id, turn=self.turn_id)
            self.event_sink("dev.started", record)
        record_path = self.directory / "dev-sessions" / (target.project + ".json")
        write_json(record_path, record)
        print("Dev copy: %s:%s" % (location, dev["workspace"]), file=sys.stderr)
        # A distinct copy even when coding and dev select the same machine.
        # Dev changes and outputs stay there; no write-back to the attempt.
        if self.spec["scheduler"].get("requeue"):
            run_id = target.project
            attempt = next(a for a in self.state["attempts"] if a["id"] == environment)
            origin = {
                "id": run_id,
                "environment": environment,
                "attempt": attempt["name"],
            }
            if self.thread_id:
                origin.update(thread=self.thread_id, turn=self.turn_id)
            directory = self.run_dir(run_id)
            write_json(
                directory / "route.json",
                {"location": location, "project": target.project},
            )
            write_json(
                directory / "run.json",
                dict(record, **origin, kind="dev", state="SUBMITTING"),
            )
            record["run_id"] = run_id
            write_json(record_path, record)
            print("Dev run: " + run_id, file=sys.stderr)
            try:
                submitted = target.rpc(
                    "submit-dev", environment=dev["id"], argv=argv, origin=origin
                )
                write_json(directory / "status.json", submitted)
            except Exception as error:
                raise CaligoError(
                    "Dev run %s retained; inspect its status before retrying: %s"
                    % (run_id, error)
                ) from error
            result = self.wait_dev(run_id, emit)
        else:
            with session(self, environment, None, controls=False) as directory:
                result = target.shell(dev["id"], argv, directory, False, emit=emit)
        record["exit_code"] = result
        write_json(record_path, record)
        if self.event_sink:
            self.event_sink("dev.completed", record)
        return result

    def frozen_source(self, environment):
        if self.freeze is not None:
            return self.freeze()
        attempt = next(
            (
                item
                for item in self.state.get("attempts", [])
                if item["id"] == environment
            ),
            None,
        )
        if attempt and attempt["mode"] == "agent":
            raise CaligoError(
                "Agent snapshots must be requested through their persistent thread service"
            )
        return nullcontext()

    def wait_dev(self, run_id, emit=None):
        target = self.run_endpoint(run_id)
        offset = 0

        def drain():
            nonlocal offset
            while True:
                chunk = target.rpc("tail", run=run_id, offset=offset)
                data = base64.b64decode(chunk["data"])
                offset = chunk["offset"]
                if not data:
                    return
                if emit:
                    emit("stdout", data)
                else:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

        try:
            while True:
                result = self.status(run_id)
                drain()
                evidence = result.get("scheduler", {})
                if (
                    not result.get("poll_error")
                    and evidence.get("state") in scheduler.TERMINAL
                ):
                    code = result.get("exit_code")
                    if (
                        evidence["state"] == "COMPLETED"
                        and result["state"] == "SUCCEEDED"
                    ):
                        return 0
                    if (
                        evidence["state"] == "FAILED"
                        and isinstance(code, int)
                        and code != 0
                    ):
                        return code if code > 0 else 128 - code
                    print(
                        "Dev run %s ended: %s" % (run_id, result["state"]),
                        file=sys.stderr,
                    )
                    return 1
                time.sleep(5)
        except KeyboardInterrupt:
            self.cancel(run_id)
            return 130
        except (CaligoError, OSError) as error:
            raise CaligoError(
                "Lost connection to dev run %s; the job may still be running. "
                "Use caligo status/logs/cancel with that ID: %s" % (run_id, error)
            ) from error
