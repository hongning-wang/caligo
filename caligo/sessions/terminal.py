"""Operations on the private tmux server."""

import hashlib
import shlex
import time
import uuid
from ..util import read_json
from ..errors import Error
from ..process import run, interactive
from .agents import prompt


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


def launch(experiment, command):
    args, name = tmux(experiment)
    run(
        [
            *args,
            "new-session",
            "-d",
            "-s",
            name,
            "-x",
            "120",
            "-y",
            "40",
            "exec " + shlex.join(command),
        ]
    )
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


def attach(experiment):
    args, name = tmux(experiment)
    return interactive([*args, "attach-session", "-t", name])


def stop(experiment):
    args, name = tmux(experiment)
    if running(experiment):
        run([*args, "kill-session", "-t", name])
