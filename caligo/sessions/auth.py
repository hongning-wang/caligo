"""Project login and experiment credential sharing."""

import os
import uuid
from pathlib import Path
from .. import sandbox
from ..util import locked, read_private, write_private, private_parent
from ..errors import Error
from ..process import interactive


def credential_path(agent):
    return ".codex/auth.json" if agent == "codex" else ".claude/.credentials.json"


def authenticate(project, agent, *, import_auth=False):
    home = project.state / "auth/home"
    home.mkdir(parents=True, exist_ok=True)
    relative = credential_path(agent)
    with locked(project.state / "auth/login.lock"):
        if import_auth:
            source = Path.home() / relative
            if not source.is_file():
                raise Error(
                    f"No file-based {agent} credentials found; run caligo login {agent} without --import."
                )
            write_private(home, relative, source.read_bytes())
            return 0
        backend = sandbox.prepare_auth(project)
        try:
            sandbox.require_agent(backend, agent)
            command = (
                [
                    "codex",
                    "-c",
                    'cli_auth_credentials_store="file"',
                    "login",
                    "--device-auth",
                ]
                if agent == "codex"
                else ["claude", "auth", "login"]
            )
            return interactive(backend.command(command, interactive=True))
        finally:
            backend.stop()


def share_project_credentials(experiment, agent):
    """Use the project login without sharing conversations or CLI settings."""
    home = experiment.project.state / "auth/home"
    if not home.exists():
        return
    relative = credential_path(agent)
    with locked(experiment.project.state / "auth/login.lock"):
        content = read_private(home, relative)
        if content is None:
            return
        if agent == "claude":
            # Claude replaces its credentials atomically, so keep a private copy.
            write_private(experiment.state / "home", relative, content)
            return
        with private_parent(experiment.state / "home", relative) as (fd, name):
            temporary = ".caligo-" + uuid.uuid4().hex
            # Codex refreshes credentials in place. A hard link shares
            # those writes without exposing the project home or other sessions.
            os.link(home / relative, temporary, dst_dir_fd=fd, follow_symlinks=False)
            try:
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
