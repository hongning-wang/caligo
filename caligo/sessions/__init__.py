"""Persistent native agent session workflows."""

import shutil
from .. import broker, sandbox
from ..util import locked, read_json, write_json, timestamp
from ..errors import Error
from ..process import run, interactive
from . import agents, terminal
from .agents import prompt
from .auth import authenticate, credential_path, share_project_credentials
from .terminal import tmux, running, wait_for_input, steer


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
        agents.configure(experiment, agent)
        auth = agents.auth_command(agent)
        if run(backend.command(auth), check=False).returncode:
            raise Error(f"Authenticate first: caligo login {agent}")
        history = (
            experiment.state
            / "home"
            / (".codex" if agent == "codex" else ".claude/projects")
        )
        resume = saved.get("agent") == agent and any(history.rglob("*.jsonl"))
        command = agents.launch_command(agent, resume)
        terminal.launch(experiment, backend.command(command, interactive=True))
        state = {"agent": agent, "started": timestamp(), "goal": experiment.goal()}
        write_json(experiment.state / "session.json", state)
        wait_for_input(experiment, agent)
        steer(experiment, experiment.goal())
        return state


def attach(experiment, agent=None, *, shell=False):
    if agent:
        start(experiment, agent)
    if not shell and running(experiment):
        return terminal.attach(experiment)
    broker.ensure(experiment)
    backend = sandbox.prepare(experiment)
    return interactive(
        backend.command(
            ["bash", "--rcfile", "/home/worker/.caligo-bashrc", "-i"], interactive=True
        )
    )


def stop(experiment):
    terminal.stop(experiment)
    sandbox.runtime(experiment).stop()
    write_json(experiment.state / "controller-stop.json", {"time": timestamp()})


__all__ = [
    "prompt",
    "authenticate",
    "credential_path",
    "share_project_credentials",
    "tmux",
    "running",
    "wait_for_input",
    "steer",
    "start",
    "attach",
    "stop",
]
