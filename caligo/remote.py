"""Standard-library controller, shipped as a zipapp to the SSH host."""

import base64
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback

from .common import (
    CaligoError,
    archive_records,
    copy_tree,
    digest,
    identifier,
    inventory,
    is_bucket,
    lock,
    now,
    read_json,
    rclone_path,
    run,
    transfer,
    valid_id,
    write_json,
)
from .controller_api import dispatch
from .manifest import compute_resources, declared_areas, script_path
from . import runtime, scheduler


def unpack(archive, destination):
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            p = Path(member.name)
            if (
                p.is_absolute()
                or ".." in p.parts
                or not (member.isfile() or member.isdir())
            ):
                raise CaligoError("Unsafe input archive member: " + member.name)
        # Members have been checked explicitly for older remote Python versions.
        if hasattr(tarfile, "data_filter"):

            def safe_permissions(member, path):
                filtered = tarfile.data_filter(member, path)
                if filtered is not None:
                    # data_filter normally makes read-only files writable.
                    # Preserve regular rwx bits for workspace permissions and
                    # snapshot hashes, while still stripping special bits.
                    filtered.mode = member.mode & 0o777
                return filtered

            tar.extractall(destination, filter=safe_permissions)
        else:
            tar.extractall(destination)


class Controller:
    def __init__(self, root, project):
        self.root = Path(root).expanduser().resolve()
        self.project = self.root / "projects" / valid_id(project)

    @property
    def spec(self):
        return read_json(self.project / "project.json")

    def bootstrap(self, archive, development=False):
        self.project.parent.mkdir(parents=True, exist_ok=True)
        with lock(self.root / "locks" / (self.project.name + ".lock")):
            if self.project.exists():
                return {"project": str(self.project), "existing": True}
            with tempfile.TemporaryDirectory(
                dir=self.project.parent, prefix=".prepare-"
            ) as temp:
                stage = Path(temp)
                unpack(archive, stage)
                spec = read_json(stage / "project.json")
                seed = stage / "seed"
                for area, source in spec["inputs"].items():
                    if is_bucket(source):
                        (seed / area).mkdir(exist_ok=True)
                        transfer(source, seed / area)
                for area in declared_areas(spec):
                    (seed / area).mkdir(parents=True, exist_ok=True)
                    inventory(seed / area)
                fixed = stage / "fixed"
                fixed.mkdir(exist_ok=True)
                from .permissions import protect

                for path in sorted(
                    [fixed, *fixed.rglob("*")], key=lambda p: len(p.parts), reverse=True
                ):
                    protect(path)
                fixed_inventory = inventory(fixed)
                if spec.get("eval") and not (fixed / spec["eval"]).is_file():
                    raise CaligoError(
                        "eval script is missing from fixed setup: " + spec["eval"]
                    )
                seed_modes = {
                    area: {
                        ".": {"mode": (seed / area).stat().st_mode & 0o777},
                        **inventory(seed / area),
                    }
                    for area in declared_areas(spec)
                }
                write_json(stage / "permissions.json", seed_modes)
                # Build a new history from declared folders only. Never import
                # the source .git, including unreachable objects and reflogs.
                run(["git", "init", "--initial-branch=main", str(seed)])
                run(["git", "-C", str(seed), "add", "--force", "."])
                run(
                    [
                        "git",
                        "-C",
                        str(seed),
                        "-c",
                        "user.name=Caligo setup",
                        "-c",
                        "user.email=caligo@localhost",
                        "commit",
                        "--allow-empty",
                        "-m",
                        "Initialize exposed caligo files",
                    ]
                )
                run(
                    [
                        "git",
                        "clone",
                        "--bare",
                        "--no-hardlinks",
                        str(seed),
                        str(stage / "repo.git"),
                    ]
                )
                run(
                    [
                        "git",
                        "--git-dir",
                        str(stage / "repo.git"),
                        "config",
                        "--remove-section",
                        "remote.origin",
                    ]
                )
                for path in [seed, *seed.rglob("*")]:
                    if path.is_dir():
                        path.chmod(path.stat().st_mode | 0o700)
                if sys.platform == "darwin":
                    subprocess.run(["/bin/chmod", "-RN", str(seed)], check=True)
                shutil.rmtree(seed)
                if development:
                    image_evidence = {"resolved": None, "runtime": "native"}
                    write_json(stage / "development.json", {"native": True})
                else:
                    image_evidence = self.resolve_image(spec)
                write_json(
                    stage / "provenance.json",
                    {
                        "image": image_evidence,
                        "fixed_sha256": digest(fixed_inventory),
                        "created_at": now(),
                    },
                )
                stage.rename(self.project)
            return {"project": str(self.project), "existing": False}

    def resolve_image(self, spec):
        image = runtime.resolve_image(spec, self.root / "images")
        evidence = {"resolved": image}
        if spec["runtime"] == "apptainer":
            import hashlib

            h = hashlib.sha256()
            with open(image, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            evidence["sha256"] = h.hexdigest()
        return evidence

    def create(self, mode, name, addons=None):
        if mode not in {"human", "agent"}:
            raise CaligoError("mode must be human or agent")
        spec = self.spec
        from .addons import validate_selection

        addons = validate_selection(addons or [], spec, check_tools=True)
        env_id = identifier("attempt-")
        workspace = self.project / "worktrees" / env_id
        valid_id(name)
        run(["git", "check-ref-format", "--branch", name])
        with lock(self.project / "repository.lock"):
            run(
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    str(self.project / "repo.git"),
                    str(workspace),
                ]
            )
            run(["git", "-C", str(workspace), "remote", "remove", "origin"])
            run(["git", "-C", str(workspace), "switch", "-C", name])
            git_dir = self.project / "attempt-state" / env_id / "git"
            git_dir.parent.mkdir(parents=True)
            shutil.move(str(workspace / ".git"), git_dir)
            run(
                [
                    "git",
                    "--git-dir",
                    str(git_dir),
                    "config",
                    "core.worktree",
                    str(workspace),
                ]
            )
        for area in declared_areas(spec):
            (workspace / area).mkdir(exist_ok=True)
        seed_modes = read_json(self.project / "permissions.json")
        for area, records in seed_modes.items():
            for relative, record in records.items():
                if record.get("type") == "dir":
                    (workspace / area / relative).mkdir(parents=True, exist_ok=True)
        from .permissions import apply, protect
        from .preparation import agent_guide

        guide = workspace / "Agent.MD"
        guide.write_text(agent_guide(spec))
        protect(guide)
        apply(workspace, spec, seed_modes)
        record = {
            "id": env_id,
            "mode": mode,
            "workspace": str(workspace),
            "branch": env_id,
            "created_at": now(),
        }
        record.update(name=name, branch=name, git_dir=str(git_dir), addons=addons)
        write_json(self.project / "attempts" / (env_id + ".json"), record)
        return record

    def migrate_human(self, env_id):
        record = self.environment(env_id)
        workspace = Path(record["workspace"])
        from .permissions import apply

        modes = (
            read_json(self.project / "permissions.json")
            if (self.project / "permissions.json").exists()
            else {}
        )
        apply(workspace, self.spec, modes)
        record["mode"] = "human"
        write_json(self.project / "attempts" / (valid_id(env_id) + ".json"), record)
        return record

    def environment(self, env_id):
        return read_json(self.project / "attempts" / (valid_id(env_id) + ".json"))

    def check_workspace(self, workspace):
        spec = self.spec
        areas = declared_areas(spec)
        if set(p.name for p in workspace.iterdir()) - {*areas, "Agent.MD"}:
            raise CaligoError("Worktree contains undeclared root entries")
        for area in areas:
            inventory(workspace / area)

    def run_dir(self, run_id):
        pointer = read_json(self.project / "runs" / (valid_id(run_id) + ".json"))
        return Path(pointer["path"])

    def record_base(self, spec):
        if (self.project / "development.json").exists() or is_bucket(spec["outputs"]):
            base = self.project / "records"
        else:
            base = Path(spec["outputs"]).expanduser()
            if not base.is_absolute():
                base = self.project / base
            base = base.resolve()
            protected = [
                self.project / name
                for name in ("worktrees", "repo.git", "fixed", "attempts", "runs")
            ]
            if any(base == p or p in base.parents for p in protected):
                raise CaligoError("Outputs overlap protected project state")
        return base

    def snapshot(self, env_id, selected):
        spec = self.spec
        selected = script_path(spec, selected)
        env = self.environment(env_id)
        workspace = Path(env["workspace"])
        run_id = identifier("run-")
        base = self.record_base(spec)
        record_dir = base / run_id
        record_dir.mkdir(parents=True)
        write_json(
            self.project / "runs" / (run_id + ".json"), {"path": str(record_dir)}
        )
        record = {
            "id": run_id,
            "environment": env_id,
            "attempt": env["name"],
            "project": spec["name"],
            "worktree": str(workspace),
            "created_at": now(),
            "script": selected,
            "spec": spec,
            "mode": env["mode"],
            "revision": self.project.name,
            "state": "PREPARING",
        }
        write_json(record_dir / "run.json", record)
        try:
            with lock(self.project / "locks" / (env_id + ".lock")):
                self.check_workspace(workspace)
                if not (workspace / selected).is_file():
                    raise CaligoError(
                        "Script does not exist in the worktree: " + selected
                    )
                files = {}
                for area in declared_areas(spec):
                    files[area] = copy_tree(
                        workspace / area, record_dir / "inputs" / area
                    )
                record["git"] = {
                    "commit": self.git_metadata(env, ["rev-parse", "HEAD"]),
                    "status": self.git_metadata(
                        env, ["status", "--porcelain=v1", "--untracked-files=all"]
                    ),
                    "branch": self.git_metadata(env, ["branch", "--show-current"]),
                }
                record["files"] = files
                record["inputs_sha256"] = digest(files)
            provenance = read_json(self.project / "provenance.json")
            if digest(inventory(self.project / "fixed")) != provenance["fixed_sha256"]:
                raise CaligoError(
                    "Fixed setup changed after project creation; create a new project"
                )
            record["provenance"] = provenance
            record["state"] = "PREPARED"
            write_json(record_dir / "run.json", record)
            (record_dir / "artifacts").mkdir()
            return record
        except Exception as e:
            record.update(state="PREPARATION_FAILED", error=str(e))
            write_json(record_dir / "run.json", record)
            raise CaligoError(
                "Run %s retained at %s: %s" % (run_id, record_dir, e)
            ) from e

    def submit(self, env_id, selected, agent):
        record = self.snapshot(env_id, selected)
        return self.schedule(record["id"], agent)

    def submit_dev(self, env_id, argv, origin, agent):
        if not argv or not all(isinstance(arg, str) for arg in argv):
            raise CaligoError("Dev execution requires a command")
        env = self.environment(env_id)
        run_id = valid_id(origin["id"])
        record_dir = self.record_base(self.spec) / run_id
        (record_dir / "artifacts").mkdir(parents=True)
        record = {
            "id": run_id,
            "environment": origin["environment"],
            "attempt": origin["attempt"],
            "project": self.spec["name"],
            "kind": "dev",
            "dev_environment": env_id,
            "worktree": env["workspace"],
            "script": None,
            "argv": argv,
            "spec": self.spec,
            "created_at": now(),
            "state": "PREPARED",
        }
        record.update({key: origin[key] for key in ("thread", "turn") if key in origin})
        write_json(record_dir / "run.json", record)
        write_json(
            self.project / "runs" / (run_id + ".json"), {"path": str(record_dir)}
        )
        return self.schedule(run_id, agent)

    def schedule(self, run_id, agent):
        record_dir = self.run_dir(run_id)
        record = read_json(record_dir / "run.json")
        spec = record["spec"]
        try:
            record["state"] = "SUBMITTING"
            write_json(record_dir / "run.json", record)
            script = record_dir / "job.sh"
            preamble = "#!/bin/bash\nset -e\n"
            if spec["scheduler"].get("setup"):
                preamble += spec["scheduler"]["setup"] + "\n"
            script.write_text(
                preamble
                + "exec "
                + shlex.join(
                    [
                        sys.executable,
                        str(agent),
                        "_remote-job",
                        str(self.root),
                        self.project.name,
                        run_id,
                    ]
                )
                + "\n"
            )
            job = scheduler.submit(spec, script, record_dir / "logs.txt", run_id)
            write_json(
                record_dir / "submission.json", {"job_id": job, "submitted_at": now()}
            )
            self.publish(run_id)
            return self.status(run_id, refresh=False)
        except Exception as e:
            if not (record_dir / "submission.json").exists():
                record.update(state="SUBMISSION_UNKNOWN", error=str(e))
                write_json(record_dir / "run.json", record)
            raise CaligoError(
                "Run %s retained at %s: %s" % (run_id, record_dir, e)
            ) from e

    def receive(self, archive, agent):
        """Import a frozen run from the coding host, preserving its provenance."""
        self.project.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.project.parent) as tmp:
            stage = Path(tmp)
            unpack(archive, stage)
            record = read_json(stage / "run.json")
            run_id = valid_id(record["id"])
            spec = record["spec"]
            actual = {
                area: inventory(stage / "inputs" / area) for area in record["files"]
            }
            if actual != record["files"] or digest(actual) != record["inputs_sha256"]:
                raise CaligoError(
                    "Transferred inputs do not match the submitted snapshot"
                )
            fixed_hash = digest(inventory(stage / "fixed"))
            if fixed_hash != record["provenance"]["fixed_sha256"]:
                raise CaligoError("Transferred fixed setup does not match its snapshot")
            with lock(self.root / "locks" / (self.project.name + ".lock")):
                self.project.mkdir(parents=True, exist_ok=True)
                fixed = self.project / "fixed"
                if fixed.exists():
                    if digest(inventory(fixed)) != fixed_hash:
                        raise CaligoError(
                            "Fixed setup differs from the existing project"
                        )
                else:
                    copy_tree(stage / "fixed", fixed)
                destination = self.record_base(spec) / run_id
                if destination.exists():
                    raise CaligoError(
                        "Run already received; inspect its status before retrying"
                    )
                destination.mkdir(parents=True)
                copy_tree(stage / "inputs", destination / "inputs")
                (destination / "artifacts").mkdir()
                record["kind"] = "experiments"
                record["state"] = "PREPARED"
                write_json(destination / "run.json", record)
                write_json(
                    self.project / "runs" / (run_id + ".json"),
                    {"path": str(destination)},
                )
            try:
                record["provenance"]["image"] = self.resolve_image(spec)
                write_json(destination / "run.json", record)
                return self.schedule(run_id, agent)
            except Exception as error:
                if not (destination / "submission.json").exists():
                    saved = read_json(destination / "run.json")
                    if saved["state"] != "SUBMISSION_UNKNOWN":
                        saved.update(state="PREPARATION_FAILED", error=str(error))
                        write_json(destination / "run.json", saved)
                raise

    def environment_archive(self, env_id, output, fixed=True):
        workspace = Path(self.environment(env_id)["workspace"])
        self.check_workspace(workspace)
        spec = self.spec
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            for area in declared_areas(spec):
                copy_tree(workspace / area, stage / "seed" / area)
            if fixed:
                copy_tree(self.project / "fixed", stage / "fixed")
            else:
                (stage / "fixed").mkdir()
                spec.pop("eval", None)
            spec["inputs"] = {}
            write_json(stage / "project.json", spec)
            with tarfile.open(fileobj=output, mode="w|gz") as tar:
                for item in stage.iterdir():
                    tar.add(item, arcname=item.name)

    def snapshot_archive(self, run_id, output):
        directory = self.run_dir(run_id)
        with tarfile.open(fileobj=output, mode="w|gz") as tar:
            for name in ("inputs", "run.json"):
                tar.add(directory / name, arcname=name)
            tar.add(self.project / "fixed", arcname="fixed")

    def git_command(self, env):
        return ["git", "--git-dir", env["git_dir"], "--work-tree", env["workspace"]]

    def git_metadata(self, env, argv):
        if env["mode"] == "agent":
            from .containers import git_metadata

            return git_metadata(self, env, argv)
        return run(self.git_command(env) + ["-c", "core.fsmonitor=false"] + argv)

    def container_dev(self, env, argv, outputs, emit=None, gpu=False):
        image = self.resolve_image(self.spec)["resolved"]
        command = runtime.command(
            self.spec,
            image,
            Path(env["workspace"]),
            argv,
            outputs=outputs,
            gpu=gpu and bool(compute_resources(self.spec).get("gpus")),
        )
        if emit:
            from .common import stream_command

            return stream_command(command, emit, env=runtime.host_environment())
        return subprocess.call(
            command, stdin=subprocess.DEVNULL, env=runtime.host_environment()
        )

    def native_shell(self, env_id, argv, session_dir, coding=False, emit=None):
        env = self.environment(env_id)
        workspace = Path(env["workspace"])
        spec = self.spec
        outputs = self.project / "dev-outputs" / env_id
        outputs.mkdir(parents=True, exist_ok=True)
        variables = dict(
            os.environ,
            CALIGO_WORKSPACE=str(workspace),
            CALIGO_EXECUTABLE=str(workspace / spec["executable"]),
            CALIGO_OUTPUTS=str(outputs),
            CALIGO_SOCKET=str(Path(session_dir) / "control.sock"),
            PATH=str(Path(session_dir) / "bin")
            + os.pathsep
            + os.environ.get("PATH", ""),
            PYTHONDONTWRITEBYTECODE="1",
        )
        variables.pop("CALIGO_FIXED", None)
        variables.update(
            CALIGO_ATTEMPT=env["name"],
            CALIGO_CLIENT=str(Path(session_dir) / "bin/caligo"),
            BASH_ENV=str(Path(session_dir) / "bashrc"),
            BASH_SILENCE_DEPRECATION_WARNING="1",
        )
        command = argv or [
            "bash",
            "--noprofile",
            "--rcfile",
            str(Path(session_dir) / "bashrc"),
        ]
        if env.get("git_dir"):
            variables.update(GIT_DIR=env["git_dir"], GIT_WORK_TREE=str(workspace))
        if env["mode"] == "agent":
            if coding:
                raise CaligoError(
                    "Agent attempts use caligo attach; they do not expose host shells"
                )
            return self.container_dev(env, command, outputs, emit)
        if coding:
            from .coding import launch

            return launch(self, env, argv, session_dir, variables)
        from .permissions import human_command

        command = human_command(workspace, spec, command, [self.project / "fixed"])
        if emit:
            from .common import stream_command

            return stream_command(
                command, emit, cwd=workspace / spec["executable"], env=variables
            )
        return subprocess.call(
            command, cwd=workspace / spec["executable"], env=variables
        )

    def execute(self, run_id):
        record_dir = self.run_dir(run_id)
        record = read_json(record_dir / "run.json")
        spec = record["spec"]
        restart = int(os.environ.get("SLURM_RESTART_COUNT", "0"))
        outcome = {
            "state": "RUNNING",
            "started_at": now(),
            "exit_code": None,
            "restart_count": restart,
        }
        write_json(record_dir / "outcome.json", outcome)
        history = record_dir / "executions" / str(restart) / "outcome.json"
        write_json(history, outcome)
        code = 1
        try:
            if record.get("kind") == "dev":
                workspace = Path(record["worktree"])
                variables = dict(
                    os.environ,
                    CALIGO_WORKSPACE=str(workspace),
                    CALIGO_EXECUTABLE=str(workspace / spec["executable"]),
                    CALIGO_OUTPUTS=str(record_dir / "artifacts"),
                    PYTHONDONTWRITEBYTECODE="1",
                )
                for key in (
                    "CALIGO_FIXED",
                    "CALIGO_SOCKET",
                    "GIT_DIR",
                    "GIT_WORK_TREE",
                ):
                    variables.pop(key, None)
                env = self.environment(record["dev_environment"])
                if env["mode"] == "agent":
                    code = self.container_dev(
                        env, record["argv"], record_dir / "artifacts", gpu=True
                    )
                else:
                    from .permissions import human_command

                    command = human_command(
                        workspace, spec, record["argv"], [self.project / "fixed"]
                    )
                    code = subprocess.call(
                        command,
                        cwd=workspace / spec["executable"],
                        stdin=subprocess.DEVNULL,
                        env=variables,
                    )
                outcome.update(
                    state="SUCCEEDED" if code == 0 else "FAILED", exit_code=code
                )
                return code
            actual = {
                area: inventory(record_dir / "inputs" / area)
                for area in record["files"]
            }
            if actual != record["files"]:
                raise CaligoError("Submitted inputs were modified")
            if (
                digest(inventory(self.project / "fixed"))
                != record["provenance"]["fixed_sha256"]
            ):
                raise CaligoError("Fixed setup was modified")
            working = self.project / "execution" / run_id
            if not working.exists():
                working.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(
                    dir=working.parent, prefix=".prepare-"
                ) as tmp:
                    prepared = Path(tmp) / "workspace"
                    prepared.mkdir()
                    for area in record["files"]:
                        copy_tree(record_dir / "inputs" / area, prepared / area)
                    prepared.rename(working)
            image = record["provenance"]["image"]["resolved"]
            common = dict(
                outputs=record_dir / "artifacts",
                gpu=bool(compute_resources(spec).get("gpus")),
            )
            command = runtime.command(
                spec,
                image,
                working,
                runtime.program(working / record["script"]),
                **common,
            )
            code = subprocess.call(
                command, stdin=subprocess.DEVNULL, env=runtime.host_environment()
            )
            outcome["experiment_exit_code"] = code
            if code == 0 and spec.get("eval"):
                # Do not let participant-created links resolve against the
                # evaluator's private mounts in the next container.
                outcome["artifacts_sha256"] = digest(
                    inventory(record_dir / "artifacts")
                )
                evaluation = record_dir / "evaluation"
                evaluation.mkdir(exist_ok=True)
                command = runtime.command(
                    spec,
                    image,
                    working,
                    runtime.program(Path("/fixed") / spec["eval"]),
                    fixed=self.project / "fixed",
                    evaluation=evaluation,
                    **common,
                )
                code = subprocess.call(
                    command, stdin=subprocess.DEVNULL, env=runtime.host_environment()
                )
                outcome["evaluation_exit_code"] = code
            outcome.update(state="SUCCEEDED" if code == 0 else "FAILED", exit_code=code)
        except Exception as e:
            traceback.print_exc()
            code = code or 1
            outcome.update(state="FAILED", error=str(e), exit_code=code)
        finally:
            outcome["finished_at"] = now()
            write_json(history, outcome)
            write_json(record_dir / "outcome.json", outcome)
            self.publish(run_id)
        return code

    def publish(self, run_id):
        record_dir = self.run_dir(run_id)
        spec = read_json(record_dir / "run.json")["spec"]
        if not is_bucket(spec["outputs"]):
            return
        with lock(self.project / "locks" / ("publish-" + run_id + ".lock")):
            target = spec["outputs"].rstrip("/") + "/" + run_id
            receipt = record_dir / "publication.json"
            try:
                write_json(receipt, {"state": "PUBLISHING", "at": now()})
                transfer(record_dir, target)
                write_json(receipt, {"state": "PUBLISHED", "at": now()})
                run(
                    [
                        "rclone",
                        "copyto",
                        str(receipt),
                        rclone_path(target + "/publication.json"),
                    ]
                )
            except Exception as e:
                write_json(receipt, {"state": "FAILED", "error": str(e), "at": now()})

    def status(self, run_id, refresh=True):
        record_dir = self.run_dir(run_id)
        record = read_json(record_dir / "run.json")
        result = {
            key: record[key]
            for key in ("id", "environment", "state", "script", "created_at")
        }
        result["worktree"] = record["worktree"]
        for key in (
            "git",
            "inputs_sha256",
            "kind",
            "execution_location",
            "attempt",
            "project",
            "mode",
            "revision",
            "thread",
            "turn",
        ):
            if key in record:
                result[key] = record[key]
        result["location"] = str(record_dir)
        if "error" in record:
            result["error"] = record["error"]
        receipt = record_dir / "submission.json"
        if receipt.exists():
            result.update(read_json(receipt))
            result["state"] = "SUBMITTED"
            if refresh:
                try:
                    evidence = scheduler.status(result["job_id"])
                    result["scheduler"] = evidence
                    write_json(record_dir / "scheduler.json", evidence)
                except CaligoError as e:
                    result["poll_error"] = str(e)
        outcome = record_dir / "outcome.json"
        if outcome.exists():
            result.update(read_json(outcome))
        evidence = result.get("scheduler", {})
        if evidence.get("state") in scheduler.TERMINAL:
            if evidence["state"] != "COMPLETED":
                result["state"] = evidence["state"]
            elif not outcome.exists() or result["state"] == "RUNNING":
                result["state"] = "FAILED"
                result["error"] = "Scheduler completed without a recorded outcome"
            self.publish(run_id)
        elif evidence.get("state") and evidence["state"] != "UNKNOWN":
            # A previous execution's outcome must not end a requeued run.
            result["state"] = evidence["state"]
            result["exit_code"] = None
            result.pop("finished_at", None)
            result.pop("error", None)
        publication = record_dir / "publication.json"
        if publication.exists():
            result["publication"] = read_json(publication)
        return result

    def tail(self, run_id, offset=0):
        path = self.run_dir(run_id) / "logs.txt"
        if not path.exists():
            return {"data": "", "offset": offset}
        with path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(65536)
            return {"data": base64.b64encode(data).decode(), "offset": stream.tell()}

    def cancel(self, run_id):
        receipt = read_json(self.run_dir(run_id) / "submission.json")
        scheduler.cancel(receipt["job_id"])
        return {"id": run_id, "cancel_requested": True, "job_id": receipt["job_id"]}

    def list(self, env_id=None):
        environments = (
            [self.environment(env_id)]
            if env_id
            else [
                read_json(p) for p in sorted((self.project / "attempts").glob("*.json"))
            ]
        )
        runs = [
            self.status(p.stem, refresh=False)
            for p in sorted((self.project / "runs").glob("*.json"))
        ]
        if env_id:
            runs = [record for record in runs if record["environment"] == env_id]
        return {"attempts": environments, "runs": runs}


