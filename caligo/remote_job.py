"""Standalone Slurm helper; intentionally compatible with cluster Python 3.6.

This runs outside the workload container. It never executes experiment code on a
login node. The controller uploads it with each validated source snapshot.
"""

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import time
from pathlib import Path

if __package__:
    from .image_cache import ensure_sif
else:
    from image_cache import ensure_sif


def write(path, value):
    temporary = str(path) + ".tmp"
    with open(temporary, "w") as stream:
        json.dump(value, stream)
    os.replace(temporary, str(path))


def read(path):
    with open(str(path)) as stream:
        return json.load(stream)


def command(args):
    return subprocess.check_output(args, universal_newlines=True).strip()


def partitions(text, hardware):
    eligible = set()
    for line in text.splitlines():
        fields = line.split("|")
        if len(fields) != 4:
            continue
        partition, availability, cpus, gres = fields
        if availability != "up" or int(cpus.rstrip("+")) < hardware["cpus"]:
            continue
        devices = re.findall(r"gpu(?::([^:,()]+))?:(\d+)", gres)
        count = sum(
            int(number)
            for kind, number in devices
            if not hardware.get("gpu_type") or kind == hardware["gpu_type"]
        )
        if hardware["gpus"]:
            if count < hardware["gpus"]:
                continue
        elif devices:
            continue
        eligible.add(partition.rstrip("*"))
    return sorted(eligible)


def extract_source(archive, target):
    target.mkdir()
    with tarfile.open(str(archive)) as source:
        for member in source.getmembers():
            destination = (target / member.name).resolve()
            if str(destination) != str(target) and not str(destination).startswith(
                str(target) + "/"
            ):
                raise ValueError("Archive path escapes source directory")
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("Unsupported archive entry")
            if member.issym():
                link = (destination.parent / member.linkname).resolve()
                if os.path.isabs(member.linkname) or not str(link).startswith(
                    str(target) + "/"
                ):
                    raise ValueError("Archive symlink escapes source directory")
        source.extractall(str(target))


def prepare(root, spec):
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source"
    if not source.exists():
        extract_source(root / "source.tar.gz", source)
    for directory in ("home", "outputs", "temporary"):
        (root / directory).mkdir(exist_ok=True)
    image, digest = ensure_sif(spec["config"]["base_image"])
    spec["image"] = str(image)
    spec["image_digest"] = digest
    section = spec["config"][spec["phase"]]
    info = command(["sinfo", "--noheader", "--Node", "--format=%P|%a|%c|%G"])
    eligible = partitions(info, section["hardware"])
    if section.get("partitions"):
        eligible = [item for item in eligible if item in section["partitions"]]
    if not eligible:
        raise ValueError(
            "No available partition matches the requested hardware/partitions"
        )
    write(root / "spec.json", spec)
    batch = root / "batch.sh"
    batch.write_text(
        "#!/bin/bash\nset -e\n"
        "if ! command -v apptainer >/dev/null 2>&1; then\n"
        "  source /etc/profile\n  module load apptainer\nfi\nexec "
        + " ".join(
            shlex.quote(item)
            for item in [
                sys.executable,
                str(root / "remote_job.py"),
                "execute",
                str(root),
            ]
        )
        + "\n"
    )
    hardware = section["hardware"]
    args = [
        "sbatch",
        "--parsable",
        "--nodes=1",
        "--ntasks=1",
        "--cpus-per-task=" + str(hardware["cpus"]),
        "--partition=" + ",".join(eligible),
        "--job-name=caligo-" + spec["id"],
        "--output=" + str(root / "slurm.log"),
        "--export=NONE",
        "--no-requeue",
    ]
    if hardware["gpus"]:
        gpu = "gpu:" + (hardware["gpu_type"] + ":" if hardware.get("gpu_type") else "")
        args += ["--gres=" + gpu + str(hardware["gpus"])]
    for option in ("account", "time", "qos"):
        if section.get(option):
            args += ["--" + option + "=" + section[option]]
    if (root / "decision.json").exists() and not read(root / "decision.json").get(
        "run"
    ):
        raise ValueError("Submission cancelled during image preparation")
    job_id = command(args + [str(batch)]).split(";")[0]
    write(root / "allocation.json", {"id": job_id})
    return {
        "slurm_id": job_id,
        "partitions": eligible,
        "image_digest": spec["image_digest"],
    }


