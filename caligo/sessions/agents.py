"""Native agent configuration, prompts, authentication checks and launch arguments."""

import json
import tomllib
from ..util import read_private, write_private


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


def configure(experiment, agent):
    if agent == "codex":
        content = read_private(experiment.state / "home", ".codex/config.toml") or b""
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
        write_private(experiment.state / "home", ".claude.json", json.dumps(settings))
        preferences = json.loads(
            read_private(experiment.state / "home", ".claude/settings.json") or b"{}"
        )
        preferences["skipDangerousModePermissionPrompt"] = True
        write_private(
            experiment.state / "home",
            ".claude/settings.json",
            json.dumps(preferences),
        )


def auth_command(agent):
    return (
        [agent, "-c", 'cli_auth_credentials_store="file"', "login", "status"]
        if agent == "codex"
        else [agent, "auth", "status"]
    )


def launch_command(agent, resume):
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
    return command
