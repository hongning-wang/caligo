from pathlib import Path, PurePosixPath
import re

from .common import CaligoError, is_bucket, valid_id


def relative(value, label, single=False):
    if not isinstance(value, str) or not value or "\\" in value:
        raise CaligoError("%s must be a relative path" % label)
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in {"", ".", ".."} for x in value.split("/")):
        raise CaligoError("%s must be a normalized relative path" % label)
    if any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", x) for x in p.parts):
        raise CaligoError("%s contains an unsupported path component" % label)
    if single and len(p.parts) != 1:
        raise CaligoError("%s must name a root folder; subfolders may be created inside it" % label)
    return value


def compute_resources(spec):
    cpu = spec.get("cpu")
    if not isinstance(cpu, int) or isinstance(cpu, bool) or cpu < 1:
        raise CaligoError("cpu must be a positive integer")
    resources = {"cpus": cpu}
    if "gpu" not in spec:
        return resources
    value = spec["gpu"]
    gpu = re.fullmatch(r"(?:([A-Za-z][A-Za-z0-9_.-]*)(?::([1-9][0-9]*))?|([1-9][0-9]*))", value) if isinstance(value, str) else None
    if not gpu:
        raise CaligoError("gpu must be a GPU type/count without the 'gpu:' prefix, e.g. 'h200:4' or '4'; omit gpu for CPU-only jobs")
    resources.update(gpus=int(gpu[2] or gpu[3] or 1), gres="gpu:" + value)
    return resources


def load(path):
    import tomllib
    path = Path(path).resolve()
    try:
        spec = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise CaligoError(str(e)) from e
    allowed = {"name", "cpu", "gpu", "image", "editable", "readable", "executable", "outputs", "execute", "locations", "fixed", "eval", "inputs", "remote", "scheduler", "runtime", "addons"}
    if set(spec) - allowed:
        raise CaligoError("Unknown manifest fields: " + ", ".join(sorted(set(spec) - allowed)))
    from .addons import validate_settings
    validate_settings(spec.get("addons", {}))
    for key in ("name", "cpu", "image", "editable", "readable", "executable", "outputs", "locations"):
        if key not in spec:
            raise CaligoError("Missing manifest field: " + key)
    valid_id(spec["name"])
    locations = spec["locations"]
    if not isinstance(locations, dict) or set(locations) != {"coding", "dev", "experiments"}:
        raise CaligoError("locations must explicitly specify coding, dev, and experiments")
    for key, value in locations.items():
        if not isinstance(value, str) or not value or value.startswith("-") or any(c.isspace() for c in value):
            raise CaligoError("locations.%s must be 'local' or an SSH host alias" % key)
    compute_resources(spec)
    for key in ("image", "outputs"):
        if not isinstance(spec[key], str) or not spec[key] or "\n" in spec[key]:
            raise CaligoError(key + " must be a nonempty string")
    areas = []
    for kind in ("editable", "readable"):
        if not isinstance(spec[kind], list):
            raise CaligoError(kind + " must be a list of folder names")
        areas += [relative(x, kind, single=True) for x in spec[kind]]
    areas += [relative(spec["executable"], "executable", single=True)]
    if len(areas) != len(set(areas)):
        raise CaligoError("Editable, readable, and executable folders must be disjoint")
    spec.setdefault("fixed", "fixed")
    relative(spec["fixed"], "fixed")
    if spec["fixed"].split("/")[0] in areas:
        raise CaligoError("Fixed setup must be outside every exposed folder")
    if spec.get("execute"):
        script_path(spec, spec["execute"])
    if spec.get("eval"):
        relative(spec["eval"], "eval")
    spec.setdefault("inputs", {})
    if not isinstance(spec["inputs"], dict) or set(spec["inputs"]) - set(areas):
        raise CaligoError("inputs must map declared folders to filesystem paths or bucket URIs")
    for source in spec["inputs"].values():
        if not isinstance(source, str) or not source:
            raise CaligoError("Input sources must be nonempty strings")
    spec.setdefault("remote", {})
    if not isinstance(spec["remote"], dict) or set(spec["remote"]) - {"root", "python"}:
        raise CaligoError("remote supports root and python; choose hosts explicitly in locations")
    if "python" in spec["remote"]:
        python = spec["remote"]["python"]
        if not isinstance(python, str) or not python.strip() or python.startswith("-") or "\n" in python or "\0" in python:
            raise CaligoError("remote.python must name a Python 3.11+ executable, not a shell command")
    spec.setdefault("scheduler", {})
    allowed_scheduler = {"type", "partition", "account", "time", "memory", "constraint", "qos", "setup"}
    if not isinstance(spec["scheduler"], dict) or set(spec["scheduler"]) - allowed_scheduler:
        raise CaligoError("Unknown scheduler option")
    spec["scheduler"].setdefault("type", "slurm")
    if spec["scheduler"]["type"] != "slurm":
        raise CaligoError("The supported scheduler is slurm")
    for key, value in spec["scheduler"].items():
        if not isinstance(value, (str, int)) or isinstance(value, bool) or "\n" in str(value):
            raise CaligoError("Invalid scheduler value: " + key)
    if "setup" in spec["scheduler"] and not isinstance(spec["scheduler"]["setup"], str):
        raise CaligoError("scheduler.setup must be a shell command string")
    spec.setdefault("runtime", "apptainer")
    if spec["runtime"] not in {"apptainer", "docker"}:
        raise CaligoError("runtime must be apptainer or docker")
    if not is_bucket(spec["outputs"]):
        output = Path(spec["outputs"]).expanduser()
        if not output.is_absolute():
            relative(spec["outputs"], "outputs")
            if output.parts[0] in areas or output.parts[0] in {"repo.git", "fixed", "worktrees", "runs", "attempts"}:
                raise CaligoError("outputs must be separate from working files and controller state")
    return spec


def script_path(spec, selected=None):
    selected = selected or spec.get("execute")
    if not selected:
        raise CaligoError("Select a script: caligo run %s/train.py (or set execute in project.toml)" % spec["executable"])
    relative(selected, "experiment script")
    if not selected.startswith(spec["executable"] + "/"):
        raise CaligoError("Experiment scripts must be inside " + spec["executable"] + "/")
    return selected