def execute(root, spec):
    write(root / "ready.json", {"slurm_id": os.environ["SLURM_JOB_ID"]})
    deadline = time.monotonic() + 300
    while not (root / "decision.json").exists():
        if time.monotonic() > deadline:
            write(
                root / "exit.json",
                {"returncode": 125, "error": "Controller did not grant execution"},
            )
            return 125
        time.sleep(0.5)
    if not read(root / "decision.json").get("run"):
        return 0
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
        str(root / "home") + ":/home/worker",
        "--workdir",
        str(root / "temporary"),
        "--bind",
        str(root / "source") + ":/workspace:ro",
        "--bind",
        str(root / "outputs") + ":/outputs:rw",
        "--env",
        "CALIGO_OUTPUTS=/outputs",
    ]
    for relative in spec["config"]["writable"]:
        args += [
            "--bind",
            str(root / "source" / relative) + ":/workspace/" + relative + ":rw",
        ]
    for name, source in spec["config"].get("datasets", {}).items():
        args += ["--bind", source + ":/datasets/" + name + ":ro"]
    if spec["config"][spec["phase"]]["hardware"]["gpus"]:
        args += ["--nv"]
        if "CUDA_VISIBLE_DEVICES" in os.environ:
            args += [
                "--env",
                "CUDA_VISIBLE_DEVICES=" + os.environ["CUDA_VISIBLE_DEVICES"],
            ]
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(
            (
                "APPTAINER_BIND",
                "APPTAINER_MOUNT",
                "SINGULARITY_BIND",
                "SINGULARITY_MOUNT",
                "APPTAINERENV_",
                "SINGULARITYENV_",
            )
        ):
            del env[key]
    started = time.time()
    try:
        code = subprocess.call(args + [spec["image"]] + spec["command"], env=env)
        result = {
            "returncode": code,
            "started": started,
            "finished": time.time(),
            "image_digest": spec["image_digest"],
        }
    except OSError as exc:
        result = {"returncode": 127, "error": str(exc)}
    write(root / "exit.json", result)
    return result["returncode"]


def status(root, offset):
    result = {"ready": (root / "ready.json").exists(), "offset": offset, "log": ""}
    if (root / "exit.json").exists():
        result["exit"] = read(root / "exit.json")
    if (root / "slurm.log").exists():
        with open(str(root / "slurm.log"), "rb") as stream:
            stream.seek(offset)
            block = stream.read(256 * 1024)
            result.update(
                offset=offset + len(block), log=base64.b64encode(block).decode()
            )
    allocation = root / "allocation.json"
    if allocation.exists() and "exit" not in result:
        job_id = read(allocation)["id"]
        output = command(["squeue", "-h", "-j", job_id, "-o", "%T"])
        result["state"] = output or "FINISHED"
        if not output:
            try:
                result["accounting"] = command(
                    [
                        "sacct",
                        "-n",
                        "-X",
                        "-j",
                        job_id,
                        "--format=State,ExitCode",
                        "--parsable2",
                    ]
                )
            except subprocess.CalledProcessError:
                result["accounting"] = ""
    return result


def artifacts(root, spec):
    path = root / "artifacts.tar.gz"
    with tarfile.open(str(path), "w:gz", dereference=False) as archive:
        archive.add(str(root / "outputs"), arcname="outputs")
        for relative in spec["config"]["writable"]:
            archive.add(str(root / "source" / relative), arcname="worktree/" + relative)
    return {"path": str(path)}


def main():
    operation, directory = sys.argv[1:3]
    root = Path(directory).resolve()
    spec = read(root / "spec.json")
    if operation == "prepare":
        result = prepare(root, spec)
    elif operation == "execute":
        sys.exit(execute(root, spec))
    elif operation == "status":
        result = status(root, int(sys.argv[3]))
    elif operation == "grant":
        write(root / "decision.json", {"run": True})
        result = {}
    elif operation == "cancel":
        write(root / "decision.json", {"run": False})
        if (root / "allocation.json").exists():
            allocation = read(root / "allocation.json")
            subprocess.call(["scancel", allocation["id"]])
        result = {}
    elif operation == "artifacts":
        result = artifacts(root, spec)
    else:
        raise ValueError("Unknown operation")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
