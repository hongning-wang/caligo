"""Runtime-enforced filesystem views, shared by shells and agent sessions."""

import hashlib
import os
import shutil
from pathlib import Path

from . import images
from .util import Error, locked, read_json, read_private, run, write_json, write_private

PACKAGE = Path(__file__).resolve().parent


def checked_mount(path):
    path = Path(path)
    if any(char in str(path) for char in (",", ":", "\n", "\r")):
        raise Error(
            f"Runtime mount paths cannot contain commas, colons, or newlines: {path}"
        )
    return path


def mounts(experiment):
    tree = experiment.worktree
    result = [
        (tree, "/workspace", "ro"),
        (experiment.state / "empty-git", "/workspace/.git", "ro"),
        (experiment.state / "home", "/home/worker", "rw"),
        (experiment.state / "outputs", "/outputs", "rw"),
        (PACKAGE, "/opt/caligo/caligo", "ro"),
        (experiment.state / "launcher", "/usr/local/bin/caligo", "ro"),
        (experiment.state / "mailbox/requests", "/run/caligo/requests", "rw"),
        (experiment.state / "mailbox/responses", "/run/caligo/responses", "rw"),
    ]
    for relative in experiment.config["writable"]:
        path = tree / relative
        if (
            path.is_symlink()
            or not path.is_dir()
            or not path.resolve().is_relative_to(tree)
        ):
            raise Error(f"Unsafe writable directory: {relative}")
        result.append((path, "/workspace/" + relative, "rw"))
    for name, source in experiment.config.get("datasets", {}).items():
        if not Path(source).exists():
            raise Error(f"Dataset is unavailable on the coding host: {source}")
        result.append((Path(source), "/datasets/" + name, "ro"))
    return [(checked_mount(source), target, mode) for source, target, mode in result]


ENV = {
    "HOME": "/home/worker",
    "PYTHONPATH": "/opt/caligo",
    "CALIGO_SANDBOX": "1",
    "CALIGO_OUTPUTS": "/outputs",
    "TERM": "xterm-256color",
    "PYTHONDONTWRITEBYTECODE": "1",
    "SHELL": "/bin/bash",
    "PS1": r"caligo:\w\$ ",
}


class Docker:
    def __init__(self, experiment):
        self.experiment = experiment
        project_id = read_json(experiment.project.state / "project.json")["id"]
        self.name = (
            "caligo-"
            + hashlib.sha256((project_id + experiment.name).encode()).hexdigest()[:20]
        )

    def ensure(self):
        image = self.experiment.config["coding_image"]
        image_id = images.docker_image(image)
        status = run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}} {{.Image}}",
                self.name,
            ],
            check=False,
        )
        if status.returncode == 0 and status.stdout.startswith("true "):
            if status.stdout.split()[1] != image_id:
                raise Error(
                    f"Coding image changed. Exit the shell, run caligo stop {self.experiment.name}, then attach again."
                )
            return
        if status.returncode == 0:
            run(["docker", "rm", self.name])
        args = [
            "docker",
            "run",
            "--detach",
            "--name",
            self.name,
            "--label",
            "caligo.experiment=" + self.experiment.name,
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--init",
            "--pids-limit",
            "512",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,mode=1777",
            "--tmpfs",
            "/var/tmp:rw,nosuid,nodev,mode=1777",
            "--workdir",
            "/workspace",
        ]
        for source, target, mode in mounts(self.experiment):
            value = f"type=bind,src={source},dst={target}"
            args += ["--mount", value + (",readonly" if mode == "ro" else "")]
        for key, value in ENV.items():
            args += ["--env", key + "=" + value]
        args += [
            "--entrypoint",
            "python3",
            image,
            "-c",
            "import time; time.sleep(10**9)",
        ]
        run(args, timeout=120)
        write_json(
            self.experiment.state / "image.json",
            {"image": image, "digest": image_id},
        )

    def command(self, command, *, interactive=False):
        return [
            "docker",
            "exec",
            *(["-it"] if interactive else []),
            self.name,
            "sh",
            "-c",
            'export PATH=/home/worker/.local/bin:$PATH; exec "$@"',
            "caligo",
            *command,
        ]

    def stop(self):
        run(["docker", "rm", "--force", self.name], check=False)


