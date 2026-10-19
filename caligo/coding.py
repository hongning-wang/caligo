"""Human coding uses a native interactive shell and preserved file permissions.

Agent coding is owned by service.py and containers.py; it has no native fallback.
"""

import subprocess
from pathlib import Path

from .common import CaligoError


def launch(controller, attempt, argv, session_dir, variables):
    if attempt["mode"] != "human":
        raise CaligoError("Agent attempts require the persistent container service")
    if attempt.get("addons"):
        # Installed commands already resolve through the normal host environment.
        # Human tools use their normal credentials and settings.
        from .addons import prepare

        prepare(attempt["addons"], controller.spec, session_dir, variables)
    from .permissions import human_command

    command = human_command(
        attempt["workspace"],
        controller.spec,
        argv or ["bash", "--noprofile", "--rcfile", str(Path(session_dir) / "bashrc")],
        [controller.project / "fixed"],
    )
    return subprocess.call(
        command,
        cwd=Path(attempt["workspace"]) / controller.spec["executable"],
        env=variables,
    )
