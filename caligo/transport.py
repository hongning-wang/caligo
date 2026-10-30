"""SSH transport. Credentials and SSH configuration never enter a sandbox."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .errors import Error
from .process import run, interactive


class SSH:
    def __init__(self, host):
        if not host or host.startswith("-") or any(c.isspace() for c in host):
            raise Error("Expected an SSH config name.")
        self.host = host
        self.local = host == os.environ.get("CALIGO_CODING_HOST")
        self.options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        control = os.environ.get("CALIGO_SSH_CONTROL_PATH")
        if not control and list((Path.home() / ".ssh").glob("caligo-*")):
            control = "~/.ssh/caligo-%C"
        if control:
            self.options += ["-o", f"ControlPath={control}"]

    def script(self, script, *, input=None, timeout=120, check=True):
        if self.local:
            return run(
                ["bash", "-lc", script], input=input, timeout=timeout, check=check
            )
        return run(
            ["ssh", *self.options, self.host, script],
            input=input,
            timeout=timeout,
            check=check,
        )

    # Preserve the original transport API for existing integrations.
    command = script

    def run(self, args, **kwargs):
        """Execute literal arguments; shell quoting belongs at this boundary."""
        return self.script(shlex.join([str(arg) for arg in args]), **kwargs)

    def interactive(self, args, *, tty=False):
        command = shlex.join([str(arg) for arg in args])
        if self.local:
            return interactive(["bash", "-lc", command])
        return interactive(
            ["ssh", *self.options, *(["-tt"] if tty else []), self.host, command]
        )

    def put(self, local, remote):
        if self.local:
            shutil.copyfile(local, remote)
            return
        with open(local, "rb") as source:
            result = subprocess.run(
                ["ssh", *self.options, self.host, f"cat > {shlex.quote(remote)}"],
                stdin=source,
                capture_output=True,
                check=False,
            )
        if result.returncode:
            raise Error(
                f"{self.host}: {result.stderr.decode(errors='replace').strip()}"
            )

    def get(self, remote, local):
        if self.local:
            shutil.copyfile(remote, local)
            return
        with open(local, "wb") as destination:
            result = subprocess.run(
                ["ssh", *self.options, self.host, f"cat {shlex.quote(remote)}"],
                stdout=destination,
                stderr=subprocess.PIPE,
                check=False,
            )
        if result.returncode:
            raise Error(
                f"{self.host}: {result.stderr.decode(errors='replace').strip()}"
            )
