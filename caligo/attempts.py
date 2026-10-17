"""Project-scoped attempt names and durable selection."""
from pathlib import Path
import tarfile
import tempfile

from .common import CaligoError, identifier, lock, read_json, valid_id, write_json
from .manifest import load


class Project:
    def __init__(self, manifest):
        self.manifest = Path(manifest).resolve()
        self.directory = self.manifest.parent / ".caligo"
        self.state_path = self.directory / "state.json"

    def read(self):
        if not self.state_path.exists():
            raise CaligoError("No attempts yet; use caligo create baseline --mode user")
        state = read_json(self.state_path)
        if state.get("version") != 2:
            raise CaligoError("Unsupported project state format")
        return state

    def find(self, name, state=None):
        state = state or self.read()
        for attempt in state["attempts"]:
            if name in (attempt.get("name"), attempt["id"]):
                return attempt
        raise CaligoError("Unknown attempt in this project: " + name)

    def create(self, name, mode, addons=None):
        from .cli import prepare
        from .workflow import Workflow
        valid_id(name)
        with lock(self.directory / "state.lock"):
            spec = load(self.manifest)
            from .addons import validate_selection
            addons = validate_selection(addons or [], spec)
            if "code-server" in addons and spec["locations"]["coding"] != "local":
                raise CaligoError("The editor preview requires locations.coding = 'local'")
            state = self.read() if self.state_path.exists() else None
            if state and state["spec"] != spec:
                raise CaligoError("Project configuration changed; use a separate project directory")
            if state and any(name in (e.get("name"), e["id"]) for e in state["attempts"]):
                raise CaligoError("Attempt already exists: " + name + "; use caligo switch " + name)
            if state is None:
                state = {"version": 2, "project": identifier("project-"), "spec": spec, "attempts": []}
            workflow = Workflow(state, self.directory / state["project"])
            if not state["attempts"]:
                with tempfile.TemporaryDirectory() as tmp:
                    stage = Path(tmp) / "payload"
                    prepare(self.manifest, spec, stage)
                    archive = Path(tmp) / "project.tar.gz"
                    with tarfile.open(archive, "w:gz") as tar:
                        for item in stage.iterdir():
                            tar.add(item, arcname=item.name)
                    result = workflow.create(archive, mode, name=name, addons=addons)
            else:
                result = workflow.coding.rpc("create", mode=mode, name=name, addons=addons)
                result["location"] = spec["locations"]["coding"]
            state["attempts"].append(result)
            state["active_attempt"] = result["id"]
            write_json(self.state_path, state)
        return result

    def select(self, name):
        with lock(self.directory / "state.lock"):
            state = self.read()
            attempt = self.find(name, state)
            state["active_attempt"] = attempt["id"]
            write_json(self.state_path, state)
        return attempt

    def workflow(self):
        from .workflow import Workflow
        state = self.read()
        workflow = Workflow(state, self.directory / state["project"])
        workflow.project_manager = self
        return workflow

    def enter(self, name, argv=None):
        import sys
        attempt = self.select(name)
        while True:
            workflow = self.workflow()
            print("%s / %s (%s)" % (workflow.spec["name"], attempt.get("name", attempt["id"]),
                                    workflow.spec["locations"]["coding"]), file=sys.stderr)
            result = workflow.coding_shell(attempt["id"], argv or [], tty=sys.stdin.isatty())
            if workflow.pending_attempt is None:
                return result
            attempt = self.select(workflow.pending_attempt)


def format_attempts(result):
    lines = ["Project: " + result["project"]]
    for attempt in result["attempts"]:
        lines.append("%s %s  (%s)" % ("*" if attempt["current"] else " ", attempt["name"], attempt["mode"]))
    return "\n".join(lines)
