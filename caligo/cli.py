import argparse
import base64
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import threading
import queue

from .common import CaligoError
from .manifest import load
from .preparation import prepare
from .remote import unpack
from .transport import Transport


def find_project(value=None):
    if value:
        return Path(value).resolve()
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / "project.toml").is_file():
            return parent / "project.toml"
    raise CaligoError("No project.toml found; use --project PATH or caligo init PATH")


def init(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    if (path / "project.toml").exists():
        raise CaligoError("project.toml already exists")
    for folder in ("src", "data", "executable", "fixed"):
        (path / folder).mkdir(exist_ok=True)
    (path / "project.toml").write_text("""name = "caligo-project"
cpu = 1
# gpu = "h200:4"  # Optional GPU type:count; use your cluster's GPU type
image = "python:3.12-bookworm"
editable = ["src"]
readable = ["data"]
executable = "executable"
execute = "executable/train.py"
fixed = "fixed"
outputs = "results"

[locations]
# Required: set all three to "local" or an SSH host alias.
# coding = "local"
# dev = "local"
# experiments = "your-cluster"

[remote]

[scheduler]
type = "slurm"
# requeue = true  # Requeue experiments and run dev commands as requeueable Slurm jobs
# partition = "your-partition"  # Omit to use the cluster default
# setup = "module load apptainer"  # Only if your compute nodes need it
time = "00:05:00"
memory = "1G"
""")
    script = path / "executable/train.py"
    if not script.exists():
        script.write_text(
            'import json, os\nfrom pathlib import Path\nout = Path(os.environ["CALIGO_OUTPUTS"])\n(out / "metrics.json").write_text(json.dumps({"value": 1}))\nprint("Experiment complete")\n'
        )
    # Do not rewrite an existing repository's ignore rules.
    ignore = path / ".gitignore"
    with ignore.open("a") as f:
        f.write("\n.caligo/\nfixed/\nresults/\n")
    return {"project": str(path / "project.toml")}


def parser():
    p = argparse.ArgumentParser(
        prog="caligo", description="Projects, coding attempts, and experiment runs"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Prepare a project").add_argument(
        "path", nargs="?", default="."
    )
    sub.add_parser("validate", help="Validate project files").add_argument(
        "manifest", nargs="?", default="project.toml"
    )
    sub.add_parser("doctor", help="Inspect a remote host").add_argument(
        "--host", required=True
    )
    migrate = sub.add_parser(
        "migrate", help="Explicitly convert legacy attempts to human mode"
    )
    migrate.add_argument("--project")
    ui = sub.add_parser("ui", help="Open the agent interface")
    ui.add_argument("--project")
    ui.add_argument("--port", type=int, default=0)
    ui.add_argument(
        "--no-open", action="store_true", help="Print the URL without opening a browser"
    )
    for name in (
        "start",
        "attach",
        "threads",
        "steer",
        "interrupt",
        "pause",
        "resume",
        "stop",
        "service",
    ):
        command = sub.add_parser(name)
        command.add_argument("--project")
        if name == "service":
            command.add_argument("operation", choices=["start", "status", "stop"])
        command.add_argument("name", help="Agent attempt name")
        if name == "start":
            command.add_argument(
                "--backend", choices=["codex", "claude"], default="codex"
            )
            command.add_argument("--goal", required=True)
            command.add_argument(
                "--once",
                action="store_true",
                help="Wait for instructions after each turn",
            )
        if name == "attach":
            command.add_argument("--after", type=int, default=0)
            command.add_argument("--json", action="store_true")
            command.add_argument("--no-follow", action="store_true")
        if name in {"steer", "interrupt"}:
            command.add_argument("text", nargs="?" if name == "interrupt" else None)
        if name == "steer":
            command.add_argument(
                "--delivery", choices=["queue", "steer"], default="queue"
            )
            command.add_argument("--id", help="Idempotency key for retrying a message")
    for name in (
        "create",
        "switch",
        "exec",
        "run",
        "status",
        "logs",
        "artifacts",
        "cancel",
        "publish",
        "list",
    ):
        command = sub.add_parser(name)
        command.add_argument(
            "--project",
            help="Project manifest (otherwise found in the current directory or its parents)",
        )
        if name in {"create", "switch"}:
            command.add_argument("name", help="Attempt name within this project")
        if name == "create":
            command.add_argument("--mode", default="human", choices=["human", "agent"])
            command.add_argument(
                "--backend", choices=["codex", "claude"], default="codex"
            )
            command.add_argument("--goal")
            command.add_argument("--once", action="store_true")
            from .addons import REGISTRY

            command.add_argument(
                "--addon",
                action="append",
                default=[],
                choices=REGISTRY,
                help="Enable a development tool (repeatable)",
            )
        if name == "exec":
            command.add_argument("argv", nargs=argparse.REMAINDER)
        if name == "run":
            command.add_argument("script", nargs="?")
        if name in {"status", "logs", "artifacts", "cancel", "publish"}:
            command.add_argument("run_id")
        if name == "artifacts":
            command.add_argument("destination", nargs="?", default=".")
    return p


def main():
    from .attempts import Project, format_attempts

    args = parser().parse_args()
    try:
        if args.command == "init":
            result = init(args.path)
        elif args.command == "doctor":
            transport = Transport(args.host)
            transport.install()
            result = transport.rpc({"action": "probe"})
        elif args.command == "validate":
            path = find_project(args.manifest)
            spec = load(path)
            with tempfile.TemporaryDirectory() as tmp:
                prepare(path, spec, tmp)
            result = {"valid": True, "name": spec["name"]}
        else:
            project = Project(find_project(args.project))
            if args.command == "ui":
                from .ui import serve

                serve(project, args.port, open_browser=not args.no_open)
                return
            if args.command == "migrate":
                print(json.dumps(project.migrate(), indent=2))
                return
            if args.command in {
                "start",
                "attach",
                "threads",
                "steer",
                "interrupt",
                "pause",
                "resume",
                "stop",
                "service",
            }:
                result = agent_command(project, args)
                if result is not None:
                    print(json.dumps(result, indent=2))
                return
            if args.command == "create":
                if args.mode == "agent" and not args.goal:
                    raise CaligoError("Agent creation requires --goal TEXT")
                attempt = project.create(args.name, args.mode, addons=args.addon)
                if args.mode == "agent":
                    print(
                        json.dumps(
                            project.start_agent(
                                args.name, args.backend, args.goal, not args.once
                            ),
                            indent=2,
                        )
                    )
                    return
                sys.exit(project.enter(attempt["name"]))
            if args.command == "switch":
                if project.find(args.name)["mode"] == "agent":
                    project.select(args.name)
                    attach(project, args.name)
                    return
                sys.exit(project.enter(args.name))
            if args.command == "list" and not project.state_path.exists():
                print(
                    format_attempts(
                        {"project": load(project.manifest)["name"], "attempts": []}
                    )
                )
                return
            workflow = project.workflow()
            environment = workflow.state["active_attempt"]
            if args.command == "exec":
                argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
                if project.find(environment)["mode"] == "agent":
                    sys.exit(
                        agent_exec(project, project.find(environment)["name"], argv)
                    )
                sys.exit(workflow.exec_command(environment, argv))
            if args.command == "run":
                attempt = project.find(environment)
                if attempt["mode"] == "agent":
                    thread = project.thread(attempt["name"])
                    result = project.service(
                        attempt["name"],
                        "POST",
                        "/threads/" + thread["id"] + "/run",
                        {"script": args.script},
                    )
                else:
                    result = workflow.submit(environment, args.script)
            elif args.command == "list":
                print(format_attempts(workflow.list_attempts()))
                return
            elif args.command == "status":
                result = workflow.status(args.run_id)
            elif args.command == "logs":
                print(workflow.logs(args.run_id)["logs"], end="")
                return
            elif args.command == "cancel":
                result = workflow.cancel(args.run_id)
            elif args.command == "publish":
                result = workflow.publish(args.run_id)
            elif args.command == "artifacts":
                target = Path(args.destination).resolve()
                if (target / args.run_id).exists():
                    raise CaligoError(
                        "Destination already exists: " + str(target / args.run_id)
                    )
                target.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory() as tmp:
                    archive = Path(tmp) / "run.tar.gz"
                    with archive.open("wb") as output:
                        workflow.archive(args.run_id, output)
                    unpack(archive, tmp)
                    shutil.move(str(Path(tmp) / args.run_id), target / args.run_id)
                result = {"path": str(target / args.run_id)}
        print(json.dumps(result, indent=2))
    except (CaligoError, OSError, ValueError) as error:
        print("caligo: " + str(error), file=sys.stderr)
        sys.exit(1)


def agent_command(project, args):
    if args.command == "start":
        return project.start_agent(args.name, args.backend, args.goal, not args.once)
    if args.command == "service":
        if args.operation in {"start", "status"}:
            return project.service(args.name, "GET", "/health")
        return project.service(args.name, "POST", "/shutdown", {})
    if args.command == "threads":
        attempt = project.find(args.name)
        return {
            "threads": [
                thread
                for thread in project.service(args.name)["threads"]
                if thread["attempt"] == attempt["id"]
            ]
        }
    if args.command == "attach":
        return attach(project, args.name, args.after, args.json, not args.no_follow)
    thread = project.thread(args.name)
    path = "/threads/" + thread["id"]
    if args.command == "steer":
        values = {"text": args.text, "delivery": args.delivery}
        if args.id:
            values["id"] = args.id
        return project.service(args.name, "POST", path + "/messages", values)
    return project.service(
        args.name,
        "POST",
        path + "/" + args.command,
        {"text": args.text} if args.command == "interrupt" else {},
    )


def agent_exec(project, name, argv):
    thread = project.thread(name)
    path = "/threads/" + thread["id"]
    cursor = project.service(name, "GET", path)["cursor"]
    result = queue.Queue()

    def execute():
        try:
            result.put(project.service(name, "POST", path + "/exec", {"argv": argv}))
        except Exception as error:
            result.put(error)

    worker = threading.Thread(target=execute, daemon=True)
    worker.start()
    try:
        while True:
            events = project.service(
                name, "GET", path + "/events?after=" + str(cursor)
            )["events"]
            for event in events:
                cursor = event["seq"]
                if event["type"] == "dev.output":
                    stream = (
                        sys.stderr
                        if event["data"]["channel"] == "stderr"
                        else sys.stdout
                    )
                    stream.write(
                        base64.b64decode(event["data"]["data"]).decode(errors="replace")
                    )
                    stream.flush()
            if not worker.is_alive() and len(events) < 500:
                value = result.get()
                if isinstance(value, Exception):
                    raise value
                return value["exit_code"]
            time.sleep(0.2)
    except KeyboardInterrupt:
        print(
            "Detached from dev command; inspect output with caligo attach " + name,
            file=sys.stderr,
        )
        return 130


def attach(project, name, after=0, as_json=False, follow=True):
    thread = project.thread(name)
    path = "/threads/" + thread["id"]
    try:
        while True:
            result = project.service(name, "GET", path + "/events?after=" + str(after))
            for event in result["events"]:
                after = event["seq"]
                if as_json:
                    print(json.dumps(event), flush=True)
                elif event["type"] in {"agent.text", "tool.output", "runtime.stderr"}:
                    data = event["data"]
                    print(data.get("text", data.get("delta", "")), end="", flush=True)
                elif event["type"] == "dev.output":
                    print(
                        base64.b64decode(event["data"]["data"]).decode(
                            errors="replace"
                        ),
                        end="",
                        flush=True,
                    )
                elif event["type"] != "backend.event":
                    print(
                        "[%s] %s" % (event["type"], json.dumps(event["data"])),
                        flush=True,
                    )
            if len(result["events"]) == 500:
                continue
            state = project.service(name, "GET", path)["state"]
            if not follow or state in {"STOPPED", "COMPLETED"}:
                return None
            time.sleep(0.25)
    except KeyboardInterrupt:
        print(
            "\nDetached; agent continues. Resume output with --after %d" % after,
            file=sys.stderr,
        )
