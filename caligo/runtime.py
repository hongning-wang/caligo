"""Container command construction for scheduled experiments."""

import os
from pathlib import Path
import shutil

from .common import CaligoError, digest, lock, run


def host_environment():
    # Ambient runtime bind/overlay/env settings must not add undeclared host
    # paths or credentials to a caligo container.
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("APPTAINER_", "APPTAINERENV_", "SINGULARITY_", "SINGULARITYENV_")
        )
    }


def resolve_image(spec, cache):
    image = spec["image"]
    if spec["runtime"] == "docker":
        image = image.removeprefix("docker://")
        try:
            return run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
        except CaligoError:
            run(["docker", "pull", image])
        return run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    runtime = shutil.which("apptainer") or shutil.which("singularity")
    if not runtime:
        raise CaligoError("Apptainer/Singularity is not available on the remote PATH")
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (digest(image) + ".sif")
    with lock(cache / (target.name + ".lock")):
        if not target.exists():
            pending = target.with_suffix(".partial.sif")
            if Path(image).expanduser().is_file():
                shutil.copyfile(Path(image).expanduser(), pending)
            else:
                source = image if "://" in image else "docker://" + image
                run([runtime, "pull", "--force", str(pending), source])
            pending.replace(target)
            target.chmod(0o444)
    return str(target)


def command(
    spec,
    image,
    workspace,
    argv,
    fixed=None,
    outputs=None,
    gpu=False,
    evaluation=None,
):
    """Run frozen inputs with declared writable areas and separate fixed setup."""
    workspace = Path(workspace)
    executable = workspace / spec["executable"]
    mounts = [(str(workspace), str(workspace), True)]
    writable = spec["editable"] + [spec["executable"]]
    for area in [] if evaluation else writable:
        folder = workspace / area
        mounts.append(
            (str(folder), str(folder), not bool(folder.stat().st_mode & 0o200))
        )
        for path in folder.rglob("*"):
            if not path.stat().st_mode & 0o200:
                mounts.append((str(path), str(path), True))
    if fixed:
        mounts.append((str(fixed), "/fixed", True))
    if outputs:
        mounts.append((str(outputs), "/outputs", bool(evaluation)))
    if evaluation:
        mounts.append((str(evaluation), "/evaluation", False))
    environment = {
        "CALIGO_WORKSPACE": str(workspace),
        "CALIGO_EXECUTABLE": str(executable),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if fixed:
        environment["CALIGO_FIXED"] = "/fixed"
    if outputs:
        environment["CALIGO_OUTPUTS"] = "/outputs"
    if evaluation:
        environment["CALIGO_EVALUATION"] = "/evaluation"
    if spec["runtime"] == "docker":
        result = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user",
            "%s:%s" % (os.getuid(), os.getgid()),
            "--tmpfs",
            "/tmp:rw,nosuid,nodev",
            "--workdir",
            str(executable),
        ]
        result += ["-i"]
        if gpu:
            # Slurm constrains visible devices on the allocated host. Do not
            # grant GPUs to interactive caligo containers.
            devices = os.environ.get("CUDA_VISIBLE_DEVICES")
            result += ["--gpus", '"device=%s"' % devices if devices else "all"]
        for source, dest, readonly in mounts:
            if "," in source or "," in dest:
                raise CaligoError("Container bind paths may not contain commas")
            result += [
                "--mount",
                "type=bind,src=%s,dst=%s%s"
                % (source, dest, ",readonly" if readonly else ""),
            ]
        for key, value in environment.items():
            result += ["--env", key + "=" + value]
        return result + ["--entrypoint", argv[0], image] + list(argv[1:])
    runtime = shutil.which("apptainer") or shutil.which("singularity") or "apptainer"
    result = [
        runtime,
        "exec",
        "--cleanenv",
        "--containall",
        "--no-mount",
        "home,cwd,hostfs,bind-paths",
        "--pwd",
        str(executable),
    ]
    if gpu:
        result += ["--nv"]
        if "CUDA_VISIBLE_DEVICES" in os.environ:
            environment["CUDA_VISIBLE_DEVICES"] = os.environ["CUDA_VISIBLE_DEVICES"]
    for source, dest, readonly in mounts:
        if any(c in source + dest for c in ":,"):
            raise CaligoError("Apptainer bind paths may not contain colons or commas")
        result += ["--bind", "%s:%s:%s" % (source, dest, "ro" if readonly else "rw")]
    for key, value in environment.items():
        result += ["--env", key + "=" + value]
    return result + [image] + list(argv)


def program(path):
    path = str(path)
    if path.endswith(".py"):
        return ["python3", path]
    if path.endswith(".sh"):
        return ["bash", path]
    return [path]
