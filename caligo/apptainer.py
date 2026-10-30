"""Shared Apptainer isolation policy; also uploaded to Python 3.6 clusters."""

BIND_ENV_PREFIXES = (
    "APPTAINER_BIND",
    "APPTAINER_MOUNT",
    "SINGULARITY_BIND",
    "SINGULARITY_MOUNT",
    "APPTAINERENV_",
    "SINGULARITYENV_",
)


def exec_args(home):
    return [
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
        str(home) + ":/home/worker",
    ]


def clean_environment(environment):
    return {
        key: value
        for key, value in environment.items()
        if not key.startswith(BIND_ENV_PREFIXES)
    }
