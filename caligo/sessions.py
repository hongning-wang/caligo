"""Native agent terminals, kept alive by a private tmux server."""

import hashlib
import json
import shlex
import shutil
import subprocess
import time
import tomllib
import uuid
from pathlib import Path

from . import broker, sandbox
from .util import (
    Error,
    locked,
    read_json,
    read_private,
    run,
    timestamp,
    write_json,
    write_private,
)


def tmux(experiment):
    project_id = read_json(experiment.project.state / "project.json")["id"]
    name = (
        "caligo-"
        + hashlib.sha256((project_id + experiment.name).encode()).hexdigest()[:20]
    )
    return ["tmux", "-S", str(experiment.project.state / "tmux.sock")], name


def running(experiment):
    args, name = tmux(experiment)
    return run([*args, "has-session", "-t", name], check=False).returncode == 0


def prompt(experiment):
    return (
        f"You are working on Caligo experiment {experiment.name}.\n"
        f"Goal: {experiment.goal()}\n"
        f"Editable repository directories: {', '.join(experiment.config['writable']) or 'none'}.\n"
        "Core code, evaluation and policy are fixed. Save artifacts to /outputs.\n"
        "Use caligo dev COMMAND [ARGS...] for Slurm tests and caligo run for final evaluation. "
        "Use caligo jobs, caligo logs JOB_ID, and caligo cancel JOB_ID to inspect your own jobs. "
        "Run commands in /workspace. Follow the goal's constraints and stopping conditions. "
        "Report results with job IDs. Wait for human input when finished or blocked."
    )


def authenticate(experiment, agent, *, import_auth=False):
    broker.ensure(experiment)
    backend = sandbox.prepare(experiment)
    sandbox.require_agent(backend, agent)
    if import_auth:
        relative = (
            ".codex/auth.json" if agent == "codex" else ".claude/.credentials.json"
        )
        source = Path.home() / relative
        if not source.is_file():
            raise Error(
                f"No file-based {agent} credentials found; run caligo auth without --import."
            )
        write_private(experiment.state / "home", relative, source.read_bytes())
        return 0
    command = (
        ["codex", "login", "--device-auth"]
        if agent == "codex"
        else ["claude", "auth", "login"]
    )
    return subprocess.call(backend.command(command, interactive=True))


def start(experiment, agent=None):
    if not shutil.which("tmux"):
        raise Error("Install tmux to start persistent agent sessions.")
    with locked(experiment.state / "session.lock"):
        saved = read_json(experiment.state / "session.json", {})
        if running(experiment):
            if agent and agent != saved.get("agent"):
                raise Error("A different agent is already running; stop it first.")
            return saved
        if not experiment.goal():
            raise Error(
                "Set a goal with caligo goal EXPERIMENT 'task' before starting an agent."
            )
        agent = agent or saved.get("agent", "codex")
        broker.ensure(experiment)
        backend = sandbox.prepare(experiment)
        sandbox.require_agent(backend, agent)
        if agent == "codex":
            content = (
                read_private(experiment.state / "home", ".codex/config.toml") or b""
            )
            settings = tomllib.loads(content.decode())
            if "/workspace" not in settings.get("projects", {}):
                write_private(
                    experiment.state / "home",
                    ".codex/config.toml",
                    content + b'\n[projects."/workspace"]\ntrust_level = "trusted"\n',
                )
        else:
            content = read_private(experiment.state / "home", ".claude.json") or b"{}"
            settings = json.loads(content)
            settings.setdefault("hasCompletedOnboarding", True)
            settings.setdefault("theme", "dark")
            settings.setdefault("projects", {}).setdefault("/workspace", {}).setdefault(
                "hasTrustDialogAccepted", True
            )
            write_private(
                experiment.state / "home", ".claude.json", json.dumps(settings)
            )
            preferences = json.loads(
                read_private(experiment.state / "home", ".claude/settings.json")
                or b"{}"
            )
            preferences["skipDangerousModePermissionPrompt"] = True
            write_private(
                experiment.state / "home",
                ".claude/settings.json",
                json.dumps(preferences),
            )
        auth = (
            [agent, "login", "status"]
            if agent == "codex"
            else [agent, "auth", "status"]
        )
        if run(backend.command(auth), check=False).returncode:
            raise Error(
                f"Authenticate first: caligo auth {experiment.name} --agent {agent}"
            )
        task = prompt(experiment)
        history = (
            experiment.state
            / "home"
            / (".codex" if agent == "codex" else ".claude/projects")
        )
        resume = saved.get("agent") == agent and any(history.rglob("*.jsonl"))
        if agent == "codex":
            command = [
                agent,
                "--dangerously-bypass-approvals-and-sandbox",
                "--no-alt-screen",
                "-c",
                'projects."/workspace".trust_level="trusted"',
            ]
            if resume:
                command += ["resume", "--last"]
        else:
            command = [agent, "--permission-mode", "bypassPermissions"]
            if resume:
                command += ["--continue"]
        command += [task]
        args, name = tmux(experiment)
        launch = "exec " + shlex.join(backend.command(command, interactive=True))
        run([*args, "new-session", "-d", "-s", name, "-x", "120", "-y", "40", launch])
        run(
            [
                *args,
                "pipe-pane",
                "-o",
                "-t",
                name,
                "cat >> " + shlex.quote(str(experiment.state / "session.log")),
            ]
        )
        state = {"agent": agent, "started": timestamp(), "goal": experiment.goal()}
        write_json(experiment.state / "session.json", state)
        time.sleep(0.2)
        if not running(experiment):
            raise Error(
                "Agent exited during startup; inspect "
                + str(experiment.state / "session.log")
            )
        return state


def steer(experiment, text):
    if not running(experiment):
        return False
    args, name = tmux(experiment)
    token = uuid.uuid4().hex
    run(
        [*args, "load-buffer", "-b", token, "-"],
        input="Updated goal from the human:\n" + text,
    )
    run([*args, "paste-buffer", "-p", "-d", "-b", token, "-t", name])
    run([*args, "send-keys", "-t", name, "Enter"])
    return True


def attach(experiment, agent=None, *, shell=False):
    if agent:
        start(experiment, agent)
    if not shell and running(experiment):
        args, name = tmux(experiment)
        return subprocess.call([*args, "attach-session", "-t", name])
    broker.ensure(experiment)
    backend = sandbox.prepare(experiment)
    return subprocess.call(
        backend.command(
            ["bash", "--rcfile", "/home/worker/.caligo-bashrc", "-i"], interactive=True
        )
    )


def stop(experiment):
    args, name = tmux(experiment)
    if running(experiment):
        run([*args, "kill-session", "-t", name])
    sandbox.runtime(experiment).stop()
    write_json(experiment.state / "controller-stop.json", {"time": timestamp()})
