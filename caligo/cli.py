import argparse
import json
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile

from .common import CaligoError, copy_tree, is_bucket, write_json
from .manifest import load, script_path
from .remote import unpack
from .transport import Transport


def find_project(value=None):
    if value:
        return Path(value).resolve()
    for parent in [Path.cwd(), *Path.cwd().parents]:
        if (parent / "project.toml").is_file():
            return parent / "project.toml"
    raise CaligoError("No project.toml found; use --project PATH or caligo init PATH")


def prepare(manifest, spec, destination):
    """Stage only declared filesystem contents, never source Git history."""
    destination = Path(destination)
    seed = destination / "seed"
    seed.mkdir(parents=True)
    fixed = (manifest.parent / spec["fixed"]).resolve()
    for area in spec["editable"] + spec["readable"] + [spec["executable"]]:
        source = spec["inputs"].get(area, area)
        if is_bucket(source):
            (seed / area).mkdir()
        else:
            path = Path(source).expanduser()
            if not path.is_absolute():
                path = manifest.parent / path
            resolved = path.resolve()
            if resolved == fixed or fixed in resolved.parents or resolved in fixed.parents:
                raise CaligoError("An exposed input overlaps fixed setup: " + str(path))
            if path.exists():
                copy_tree(path, seed / area)
            else:
                raise CaligoError("Declared input folder does not exist: " + str(path))
    if fixed.exists():
        copy_tree(fixed, destination / "fixed")
    else:
        if spec.get("eval"):
            raise CaligoError("Fixed setup folder does not exist: " + str(fixed))
        (destination / "fixed").mkdir()
    if spec.get("eval") and not (destination / "fixed" / spec["eval"]).is_file():
        raise CaligoError("eval script is missing from fixed setup: " + spec["eval"])
    if spec.get("execute") and not is_bucket(spec["inputs"].get(spec["executable"], "")):
        if not (seed / spec["execute"]).is_file():
            raise CaligoError("Default execute script does not exist: " + spec["execute"])
    write_json(destination / "project.json", spec)


def init(path):
    path = Path(path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    if (path / "project.toml").exists():
        raise CaligoError("project.toml already exists")
    for folder in ("src", "data", "executable", "fixed"):
        (path / folder).mkdir(exist_ok=True)
    (path / "project.toml").write_text('''name = "caligo-project"
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
# partition = "your-partition"  # Omit to use the cluster default
# setup = "module load apptainer"  # Only if your compute nodes need it
time = "00:05:00"
memory = "1G"
''')
    script = path / "executable/train.py"
    if not script.exists():
        script.write_text('import json, os\nfrom pathlib import Path\nout = Path(os.environ["CALIGO_OUTPUTS"])\n(out / "metrics.json").write_text(json.dumps({"value": 1}))\nprint("Experiment complete")\n')
    # Do not rewrite an existing repository's ignore rules.
    ignore = path / ".gitignore"
    with ignore.open("a") as f:
        f.write("\n.caligo/\nfixed/\nresults/\n")
    return {"project": str(path / "project.toml")}


def parser():
    p = argparse.ArgumentParser(prog="caligo", description="Projects, coding attempts, and experiment runs")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Prepare a project").add_argument("path", nargs="?", default=".")
    sub.add_parser("validate", help="Validate project files").add_argument("manifest", nargs="?", default="project.toml")
    sub.add_parser("doctor", help="Inspect a remote host").add_argument("--host", required=True)
    for name in ("create", "switch", "exec", "run", "status", "logs", "artifacts", "cancel", "publish", "list"):
        command = sub.add_parser(name)
        command.add_argument("--project", help="Project manifest (otherwise found in the current directory or its parents)")
        if name in {"create", "switch"}:
            command.add_argument("name", help="Attempt name within this project")
        if name == "create":
            command.add_argument("--mode", required=True, choices=["supervisor", "user"])
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
            if args.command == "create":
                attempt = project.create(args.name, args.mode)
                sys.exit(project.enter(attempt["name"]))
            if args.command == "switch":
                sys.exit(project.enter(args.name))
            if args.command == "list" and not project.state_path.exists():
                print(format_attempts({"project": load(project.manifest)["name"], "attempts": []}))
                return
            workflow = project.workflow()
            environment = workflow.state["active_attempt"]
            if args.command == "exec":
                argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
                sys.exit(workflow.exec_command(environment, argv))
            if args.command == "run":
                if load(project.manifest) != workflow.spec:
                    raise CaligoError("Project configuration changed; use a separate project directory")
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
                    raise CaligoError("Destination already exists: " + str(target / args.run_id))
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
