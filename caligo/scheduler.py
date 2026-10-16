import re
from pathlib import Path

from .common import CaligoError, run
from .manifest import compute_resources


TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL", "DEADLINE", "REVOKED"}


def submit(spec, script, log, name):
    resources = compute_resources(spec)
    settings = spec["scheduler"]
    args = ["sbatch", "--parsable", "--nodes=1", "--ntasks=1", "--no-requeue", "--job-name=" + name,
            "--output=" + str(log), "--error=" + str(log), "--chdir=" + str(Path(script).parent),
            "--cpus-per-task=" + str(resources["cpus"])]
    if resources.get("gpus"):
        args += ["--gres=" + resources["gres"]]
    for key, flag in {"partition": "partition", "account": "account", "time": "time", "memory": "mem", "constraint": "constraint", "qos": "qos"}.items():
        if key in settings:
            args += ["--%s=%s" % (flag, settings[key])]
    output = run(args + [str(script)])
    if not re.fullmatch(r"[0-9]+(?:;[A-Za-z0-9_.-]+)?", output):
        raise CaligoError("Unrecognized sbatch response; submission may have succeeded: " + output)
    return output


def status(job):
    number, _, cluster = job.partition(";")
    cluster_args = ["--clusters=" + cluster] if cluster else []
    # A completed job can vanish from squeue before appearing in sacct. Missing
    # evidence is UNKNOWN, never an instruction to resubmit.
    try:
        live = run(["squeue", "--noheader", "--jobs=" + number, "--format=%T"] + cluster_args)
    except CaligoError:
        # Slurm may reject an ID once it has aged out of the live queue.
        # Accounting remains authoritative for these completed jobs.
        live = ""
    if live:
        return {"state": live.splitlines()[0].strip(), "source": "squeue"}
    accounting = run(["sacct", "--noheader", "--parsable2", "--allocations", "--jobs=" + number, "--format=JobIDRaw,State,ExitCode"] + cluster_args)
    for line in accounting.splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0] == number:
            return {"state": parts[1].split()[0].rstrip("+"), "exit_code": parts[2], "source": "sacct"}
    return {"state": "UNKNOWN", "source": "squeue+sacct"}


def cancel(job):
    number, _, cluster = job.partition(";")
    run(["scancel"] + (["--clusters=" + cluster] if cluster else []) + [number])
