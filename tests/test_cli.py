"""CLI navigation and compatibility for the agent command namespace."""

import subprocess
import sys

from caligo.cli import parser


def invoke(directory, *args):
    return subprocess.run(
        [sys.executable, "-m", "caligo", *args],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_agent_help_is_separate_and_does_not_require_project(tmp_path):
    top = invoke(tmp_path, "--help")
    assert top.returncode == 0
    entries = {
        line.split()[0] for line in top.stdout.splitlines() if line.startswith("    ")
    }
    assert {"agent", "create", "fork", "run"} <= entries
    assert (
        not {
            "steer",
            "interrupt",
            "threads",
            "service",
            "pause",
            "resume",
            "stop",
            "attach",
            "start",
        }
        & entries
    )
    for args in (("agent",), ("agent", "--help"), ("help", "agent")):
        result = invoke(tmp_path, *args)
        assert result.returncode == 0, result.stderr
        assert "steer" in result.stdout and "attach" in result.stdout
    detail = invoke(tmp_path, "help", "agent", "start")
    assert detail.returncode == 0
    assert "--goal" in detail.stdout and "--backend" in detail.stdout


def test_agent_namespace_and_legacy_forms_accept_same_options():
    cli = parser()
    for arguments in (
        [
            "start",
            "search",
            "--goal",
            "Try regularization",
            "--backend",
            "claude",
            "--once",
        ],
        ["attach", "search", "--after", "12", "--json", "--no-follow"],
        ["steer", "search", "instruction", "--delivery", "steer", "--id", "message-1"],
        ["interrupt", "search", "new direction"],
        ["pause", "search"],
        ["resume", "search"],
        ["stop", "search"],
        ["threads", "search"],
        ["service", "status", "search"],
    ):
        canonical = cli.parse_args(["agent", *arguments])
        legacy = cli.parse_args(arguments)
        assert canonical.command == "agent"
        assert canonical.agent_action == arguments[0]
        assert vars(canonical) == vars(legacy)


def test_agent_project_option_at_each_level():
    cli = parser()
    for arguments in (
        ["--project", "study.toml", "agent", "stop", "search"],
        ["agent", "--project", "study.toml", "stop", "search"],
        ["agent", "stop", "search", "--project", "study.toml"],
    ):
        args = cli.parse_args(arguments)
        assert args.project == "study.toml"
        assert args.agent_action == "stop"
        assert args.name == "search"
