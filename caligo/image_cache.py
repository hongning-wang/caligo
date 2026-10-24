"""Shared Apptainer image cache. Also runs on cluster Python 3.6."""

import fcntl
import hashlib
import os
import platform
import subprocess
from pathlib import Path


def cache_directory():
    configured = os.environ.get("CALIGO_IMAGE_CACHE")
    if configured:
        return Path(configured).expanduser().resolve()
    scratch = Path.home() / "orcd/scratch"
    return (scratch if scratch.is_dir() else Path.home() / ".cache") / "caligo/images"


def ensure_sif(image, definition=None, recipe=""):
    cache = cache_directory()
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((image + platform.machine() + recipe).encode()).hexdigest()
    destination = cache / (key + ".sif")
    checksum = cache / (key + ".sha256")
    with open(str(cache / (key + ".lock")), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not destination.exists():
            temporary = cache / (key + ".partial.sif")
            log_path = cache / (key + ".log")
            source = (
                str(definition)
                if definition
                else (image if "://" in image else "docker://" + image)
            )
            args = [
                "apptainer",
                "build",
                "--force",
                "--mksquashfs-args",
                "-processors 1",
            ]
            if definition:
                args.append("--fakeroot")
            with open(str(log_path), "w") as log:
                result = subprocess.call(
                    args + [str(temporary), source],
                    cwd=str(Path(definition).parent) if definition else None,
                    stdout=log,
                    stderr=log,
                )
            if result:
                if temporary.exists():
                    temporary.unlink()
                raise RuntimeError(
                    "Image preparation failed: " + log_path.read_text()[-4000:]
                )
            temporary.replace(destination)
            if checksum.exists():
                checksum.unlink()
        if not checksum.exists():
            digest = hashlib.sha256()
            with destination.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            checksum.write_text("sha256:" + digest.hexdigest())
    return destination, checksum.read_text().strip()
