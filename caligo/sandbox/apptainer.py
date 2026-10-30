"""Apptainer coding runtime."""

import os
from . import images
from ..apptainer import exec_args, BIND_ENV_PREFIXES
from .mounts import mounts, ENV
from ..util import read_json, write_json


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
        args = exec_args(self.experiment.state / "home")
        for source, target, mode in mounts(self.experiment):
            if target != "/home/worker":
                args += ["--bind", f"{source}:{target}:{mode}"]
        for key, value in ENV.items():
            if key != "HOME":
                args += ["--env", key + "=" + value]
        prefix = ["env"]
        for key in os.environ:
            if key.startswith(BIND_ENV_PREFIXES):
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
