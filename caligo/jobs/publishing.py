"""Controller job support."""

import shlex
import shutil
from pathlib import Path

from ..transport import SSH
from ..process import run


def publish_logs(experiment, folder):
    target = experiment.config["logs"]
    suffix = experiment.name + "/" + folder.name
    files = [
        folder / name
        for name in ("output.log", "snapshot.json", "job.json", "artifacts.tar.gz")
        if (folder / name).exists()
    ]
    if target.startswith("s3://"):
        for path in files:
            run(
                [
                    "aws",
                    "s3",
                    "cp",
                    path,
                    target.rstrip("/") + "/" + suffix + "/" + path.name,
                ]
            )
    elif ":" in target and not target.startswith("/"):
        host, directory = target.split(":", 1)
        destination = directory.rstrip("/") + "/" + suffix
        ssh = SSH(host)
        ssh.script("mkdir -p " + shlex.quote(destination))
        for path in files:
            ssh.put(path, destination + "/" + path.name)
    else:
        destination = Path(target).expanduser()
        if not destination.is_absolute():
            destination = experiment.project.root / destination
        destination = destination / suffix
        destination.mkdir(parents=True, exist_ok=True)
        for path in files:
            if path.resolve() != (destination / path.name).resolve():
                shutil.copyfile(path, destination / path.name)