def main():
    try:
        action = sys.argv[1]
        if action in {
            "_remote-job",
            "_remote-archive",
            "_remote-service-archive",
            "_remote-snapshot",
            "_remote-workspace",
            "_remote-dev-workspace",
            "_remote-native-shell",
            "_remote-coding-shell",
        }:
            controller = Controller(sys.argv[2], sys.argv[3])
            if action == "_remote-service-archive":
                from .service import archive_run

                archive_run(controller, sys.argv[4], sys.stdout.buffer)
                return
            if action == "_remote-native-shell":
                sys.exit(
                    controller.native_shell(sys.argv[4], sys.argv[6:], sys.argv[5])
                )
            if action == "_remote-coding-shell":
                sys.exit(
                    controller.native_shell(
                        sys.argv[4], sys.argv[6:], sys.argv[5], coding=True
                    )
                )
            if action == "_remote-snapshot":
                controller.snapshot_archive(sys.argv[4], sys.stdout.buffer)
                return
            if action in {"_remote-workspace", "_remote-dev-workspace"}:
                controller.environment_archive(
                    sys.argv[4], sys.stdout.buffer, fixed=action == "_remote-workspace"
                )
                return
            if action == "_remote-job":
                sys.exit(controller.execute(sys.argv[4]))
            path = controller.run_dir(sys.argv[4])
            # Only regular records/artifacts; no controller fixed setup or
            # execution workspace, and no symlinks to arbitrary host files.
            archive_records(path, sys.stdout.buffer)
            return
        request = json.load(sys.stdin)
        if request["action"] == "probe":
            result = {
                "home": str(Path.home()),
                "python": sys.version,
                "tools": {
                    x: shutil.which(x)
                    for x in (
                        "git",
                        "sbatch",
                        "squeue",
                        "sacct",
                        "apptainer",
                        "singularity",
                        "docker",
                        "rclone",
                    )
                },
            }
        else:
            controller = Controller(request["root"], request["project"])
            action = request["action"]
            if action == "submit":
                result = controller.submit(
                    request["environment"], request.get("script"), sys.argv[0]
                )
            elif action == "list":
                result = controller.list(request.get("environment"))
            else:
                result = dispatch(controller, action, request, sys.argv[0])
                if result is NotImplemented:
                    raise CaligoError("Unknown controller action: " + action)
        print(json.dumps({"ok": True, "result": result}))
    except (CaligoError, OSError, ValueError, KeyError) as e:
        if len(sys.argv) > 1 and sys.argv[1] == "_remote-rpc":
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print("caligo: " + str(e), file=sys.stderr)
        sys.exit(1)
