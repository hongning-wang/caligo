"""Container boundary for persistent agent sessions and Git inspection."""

import json
import os
from pathlib import Path
import subprocess
import sys
from contextlib import contextmanager
import threading

from .common import CaligoError, run


def image(spec, configured=None):
    configured = configured or spec.get("agent", {}).get("image")
    if not configured:
        raise CaligoError(
            "Agent attempts require [agent].image containing Python, Codex or Claude Agent SDK, and Git"
        )
    # Resolve once and record the immutable image ID with the thread runtime.
    value = run(["docker", "image", "inspect", "--format", "{{.Id}}", configured])
    return value


def mounts(spec, attempt):
    workspace = Path(attempt["workspace"])
    result = [(workspace, workspace, True)]
    for area in spec["editable"] + [spec["executable"]]:
        folder = workspace / area
        result.append((folder, folder, not bool(folder.stat().st_mode & 0o200)))
        for path in folder.rglob("*"):
            if not path.stat().st_mode & 0o200:
                result.append((path, path, True))
    result.append((Path(attempt["git_dir"]), Path("/git"), False))
    return result


def command(
    spec,
    attempt,
    resolved,
    argv,
    extra_mounts=(),
    environment=None,
    name=None,
    readonly=False,
):
    result = [
        "docker",
        "run",
        "--rm",
        "--init",
        "-i",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=512",
        "--user",
        "%s:%s" % (os.getuid(), os.getgid()),
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,mode=1777",
        "--workdir",
        str(Path(attempt["workspace"]) / spec["executable"]),
    ]
    if name:
        result += ["--name", name]
    if sys.platform.startswith("linux"):
        result += ["--add-host", "host.docker.internal:host-gateway"]
    bindings = mounts(spec, attempt)
    if readonly:
        bindings = [(source, destination, True) for source, destination, _ in bindings]
    for source, destination, protected in [*bindings, *extra_mounts]:
        if any(c in str(source) + str(destination) for c in ",\n"):
            raise CaligoError("Unsupported container mount path")
        result += [
            "--mount",
            "type=bind,src=%s,dst=%s%s"
            % (source, destination, ",readonly" if protected else ""),
        ]
    env = {
        "GIT_DIR": "/git",
        "GIT_WORK_TREE": attempt["workspace"],
        "HOME": "/home/agent",
        "CALIGO_WORKSPACE": attempt["workspace"],
        "CALIGO_EXECUTABLE": str(Path(attempt["workspace"]) / spec["executable"]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(environment or {})
    for key, value in env.items():
        result += ["--env", str(key) if value is None else str(key) + "=" + str(value)]
    return result + ["--entrypoint", argv[0], resolved] + list(argv[1:])


def git_metadata(controller, attempt, argv):
    return run(
        command(
            controller.spec,
            attempt,
            image(controller.spec, attempt.get("agent_image")),
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *argv,
            ],
            environment={"GIT_OPTIONAL_LOCKS": "0"},
            readonly=True,
        )
    )


class Container:
    def __init__(self, controller, attempt, thread, directory):
        self.controller, self.attempt, self.thread = controller, attempt, thread
        self.name = "caligo-" + thread["id"]
        self.directory = Path(directory)
        self.resolved = (
            thread.get("image") or attempt.get("agent_image") or image(controller.spec)
        )
        self.lock = threading.Lock()
        self.snapshot_lock = threading.RLock()

    def start(self):
        private = Path(self.attempt["git_dir"]).parent
        home = private / "agent-home"
        home.mkdir(exist_ok=True, mode=0o700)
        (home / ".codex").mkdir(exist_ok=True, mode=0o700)
        outputs = private / "agent-outputs"
        outputs.mkdir(exist_ok=True)
        package = Path(__file__).parent
        invocation = Path(sys.argv[0]).resolve()
        if invocation.suffix == ".pyz":
            code_mount = (invocation, "/opt/caligo.pyz", True)
            argv = ["python3", "/opt/caligo.pyz", "_agent-worker"]
        else:
            code_mount = (package, "/opt/caligo/caligo", True)
            argv = ["python3", "-m", "caligo", "_agent-worker"]
        argv += [
            self.thread["backend"],
            self.thread.get("backend_id") or "",
            self.thread.get("model")
            or self.controller.spec.get("agent", {}).get("model", ""),
        ]
        env = {
            "PYTHONPATH": "/opt/caligo",
            "CALIGO_CLIENT": "/control/bin/caligo",
            "CALIGO_SOCKET": "/control/control.sock",
            "CALIGO_OUTPUTS": "/outputs",
            "PATH": "/control/bin:/opt/agent-venv/bin:/usr/local/bin:/usr/bin:/bin",
            "CODEX_HOME": "/home/agent/.codex",
            "CALIGO_EDITABLE": json.dumps(
                self.controller.spec["editable"] + [self.controller.spec["executable"]]
            ),
            "CALIGO_READABLE": json.dumps(self.controller.spec["readable"]),
        }
        broker = json.loads((self.directory / "broker.json").read_text())
        env.update(CALIGO_BROKER_URL=broker["url"], CALIGO_BROKER_TOKEN=broker["token"])
        for key in self.controller.spec.get("agent", {}).get("env", []):
            if key not in os.environ:
                raise CaligoError(
                    "Agent credential/environment variable is missing on coding host: "
                    + key
                )
            # Docker reads explicitly named credentials from its environment;
            # their values never appear in process arguments or Caligo state.
            env[key] = None
        extra = [
            code_mount,
            (home, "/home/agent", False),
            (outputs, "/outputs", False),
            (self.directory, "/control", True),
        ]
        self.process = subprocess.Popen(
            command(
                self.controller.spec,
                self.attempt,
                self.resolved,
                argv,
                extra,
                env,
                self.name,
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return self.process

    def send(self, message):
        with self.lock:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def stop(self):
        # The daemon always tears down the actual container, not just docker's client.
        with self.snapshot_lock:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
            if hasattr(self, "process"):
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()

    @contextmanager
    def frozen(self):
        # No host traversal of agent-owned files while the agent can swap paths.
        with self.snapshot_lock:
            run(["docker", "pause", self.name])
            try:
                yield
            finally:
                subprocess.run(["docker", "unpause", self.name], capture_output=True)
