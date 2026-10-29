"""The deliberately small caligo.toml schema."""

import tomllib
from pathlib import PurePosixPath

from .util import Error

STARTER = """# Research jobs use base_image; human and agent sessions use coding_image.
base_image = "python:3.12-slim"
coding_image = "caligo-coding:local"
coding = "local"
writable = ["experiments"]
logs = ".caligo/logs"

[dev]
hosts = []
[dev.hardware]
cpus = 2
gpus = 0

[run]
hosts = []
command = ["python3", "evaluation/run.py"]
[run.hardware]
cpus = 4
gpus = 0
"""


def string(value, name):
    if not isinstance(value, str) or not value or any(c in value for c in "\n\r\0"):
        raise Error(f"{name} must be a nonempty string without control characters.")
    return value


def strings(value, name, *, empty=False):
    if not isinstance(value, list) or (not value and not empty):
        raise Error(
            f"{name} must be an {'optionally empty ' if empty else ''}array of strings."
        )
    return [string(item, name) for item in value]


def load(text):
    try:
        data = tomllib.loads(text)
        for key in ("base_image", "coding_image", "coding", "logs"):
            string(data[key], key)
        writable = strings(data["writable"], "writable", empty=True)
        for index, path in enumerate(writable):
            if any(
                path == other
                or path.startswith(other + "/")
                or other.startswith(path + "/")
                for other in writable[:index]
            ):
                raise Error(
                    "Writable directories must not duplicate or overlap each other."
                )
        for path in writable:
            parsed = PurePosixPath(path)
            if (
                parsed.is_absolute()
                or any(char in path for char in ",:")
                or str(parsed) != path
                or any(part in ("..", ".git", ".caligo") for part in parsed.parts)
                or path in (".", "evaluation", "caligo.toml")
                or path.startswith("evaluation/")
            ):
                raise Error(f"Unsafe or protected writable directory: {path}")
        for phase in ("dev", "run"):
            section = data[phase]
            hosts = strings(section["hosts"], f"{phase}.hosts", empty=True)
            if any(
                host.startswith("-") or any(c.isspace() for c in host) for host in hosts
            ):
                raise Error(f"{phase}.hosts must contain SSH config names.")
            if "partitions" in section:
                strings(section["partitions"], f"{phase}.partitions")
            for option in ("account", "qos", "time"):
                if option in section:
                    string(section[option], f"{phase}.{option}")
            hardware = section["hardware"]
            for field, minimum in (("cpus", 1), ("gpus", 0)):
                value = hardware[field]
                if type(value) is not int or value < minimum:
                    raise Error(
                        f"{phase}.hardware.{field} must be an integer >= {minimum}."
                    )
            if "gpu_type" in hardware:
                kind = string(hardware["gpu_type"], f"{phase}.hardware.gpu_type")
                if not all(c.isalnum() or c in "_-" for c in kind):
                    raise Error(
                        "GPU types must use letters, digits, underscores, or hyphens."
                    )
        strings(data["run"]["command"], "run.command")
        data.setdefault("datasets", {})
        for field in ("datasets", "coding_datasets"):
            if field not in data:
                continue
            datasets = data[field]
            if not isinstance(datasets, dict):
                raise Error(
                    f"{field} must be a table of names and absolute host paths."
                )
            for name, path in datasets.items():
                if any(char in string(path, field + "." + name) for char in ",:"):
                    raise Error("Dataset mount paths cannot contain commas or colons.")
                if not name or not all(char.isalnum() or char in "_-" for char in name):
                    raise Error(
                        "Dataset names must use letters, digits, underscores, or hyphens."
                    )
                if not PurePosixPath(path).is_absolute():
                    raise Error(
                        "Dataset paths must be absolute paths available on each host."
                    )
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise Error(f"Invalid caligo.toml: {exc}") from exc
    return data
