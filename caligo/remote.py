"""Delegate coding to an SSH host while keeping the admin repository local."""

import hashlib
import shlex
import tarfile
from pathlib import Path

from . import config
from .transport import SSH
from .util import locked, read_json, write_json
from .process import run
from .errors import Error


def forward(project, args, argv):
    settings = config.load((project.root / "caligo.toml").read_text())
    if settings["coding"] == "local":
        return None
    if args.action == "create":
        commit, _ = project.baseline()
    ssh = SSH(settings["coding"])
    with locked(project.state / "remote.lock"):
        cached = read_json(project.state / "remote.json", {})
        if cached.get("host") != ssh.host:
            probe = (
                "for p in python3.14 python3.13 python3.12 python3.11 python3; do "
                "command -v $p >/dev/null 2>&1 || continue; "
                "$p -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null && "
                "{ command -v $p; break; }; done"
            )
            python = ssh.script(probe, check=False).stdout.strip()
            if not python:
                python = ssh.script(
                    "module load miniforge >/dev/null 2>&1 && "
                    "python3 -c 'import sys; assert sys.version_info >= (3,11); print(sys.executable)'",
                    check=False,
                ).stdout.strip()
            if not python.startswith("/") or "\n" in python:
                raise Error(
                    "Remote coding requires Python 3.11+ (or a miniforge environment module)."
                )
            home = ssh.script("printf '%s' \"$HOME\"").stdout
            project_id = read_json(project.state / "project.json")["id"]
            cached = {
                "host": ssh.host,
                "python": python,
                "root": home + "/.cache/caligo/projects/" + project_id,
            }
        runtime = deploy_runtime(project, ssh, cached)
        if args.action == "create":
            bundle = project.state / "admin.bundle"
            bundle.unlink(missing_ok=True)
            run(["git", "bundle", "create", bundle, "--all"], cwd=project.root)
            remote_bundle = cached["root"] + "/admin.bundle"
            ssh.put(bundle, remote_bundle)
            repository = cached["root"] + "/admin"
            script = (
                "set -e; if test -d " + shlex.quote(repository + "/.git") + "; then "
                "git -C "
                + shlex.quote(repository)
                + " fetch "
                + shlex.quote(remote_bundle)
                + "; "
                "else git clone --no-checkout "
                + shlex.quote(remote_bundle)
                + " "
                + shlex.quote(repository)
                + "; fi; "
                "git -C "
                + shlex.quote(repository)
                # This checkout is a controller-owned mirror. Replace leftover
                # init files, while retaining .caligo and its experiment worktrees.
                + " checkout --force --detach "
                + shlex.quote(commit)
            )
            ssh.script(script)
        repository = cached["root"] + "/admin"
        cached["repository"] = repository
        write_json(project.state / "remote.json", cached)
    # Replace local --project flags; all other arguments remain literal argv items.
    forwarded = []
    index = 0
    while index < len(argv):
        if argv[index] == "--project":
            index += 2
        elif argv[index].startswith("--project="):
            index += 1
        elif argv[index] in ("-e", "--experiment"):
            forwarded.extend(argv[index : index + 2])
            index += 2
        elif not argv[index].startswith("-"):
            forwarded.extend(argv[index:])
            break
        else:
            forwarded.append(argv[index])
            index += 1
    environment = [
        "env",
        "PYTHONPATH=" + runtime,
        "CALIGO_REMOTE=1",
        "CALIGO_CODING_HOST=" + ssh.host,
        "CALIGO_RUNTIME=apptainer",
    ]
    base = [*environment, cached["python"], "-m", "caligo", "--project", repository]
    if args.action in ("login", "auth"):
        # Login can precede the first experiment (and its Git mirror).
        present = ssh.script(
            "test -f " + shlex.quote(repository + "/caligo.toml"), check=False
        )
        if present.returncode:
            ssh.script("mkdir -p " + shlex.quote(repository))
            ssh.put(project.root / "caligo.toml", repository + "/caligo.toml")
        ssh.run([*base, "init"])
    if args.action == "create":
        ssh.run([*base, "init"])
    return ssh.interactive(
        [*base, *forwarded], tty=args.action in ("attach", "auth", "login")
    )


def deploy_runtime(project, ssh, cached):
    """Upload the complete source package under its content identity."""
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    sources = sorted([*package.rglob("*.py"), *(package / "coding").glob("*")])
    for path in sources:
        digest.update(path.relative_to(package).as_posix().encode() + path.read_bytes())
    code_id = digest.hexdigest()[:16]
    runtime = cached["root"] + "/.caligo-runtime/" + code_id
    if cached.get("code_id") != code_id:
        ssh.script("mkdir -p " + shlex.quote(runtime))
        archive = project.state / "runtime.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            for path in sources:
                output.add(
                    path, arcname="caligo/" + path.relative_to(package).as_posix()
                )
        ssh.put(archive, runtime + "/runtime.tar.gz")
        ssh.script(
            "tar -xzf "
            + shlex.quote(runtime + "/runtime.tar.gz")
            + " -C "
            + shlex.quote(runtime)
        )
        cached["code_id"] = code_id
    return runtime