class Apptainer:
    def __init__(self, experiment):
        self.experiment = experiment

    def ensure(self):
        image = self.experiment.config["coding_image"]
        destination, digest = images.apptainer_image(image)
        write_json(
            self.experiment.state / "image.json",
            {"image": image, "digest": digest, "path": str(destination)},
        )

    def command(self, command, *, interactive=False):
        args = [
            "apptainer",
            "exec",
            "--containall",
            "--cleanenv",
            "--no-privs",
            "--no-eval",
            "--no-mount",
            "cwd,hostfs,bind-paths",
            "--pwd",
            "/workspace",
            "--home",
            str(self.experiment.state / "home") + ":/home/worker",
        ]
        for source, target, mode in mounts(self.experiment):
            if target != "/home/worker":
                args += ["--bind", f"{source}:{target}:{mode}"]
        for key, value in ENV.items():
            if key != "HOME":
                args += ["--env", key + "=" + value]
        prefix = ["env"]
        for key in os.environ:
            if key.startswith(
                (
                    "APPTAINER_BIND",
                    "APPTAINER_MOUNT",
                    "APPTAINERENV_",
                    "SINGULARITY_BIND",
                    "SINGULARITY_MOUNT",
                    "SINGULARITYENV_",
                )
            ):
                prefix += ["-u", key]
        return [
            *prefix,
            *args,
            read_json(self.experiment.state / "image.json")["path"],
            "sh",
            "-c",
            'export PATH=/home/worker/.local/bin:$PATH; exec "$@"',
            "caligo",
            *command,
        ]

    def stop(self):
        pass  # Processes are owned by the tmux session, not a container daemon.


def runtime(experiment):
    if os.environ.get("CALIGO_RUNTIME") == "apptainer":
        return Apptainer(experiment)
    if shutil.which("docker"):
        return Docker(experiment)
    if shutil.which("apptainer"):
        return Apptainer(experiment)
    raise Error("Install Docker for local coding, or Apptainer on a Linux coding host.")


def prepare(experiment):
    # Only this user's explicitly chosen editor config is copied, never their home.
    vimrc = Path.home() / ".vimrc"
    home = experiment.state / "home"
    if vimrc.is_file() and read_private(home, ".vimrc") is None:
        write_private(home, ".vimrc", vimrc.read_bytes())
    if read_private(home, ".caligo-bashrc") is None:
        write_private(
            home,
            ".caligo-bashrc",
            "[ ! -r /etc/bash.bashrc ] || . /etc/bash.bashrc\n"
            "PS1='caligo:\\w\\$ '\n"
            '[ ! -r "$HOME/.bashrc" ] || . "$HOME/.bashrc"\n',
        )
    # Native agents launch login shells, which may reset PATH and PYTHONPATH.
    launcher = experiment.state / "launcher"
    launcher.write_text(
        '#!/usr/bin/env python3\nimport os, sys\nsys.path.insert(0, "/opt/caligo")\n'
        'os.environ["CALIGO_SANDBOX"] = "1"\nfrom caligo.cli import main\nmain()\n'
    )
    launcher.chmod(0o755)
    backend = runtime(experiment)
    with locked(experiment.state / "runtime.lock"):
        backend.ensure()
    run(
        backend.command(
            [
                "python3",
                "-c",
                "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ is required'",
            ]
        )
    )
    return backend


def require_agent(backend, agent):
    result = run(backend.command(["sh", "-c", f"command -v {agent}"]), check=False)
    if result.returncode:
        raise Error(
            f"{agent} must be installed in coding_image. Use caligo-coding:local or build your own coding image."
        )
