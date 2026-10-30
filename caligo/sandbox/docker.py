"""Docker coding runtime."""

import hashlib
import os
from . import images
from .mounts import mounts, ENV
from ..util import read_json, write_json
from ..errors import Error
from ..process import run


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
