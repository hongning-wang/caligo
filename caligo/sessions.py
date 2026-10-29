"""Native agent terminals, kept alive by a private tmux server."""

import hashlib
import json
import os
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
    private_parent,
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


def prompt(experiment, goal=None):
    return (
        f"You are working on Caligo experiment {experiment.name}.\n"
        f"Goal: {experiment.goal() if goal is None else goal}\n"
        f"Editable repository directories: {', '.join(experiment.config['writable']) or 'none'}.\n"
        "Core code, evaluation and policy are fixed. Save artifacts to /outputs.\n"
        "Use caligo dev COMMAND [ARGS...] for Slurm tests and caligo run for final evaluation. "
        "Use caligo jobs, caligo logs JOB_ID, and caligo cancel JOB_ID to inspect your own jobs. "
        "Run commands in /workspace. Follow the goal's constraints and stopping conditions. "
        "Report results with job IDs and verify the requested outputs before declaring success.\n"
        "Slurm work is asynchronous: submitting (including image preparation), queued, and running "
        "are normal waiting states, even for long periods without logs. Keep the goal active "
        "while waiting; silence or elapsed time alone is not a blocker or a reason to cancel. "
        "Let caligo dev/run finish, or use --no-wait and then caligo logs JOB_ID, which waits "
        "until the job finishes. If your command tool yields a running session, wait on that "
        "session instead of ending the task. If the tool times out or disconnects, use caligo "
        "jobs and reconnect with caligo logs JOB_ID; the submitted job continues independently. "
        "For periodic checks, sleep 30 seconds between checks instead of busy-polling. "
        "Continue the experiment when results arrive. Cancel only when the human requests it "
        "or the goal's constraints or evidence require it, and confirm the terminal job state. "
        "Ask for human input only for a concrete problem you cannot resolve within your permissions."
    )


def wait_for_input(experiment, agent):
    """Wait for the native composer before sending a slash command."""
    args, name = tmux(experiment)
    marker = "›" if agent == "codex" else "❯"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and running(experiment):
        pane = run([*args, "capture-pane", "-p", "-t", name]).stdout
        for line in pane.splitlines():
            line = line.lstrip()
            # Startup menus also use an arrow, followed by a numbered choice.
            if line.startswith(marker) and not line[1:].lstrip()[:1].isdigit():
                return
        time.sleep(0.1)
    raise Error(
        "Agent did not reach its input prompt; inspect "
        + str(experiment.state / "session.log")
        + " and attach to resolve any startup dialog. The goal has not been sent."
    )


def credential_path(agent):
    return ".codex/auth.json" if agent == "codex" else ".claude/.credentials.json"


def authenticate(project, agent, *, import_auth=False):
    home = project.state / "auth/home"
    home.mkdir(parents=True, exist_ok=True)
    relative = credential_path(agent)
    with locked(project.state / "auth/login.lock"):
        if import_auth:
            source = Path.home() / relative
            if not source.is_file():
                raise Error(
                    f"No file-based {agent} credentials found; run caligo login {agent} without --import."
                )
            write_private(home, relative, source.read_bytes())
            return 0
        backend = sandbox.prepare_auth(project)
        try:
            sandbox.require_agent(backend, agent)
            command = (
                [
                    "codex", "-c", 'cli_auth_credentials_store="file"',
                    "login", "--device-auth",
                ]
                if agent == "codex"
                else ["claude", "auth", "login"]
            )
            return subprocess.call(backend.command(command, interactive=True))
        finally:
            backend.stop()


def share_project_credentials(experiment, agent):
    """Use the project login without sharing conversations or CLI settings."""
    home = experiment.project.state / "auth/home"
    if not home.exists():
        return
    relative = credential_path(agent)
    with locked(experiment.project.state / "auth/login.lock"):
        content = read_private(home, relative)
        if content is None:
            return
        if agent == "claude":
            # Claude replaces its credentials atomically, so keep a private copy.
            write_private(experiment.state / "home", relative, content)
            return
        with private_parent(experiment.state / "home", relative) as (fd, name):
            temporary = ".caligo-" + uuid.uuid4().hex
            # Codex refreshes credentials in place. A hard link shares
            # those writes without exposing the project home or other sessions.
            os.link(home / relative, temporary, dst_dir_fd=fd, follow_symlinks=False)
            try:
                os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass


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
        share_project_credentials(experiment, agent)
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
            [agent, "-c", 'cli_auth_credentials_store="file"', "login", "status"]
            if agent == "codex"
            else [agent, "auth", "status"]
        )
        if run(backend.command(auth), check=False).returncode:
            raise Error(
                f"Authenticate first: caligo login {agent}"
            )
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
                "-c",
                'cli_auth_credentials_store="file"',
            ]
            if resume:
                command += ["resume", "--last"]
        else:
            command = [agent, "--permission-mode", "bypassPermissions"]
            if resume:
                command += ["--continue"]
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
        wait_for_input(experiment, agent)
        steer(experiment, experiment.goal())
        return state


def steer(experiment, text):
    if not running(experiment):
        return False
    args, name = tmux(experiment)
    token = uuid.uuid4().hex
    run(
        [*args, "load-buffer", "-b", token, "-"],
        input=prompt(experiment, text),
    )
    # Keep the command outside the pasted body so large/multiline goals are
    # recognized as slash commands by both native composers.
    run([*args, "send-keys", "-t", name, "-l", "/goal "])
    run([*args, "paste-buffer", "-p", "-d", "-b", token, "-t", name])
    time.sleep(0.2)
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
