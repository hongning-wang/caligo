"""Project-scoped attempts, pinned revisions, and persistent agent threads."""

from pathlib import Path
import tarfile
import tempfile

from .common import (
    CaligoError,
    digest,
    identifier,
    inventory,
    lock,
    read_json,
    valid_id,
    write_json,
)
from .manifest import load
from .preparation import prepare


class Project:
    def __init__(self, manifest):
        self.manifest = Path(manifest).resolve()
        self.directory = self.manifest.parent / ".caligo"
        self.state_path = self.directory / "state.json"
        self._service_cache = {}

    def read(self):
        if not self.state_path.exists():
            raise CaligoError(
                "No attempts yet; use caligo create baseline --mode human"
            )
        state = read_json(self.state_path)
        if state.get("version") not in {2, 3}:
            raise CaligoError("Unsupported project state format")
        return state

    def find(self, name, state=None):
        state = state or self.read()
        for attempt in state["attempts"]:
            if name in (attempt.get("name"), attempt["id"]):
                return attempt
        raise CaligoError("Unknown attempt in this project: " + name)

    def create(self, name, mode, addons=None):
        from .workflow import Workflow
        from .addons import validate_selection

        valid_id(name)
        if mode not in {"human", "agent"}:
            raise CaligoError("mode must be human or agent")
        with lock(self.directory / "state.lock"):
            spec = load(self.manifest)
            if mode == "agent" and not spec.get("agent", {}).get("image"):
                raise CaligoError(
                    "Agent attempts require [agent].image in project.toml"
                )
            addons = validate_selection(addons or [], spec)
            if mode == "agent" and addons:
                raise CaligoError(
                    "Agent attempts use a container image and backend, not host add-ons"
                )
            state = self.read() if self.state_path.exists() else None
            if state and state["version"] != 3:
                raise CaligoError(
                    "Legacy attempts require explicit conversion: caligo migrate"
                )
            if state and any(
                name in (e.get("name"), e["id"]) for e in state["attempts"]
            ):
                raise CaligoError(
                    "Attempt already exists: " + name + "; use caligo switch " + name
                )
            if state is None:
                state = {
                    "version": 3,
                    "project": identifier("project-"),
                    "spec": spec,
                    "attempts": [],
                }
            with tempfile.TemporaryDirectory() as tmp:
                stage = Path(tmp) / "payload"
                prepare(self.manifest, spec, stage)
                revision = digest(
                    {
                        "spec": spec,
                        "seed": inventory(stage / "seed"),
                        "fixed": inventory(stage / "fixed"),
                    }
                )
                controller_project = state["project"] + "-" + revision[:16]
                view = dict(state, spec=spec, controller_project=controller_project)
                workflow = Workflow(view, self.directory / state["project"])
                archive = Path(tmp) / "project.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    for item in stage.iterdir():
                        tar.add(item, arcname=item.name)
                result = workflow.create(archive, mode, name=name, addons=addons)
            result.update(
                revision=revision, controller_project=controller_project, spec=spec
            )
            state["attempts"].append(result)
            state["active_attempt"], state["spec"] = result["id"], spec
            write_json(self.state_path, state)
        self._service_cache = {}
        return result

    def select(self, name):
        with lock(self.directory / "state.lock"):
            state = self.read()
            attempt = self.find(name, state)
            state["active_attempt"] = attempt["id"]
            write_json(self.state_path, state)
        return attempt

    def fork(self, source, name):
        valid_id(name)
        with lock(self.directory / "state.lock"):
            state = self.read()
            if state["version"] != 3:
                raise CaligoError("Legacy attempts require caligo migrate first")
            original = self.find(source, state)
            if any(name in (a["name"], a["id"]) for a in state["attempts"]):
                raise CaligoError("Attempt already exists: " + name)
            workflow = self.workflow(original["id"])
            result = workflow.coding.rpc("fork", environment=original["id"], name=name)
            result.update(
                {
                    key: original[key]
                    for key in ("revision", "controller_project", "spec", "location")
                    if key in original
                }
            )
            state["attempts"].append(result)
            state["active_attempt"] = result["id"]
            write_json(self.state_path, state)
        self._service_cache = {}
        return result

    def delete(self, name):
        with lock(self.directory / "state.lock"):
            state = self.read()
            if state["version"] != 3:
                raise CaligoError("Legacy attempts require caligo migrate first")
            attempt = self.find(name, state)
            result = self.workflow(attempt["id"]).coding.rpc(
                "delete", environment=attempt["id"]
            )
            state["attempts"].remove(attempt)
            # Retain routing and pinned specs so old runs remain discoverable,
            # including runs submitted by agents on a remote coding host.
            state.setdefault("deleted_attempts", []).append(attempt)
            if state.get("active_attempt") == attempt["id"]:
                state["active_attempt"] = (
                    state["attempts"][-1]["id"] if state["attempts"] else None
                )
            write_json(self.state_path, state)
        self._service_cache = {}
        return result

    def workflow(self, attempt=None):
        from .workflow import Workflow

        state = self.read()
        selected_id = attempt or state.get("active_attempt")
        selected = (
            self.find(selected_id, state)
            if selected_id
            else (state.get("deleted_attempts", [{}])[-1])
        )
        view = dict(state, spec=selected.get("spec", state["spec"]))
        if selected.get("controller_project"):
            view["controller_project"] = selected["controller_project"]
        workflow = Workflow(view, self.directory / state["project"])
        workflow.project_manager = self
        return workflow

    def enter(self, name, argv=None):
        import sys

        attempt = self.select(name)
        if attempt["mode"] not in {"human", "agent"}:
            raise CaligoError("Legacy mode; run caligo migrate first")
        if attempt["mode"] == "agent":
            from .cli import attach

            attach(self, attempt["name"])
            return 0
        while True:
            workflow = self.workflow(attempt["id"])
            print(
                "%s / %s (%s)"
                % (
                    workflow.spec["name"],
                    attempt["name"],
                    workflow.spec["locations"]["coding"],
                ),
                file=sys.stderr,
            )
            result = workflow.coding_shell(
                attempt["id"], argv or [], tty=sys.stdin.isatty()
            )
            if workflow.pending_attempt is None:
                return result
            attempt = self.select(workflow.pending_attempt)
            if attempt["mode"] != "human":
                from .cli import attach

                attach(self, attempt["name"])
                return 0

    def service(self, name, method="GET", path="/threads", values=None):
        attempt = self.find(name)
        if attempt["mode"] != "agent":
            raise CaligoError("This operation requires an agent attempt")
        key = attempt.get("controller_project", self.read()["project"])
        if key not in self._service_cache:
            workflow = self.workflow(attempt["id"])
            state = dict(workflow.state)
            if workflow.spec["locations"]["coding"] == "local":
                state["client_directory"] = str(workflow.directory)
            elif "local" in [
                workflow.spec["locations"][key] for key in ("dev", "experiments")
            ]:
                raise CaligoError(
                    "Remote persistent agents need dev and experiments reachable from the coding host; configure SSH aliases instead of client-local locations"
                )
            endpoint = workflow.coding
            endpoint.rpc("service-start", state=state)
            self._service_cache[key] = endpoint, state
        endpoint, state = self._service_cache[key]
        try:
            return endpoint.rpc(
                "service-request", method=method, path=path, values=values
            )
        except CaligoError as error:
            if method != "GET" or not str(error).startswith(
                "Agent service unavailable"
            ):
                raise
            endpoint.rpc("service-start", state=state)
            return endpoint.rpc(
                "service-request", method=method, path=path, values=values
            )

    def thread(self, name):
        attempt = self.find(name)
        threads = self.service(name)["threads"]
        matches = [thread for thread in threads if thread["attempt"] == attempt["id"]]
        if not matches:
            raise CaligoError(
                "No thread for this attempt; use caligo agent start " + name + " --goal TEXT"
            )
        return matches[-1]

    def start_agent(self, name, backend, goal, continuous=True, model=None):
        attempt = self.find(name)
        return self.service(
            name,
            "POST",
            "/threads",
            dict(
                attempt=attempt["id"],
                backend=backend,
                goal=goal,
                continuous=continuous,
                model=model,
            ),
        )

    def migrate(self):
        with lock(self.directory / "state.lock"):
            state = self.read()
            if state["version"] == 3:
                return {"migrated": False, "version": 3}
            for attempt in state["attempts"]:
                workflow = self.workflow(attempt["id"])
                workflow.coding.rpc("migrate-human", environment=attempt["id"])
                attempt.update(mode="human", spec=state["spec"])
            state["version"] = 3
            write_json(self.state_path, state)
            return {"migrated": True, "version": 3, "attempts": len(state["attempts"])}


def format_attempts(result):
    lines = ["Project: " + result["project"]]
    for attempt in result["attempts"]:
        lines.append(
            "%s %s  (%s)"
            % ("*" if attempt["current"] else " ", attempt["name"], attempt["mode"])
        )
    return "\n".join(lines)
