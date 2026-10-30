"""Git-backed experiments and immutable, validated submission snapshots."""

import hashlib
import json
import os
import posixpath
import shutil
import stat
import tarfile
import uuid
from pathlib import Path

from . import config
from .util import identifier, locked, read_json, timestamp, write_json
from .process import run
from .errors import Error


def entries(root):
    """Walk directory descriptors so a changing sandbox cannot redirect host reads."""
    for folder, directories, files, fd in os.fwalk(root, follow_symlinks=False):
        relative = Path(folder).relative_to(root)
        directories[:] = sorted(d for d in directories if d not in (".git", ".caligo"))
        for name in sorted(directories + files):
            if name not in (".git", ".caligo"):
                yield (
                    fd,
                    name,
                    (relative / name).as_posix(),
                    os.stat(name, dir_fd=fd, follow_symlinks=False),
                )


def link_target(fd, name, relative):
    target = os.readlink(name, dir_fd=fd)
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), target))
    if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
        raise Error(f"Symlink escapes the repository: {relative}")
    return target


def manifest(root):
    """Include untracked files, modes and links, but never controller/Git state."""
    result = {}
    for fd, name, relative, info in entries(root):
        if stat.S_ISDIR(info.st_mode):
            result[relative] = {"directory": True}
        elif stat.S_ISLNK(info.st_mode):
            result[relative] = {"link": link_target(fd, name, relative)}
        elif stat.S_ISREG(info.st_mode):
            source = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            with os.fdopen(source, "rb") as stream:
                actual = os.fstat(stream.fileno())
                if not stat.S_ISREG(actual.st_mode):
                    raise Error(f"Repository file changed type: {relative}")
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            result[relative] = {"sha256": digest, "mode": stat.S_IMODE(actual.st_mode)}
        else:
            raise Error(f"Unsupported repository file: {relative}")
    for relative, item in result.items():
        if "link" in item:
            try:
                contained = (
                    (Path(root) / relative)
                    .resolve()
                    .is_relative_to(Path(root).resolve())
                )
            except RuntimeError as exc:
                raise Error(f"Cyclic repository symlink: {relative}") from exc
            if not contained:
                raise Error(f"Symlink escapes the repository: {relative}")
    return result


def copy_source(source, destination):
    destination.mkdir()
    for fd, name, relative, info in entries(source):
        target = destination / relative
        if stat.S_ISDIR(info.st_mode):
            target.mkdir(exist_ok=True)
        elif stat.S_ISLNK(info.st_mode):
            target.symlink_to(link_target(fd, name, relative))
        elif stat.S_ISREG(info.st_mode):
            source_fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
            with os.fdopen(source_fd, "rb") as stream, target.open("wb") as output:
                actual = os.fstat(stream.fileno())
                if not stat.S_ISREG(actual.st_mode):
                    raise Error(f"Repository file changed type: {relative}")
                shutil.copyfileobj(stream, output)
            target.chmod(stat.S_IMODE(actual.st_mode))
        else:
            raise Error(f"Unsupported repository file: {relative}")


