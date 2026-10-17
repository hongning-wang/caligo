"""Optional development tools. Each add-on owns its setup, not the sandbox."""
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import tomllib

from .common import CaligoError, run, write_json


def executable(name, options):
    requested = options.get("executable", name)
    found = shutil.which(str(Path(requested).expanduser()))
    if not found:
        raise CaligoError("Add-on %s requires %s on the coding host's PATH; install it or configure addons.%s.executable" % (name, requested, name))
    return found


def codex(context, options):
    home = context["home"] / ".codex"
    home.mkdir(exist_ok=True)
    config = home / "config.toml"
    content = config.read_text() if config.exists() else ""
    settings = tomllib.loads(content)
    # SRT surrounds the agent and every command it starts. Avoid a second OS
    # sandbox (Seatbelt cannot be nested), but retain approvals. Preserve any
    # explicit user choice; add only missing defaults to the private config.
    defaults = {"sandbox_mode": "danger-full-access", "approval_policy": "on-request"}
    prefix = "".join('%s = %s\n' % (key, json.dumps(value)) for key, value in defaults.items() if key not in settings)
    if prefix:
        config.write_text(prefix + content)
    env = context["env"]
    env["CODEX_HOME"] = str(home)
    if "daemon_auto_start" not in settings.get("features", {}):
        # Keep the interactive agent in the sandbox's process tree. Use the
        # tool's own config editor so existing TOML tables/comments survive.
        run([context["tools"]["codex"], "features", "disable", "daemon_auto_start"],
            env={**env, "HOME": str(context["home"])})
    certificate = options.get("ca_certificate") or env.get("CODEX_CA_CERTIFICATE") or env.get("SSL_CERT_FILE")
    if not certificate and sys.platform == "darwin":
        certificate = ssl.get_default_verify_paths().cafile
    if certificate:
        certificate = str(Path(certificate).expanduser().resolve())
        if not Path(certificate).is_file():
            raise CaligoError("Codex CA certificate does not exist: " + certificate)
        env["CODEX_CA_CERTIFICATE"] = certificate
        context["readable"].append(Path(certificate))


def code_server(context, options):
    port = options.get("port", 0)
    home, temp = context["home"], context["temp"]
    data = home / ".local/share/code-server"
    settings_path = data / "User/settings.json"
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    profile = {"Caligo": {"path": context["shell"][0], "args": context["shell"][1:]}}
    for platform in ("osx", "linux"):
        settings["terminal.integrated.profiles." + platform] = profile
        settings["terminal.integrated.defaultProfile." + platform] = "Caligo"
        settings.setdefault("terminal.integrated.env." + platform, {})["CALIGO_NESTED_SESSION"] = "1"
    settings.update({"terminal.integrated.cwd": str(context["workspace"] / context["spec"]["executable"]),
                     "git.openRepositoryInParentFolders": "never", "workbench.startupEditor": "none",
                     "chat.disableAIFeatures": True})
    write_json(settings_path, settings)
    # code-server supports CODE_SERVER_CONFIG natively. Session-local config
    # keeps socket paths independent while editor state persists in HOME.
    config = context["session_dir"] / "code-server.yaml"
    write_json(config, {"bind-addr": "127.0.0.1:" + str(port), "auth": "none",
                        "session-socket": str(temp / "editor.sock"),
                        "user-data-dir": str(data), "extensions-dir": str(data / "extensions"),
                        "disable-telemetry": True, "disable-update-check": True,
                        "disable-proxy": True, "disable-workspace-trust": True})
    context["env"]["CODE_SERVER_CONFIG"] = str(config)
    context["network"]["allowLocalBinding"] = True
    if sys.platform == "darwin":
        context["network"]["allowMachLookup"] = ["com.apple.FSEvents"]


REGISTRY = {"codex": codex, "code-server": code_server}
OPTIONS = {"codex": {"executable", "ca_certificate"}, "code-server": {"executable", "port"}}


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) - REGISTRY.keys():
        raise CaligoError("Unknown add-on configuration; available: " + ", ".join(REGISTRY))
    for name, values in settings.items():
        if not isinstance(values, dict) or set(values) - OPTIONS[name]:
            raise CaligoError("Unknown settings for add-on " + name)
        for key, value in values.items():
            if key == "port":
                if type(value) is not int or not (value == 0 or 1024 <= value <= 65535):
                    raise CaligoError("Editor port must be 0 (automatic) or between 1024 and 65535")
            elif not isinstance(value, str) or not value.strip() or "\0" in value or "\n" in value:
                raise CaligoError("Invalid add-on setting: %s.%s" % (name, key))


def validate_selection(names, spec, check_tools=False):
    if not isinstance(names, list) or any(not isinstance(n, str) or n not in REGISTRY for n in names):
        raise CaligoError("Unknown add-on; available: " + ", ".join(REGISTRY))
    names = list(dict.fromkeys(names))
    if check_tools:
        for name in names:
            executable(name, spec.get("addons", {}).get(name, {}))
    return names


def prepare(names, context):
    context["tools"] = {}
    for name in validate_selection(names, context["spec"]):
        options = context["spec"].get("addons", {}).get(name, {})
        path = executable(name, options)
        context["tools"][name] = path
        context["readable"].extend([Path(path).parent, Path(path).resolve().parent.parent])
        # Keep the usual command name even for a custom executable location.
        link = context["session_dir"] / "bin" / name
        link.symlink_to(path)
        context["env"]["PATH"] = str(link.parent) + os.pathsep + context["env"]["PATH"]
        REGISTRY[name](context, options)
