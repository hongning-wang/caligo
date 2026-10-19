"""Optional installed tools in human environments; agents use container images."""

from pathlib import Path
import shutil
import shlex

from .common import CaligoError


REGISTRY = {"codex": None, "code-server": None}
OPTIONS = {
    "codex": {"executable", "ca_certificate"},
    "code-server": {"executable", "port"},
}


def executable(name, options):
    requested = options.get("executable", name)
    found = shutil.which(str(Path(requested).expanduser()))
    if not found:
        raise CaligoError(
            "Add-on %s requires %s on the coding host PATH" % (name, requested)
        )
    return found


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) - REGISTRY.keys():
        raise CaligoError(
            "Unknown add-on configuration; available: " + ", ".join(REGISTRY)
        )
    for name, values in settings.items():
        if not isinstance(values, dict) or set(values) - OPTIONS[name]:
            raise CaligoError("Unknown settings for add-on " + name)
        for key, value in values.items():
            if key == "port":
                if type(value) is not int or not (value == 0 or 1024 <= value <= 65535):
                    raise CaligoError("Editor port must be 0 or between 1024 and 65535")
            elif (
                not isinstance(value, str)
                or not value.strip()
                or "\0" in value
                or "\n" in value
            ):
                raise CaligoError("Invalid add-on setting: " + name + "." + key)


def validate_selection(names, spec, check_tools=False):
    if not isinstance(names, list) or any(
        not isinstance(name, str) or name not in REGISTRY for name in names
    ):
        raise CaligoError("Unknown add-on; available: " + ", ".join(REGISTRY))
    names = list(dict.fromkeys(names))
    if check_tools:
        for name in names:
            executable(name, spec.get("addons", {}).get(name, {}))
    return names


def prepare(names, spec, session_dir, variables):
    for name in names:
        options = spec.get("addons", {}).get(name, {})
        path = executable(name, options)
        wrapper = Path(session_dir) / "bin" / name
        args = [path]
        prefix = ""
        if name == "code-server":
            prefix = "export CALIGO_NESTED_SESSION=1\n"
            if "port" in options:
                args += ["--bind-addr", "127.0.0.1:" + str(options["port"])]
        if name == "codex" and options.get("ca_certificate"):
            certificate = Path(options["ca_certificate"]).expanduser().resolve()
            if not certificate.is_file():
                raise CaligoError(
                    "Codex CA certificate does not exist: " + str(certificate)
                )
            variables["CODEX_CA_CERTIFICATE"] = str(certificate)
        wrapper.write_text(
            "#!/bin/sh\n" + prefix + "exec " + shlex.join(args) + ' "$@"\n'
        )
        wrapper.chmod(0o755)