def allowed(path, writable):
    return any(path.startswith(directory + "/") for directory in writable)


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state = self.root / ".caligo"

    @classmethod
    def discover(cls, start=None):
        here = Path(start or Path.cwd()).resolve()
        for directory in (here, *here.parents):
            if (directory / ".caligo" / "project.json").exists():
                return cls(directory)
        raise Error("No Caligo project found. Run caligo init in your repository.")

    def init(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").exists():
            run(["git", "init", self.root])
        path = self.root / "caligo.toml"
        if not path.exists():
            path.write_text(config.STARTER)
        ignore = self.root / ".gitignore"
        text = ignore.read_text() if ignore.exists() else ""
        if ".caligo/" not in text.splitlines():
            ignore.write_text(text.rstrip() + "\n.caligo/\n")
        if not (self.state / "project.json").exists():
            write_json(
                self.state / "project.json",
                {"id": uuid.uuid4().hex, "root": str(self.root)},
            )
        self.state.chmod(0o700)
        return self.root

    def experiment(self, name):
        identifier(name)
        location = self.state / "experiments" / name
        metadata = read_json(location / "experiment.json")
        if metadata is None:
            raise Error(f"Unknown experiment: {name}")
        return Experiment(self, location, metadata)

    def current(self):
        cwd = Path.cwd().resolve()
        for path in (self.state / "experiments").glob("*/experiment.json"):
            data = read_json(path)
            if cwd.is_relative_to(Path(data["worktree"])):
                return Experiment(self, path.parent, data)
        raise Error(
            "Select an experiment with --experiment NAME, or cd into its worktree."
        )

    def baseline(self):
        revision = run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=self.root, check=False
        )
        if revision.returncode:
            raise Error("Commit your admin baseline before creating an experiment.")
        commit = revision.stdout.strip()
        result = run(
            ["git", "show", f"{commit}:caligo.toml"], cwd=self.root, check=False
        )
        if result.returncode:
            raise Error(
                "caligo.toml is missing from the committed admin baseline (HEAD). "
                "From the admin repository, run `git add caligo.toml` and "
                '`git commit -m "Add Caligo configuration"`, then retry '
                "`caligo create experiment <name>`. Staging alone is not enough."
            )
        return commit, config.load(result.stdout)

    def create(self, name):
        identifier(name)
        if name == "admin":
            raise Error("admin is reserved for the human-maintained base repository.")
        with locked(self.state / "project.lock"):
            location = self.state / "experiments" / name
            if location.exists():
                raise Error(f"Experiment already exists: {name}")
            commit, settings = self.baseline()
            location.mkdir(parents=True)
            worktree = location / "worktree"
            try:
                run(
                    ["git", "worktree", "add", "--detach", worktree, commit],
                    cwd=self.root,
                )
                for directory in settings["writable"]:
                    target = worktree / directory
                    if not target.resolve().is_relative_to(worktree):
                        raise Error(
                            f"Writable directory escapes the repository: {directory}"
                        )
                    target.mkdir(parents=True, exist_ok=True)
                    if target.is_symlink() or not target.resolve().is_relative_to(
                        worktree
                    ):
                        raise Error(
                            f"Writable path must be a real repository directory: {directory}"
                        )
                metadata = {
                    "name": name,
                    "worktree": str(worktree),
                    "baseline": commit,
                    "config": settings,
                    "created": timestamp(),
                }
                write_json(location / "baseline.json", manifest(worktree))
                write_json(location / "experiment.json", metadata)
                for directory in (
                    "home",
                    "outputs",
                    "mailbox/requests",
                    "mailbox/responses",
                    "jobs",
                ):
                    (location / directory).mkdir(parents=True)
                (location / "empty-git").touch()
            except Exception:
                run(
                    ["git", "worktree", "remove", "--force", worktree],
                    cwd=self.root,
                    check=False,
                )
                shutil.rmtree(location)
                raise
            return Experiment(self, location, metadata)


class Experiment:
    def __init__(self, project, state, metadata):
        self.project = project
        self.state = state
        self.metadata = metadata
        self.name = metadata["name"]
        self.worktree = Path(metadata["worktree"])
        self.config = metadata["config"]

    def goal(self):
        return read_json(self.state / "goal.json", {}).get("text", "")

    def set_goal(self, text):
        if not text.strip() or "\0" in text:
            raise Error("A goal must contain text.")
        entry = {"text": text, "time": timestamp()}
        with locked(self.state / "goal.lock"):
            write_json(self.state / "goal.json", entry)
            history = read_json(self.state / "history.json", [])
            history.append({"type": "goal", **entry})
            write_json(self.state / "history.json", history)

    def snapshot(self, job_id):
        destination = self.state / "jobs" / identifier(job_id)
        destination.mkdir(parents=True, exist_ok=False)
        tree = destination / "source"
        try:
            # Copy first, then validate the exact bytes to be submitted.
            before = manifest(self.worktree)
            copy_source(self.worktree, tree)
            current = manifest(tree)
            if before != current or manifest(self.worktree) != current:
                raise Error(
                    "Repository changed while capturing the snapshot; retry the submission."
                )
            baseline = read_json(self.state / "baseline.json")
            changed = [
                path
                for path in sorted(current.keys() | baseline.keys())
                if current.get(path) != baseline.get(path)
                and not allowed(path, self.config["writable"])
            ]
            if changed:
                raise Error("Protected files changed: " + ", ".join(changed))
            for directory in self.config["writable"]:
                path = tree / directory
                if path.is_symlink() or not path.is_dir():
                    raise Error(f"Writable mount must remain a directory: {directory}")
            digest = hashlib.sha256(
                json.dumps(current, sort_keys=True).encode()
            ).hexdigest()
            write_json(
                destination / "snapshot.json",
                {
                    "id": digest,
                    "baseline": self.metadata["baseline"],
                    "goal": self.goal(),
                    "config": self.config,
                    "created": timestamp(),
                },
            )
            with tarfile.open(
                destination / "source.tar.gz", "w:gz", dereference=False
            ) as archive:
                for path in sorted(tree.iterdir()):
                    archive.add(path, arcname=path.name)
            return destination
        except Exception:
            shutil.rmtree(destination)
            raise
