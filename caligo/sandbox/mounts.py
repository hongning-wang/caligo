"""Coding sandbox filesystem and environment policy."""

from pathlib import Path
from ..errors import Error

PACKAGE = Path(__file__).resolve().parent.parent


def checked_mount(path):
    path = Path(path)
    if any(char in str(path) for char in (",", ":", "\n", "\r")):
        raise Error(
            f"Runtime mount paths cannot contain commas, colons, or newlines: {path}"
        )
    return path


def mounts(experiment):
    tree = experiment.worktree
    result = [
        (tree, "/workspace", "ro"),
        (experiment.state / "empty-git", "/workspace/.git", "ro"),
        (experiment.state / "home", "/home/worker", "rw"),
        (experiment.state / "outputs", "/outputs", "rw"),
        (PACKAGE, "/opt/caligo/caligo", "ro"),
        (experiment.state / "launcher", "/usr/local/bin/caligo", "ro"),
        (experiment.state / "mailbox/requests", "/run/caligo/requests", "rw"),
        (experiment.state / "mailbox/responses", "/run/caligo/responses", "rw"),
    ]
    for relative in experiment.config["writable"]:
        path = tree / relative
        if (
            path.is_symlink()
            or not path.is_dir()
            or not path.resolve().is_relative_to(tree)
        ):
            raise Error(f"Unsafe writable directory: {relative}")
        result.append((path, "/workspace/" + relative, "rw"))
    datasets = experiment.config.get(
        "coding_datasets", experiment.config.get("datasets", {})
    )
    for name, source in datasets.items():
        if not Path(source).exists():
            raise Error(f"Dataset is unavailable on the coding host: {source}")
        result.append((Path(source), "/datasets/" + name, "ro"))
    return [(checked_mount(source), target, mode) for source, target, mode in result]


ENV = {
    "HOME": "/home/worker",
    "PYTHONPATH": "/opt/caligo",
    "CALIGO_SANDBOX": "1",
    "CALIGO_OUTPUTS": "/outputs",
    "TERM": "xterm-256color",
    "PYTHONDONTWRITEBYTECODE": "1",
    "SHELL": "/bin/bash",
    "PS1": r"caligo:\w\$ ",
}
