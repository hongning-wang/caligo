"""Prepare isolated coding shells and project login environments."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from .. import config
from ..util import locked, read_private, write_private
from ..errors import Error
from ..process import run
from .docker import Docker
from .apptainer import Apptainer
from .mounts import PACKAGE, ENV, mounts, checked_mount


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


def prepare_auth(project):
    """Use the coding image with an empty workspace and a project login home."""
    state = project.state / "auth"
    for relative in (
        "home",
        "workspace",
        "outputs",
        "mailbox/requests",
        "mailbox/responses",
    ):
        (state / relative).mkdir(parents=True, exist_ok=True)
    (state / "empty-git").touch()
    (state / "workspace/.git").touch()
    settings = config.load((project.root / "caligo.toml").read_text())
    settings.update(writable=[], datasets={}, coding_datasets={})
    # @ cannot occur in an experiment name, so the login container cannot collide.
    context = SimpleNamespace(
        project=project,
        state=state,
        name="@auth",
        worktree=state / "workspace",
        config=settings,
    )
    return prepare(context)


def require_agent(backend, agent):
    result = run(backend.command(["sh", "-c", f"command -v {agent}"]), check=False)
    if result.returncode:
        raise Error(
            f"{agent} must be installed in coding_image. Use caligo-coding:local or build your own coding image."
        )


__all__ = [
    "PACKAGE",
    "ENV",
    "mounts",
    "checked_mount",
    "runtime",
    "prepare",
    "prepare_auth",
    "require_agent",
]
