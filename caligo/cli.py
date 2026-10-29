"""The human CLI and the identical, scoped CLI available inside a sandbox."""

import argparse
import os
import sys
import time
from pathlib import Path

from . import __version__, broker, jobs, sessions
from .util import Error, read_json
from .workspace import Project


def parser():
    root = argparse.ArgumentParser(
        prog="caligo",
        description="Isolated research experiments and Slurm jobs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Human mode:  caligo attach trial-1 --shell\n"
            'Set/revise:  caligo goal trial-1 "Compare three learning rates"\n'
            "Show goal:   caligo goal trial-1\n"
            "Command help: caligo COMMAND --help"
        ),
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument(
        "--project",
        type=Path,
        help="project root (default: discover from current directory)",
    )
    root.add_argument(
        "-e",
        "--experiment",
        help="select an experiment instead of the current worktree",
    )
    commands = root.add_subparsers(dest="action", required=True, metavar="COMMAND")
    commands.add_parser(
        "init", help="create configuration and initialize Git if needed"
    )
    create = commands.add_parser("create", help="create an experiment worktree")
    create.add_argument("kind", choices=["experiment"])
    create.add_argument("name")
    commands.add_parser("experiments", help="list experiments")
    image = commands.add_parser(
        "image", help="build the shared coding image with editors and agent tools"
    )
    image.add_argument("operation", choices=["build"])
    login = commands.add_parser("login", help="log in once for all project experiments")
    login.add_argument("agent", choices=["codex", "claude"], nargs="?", default="codex")
    login.add_argument(
        "--import",
        dest="import_auth",
        action="store_true",
        help="copy the selected CLI's file-based credentials from this host",
    )
    descriptions = {
        "goal": "show, set, or revise an experiment's goal",
        "start": "start an agent detached",
        "attach": "open a human sandbox shell (--shell) or join an agent",
        "auth": "log in once for all project experiments (alias for login)",
        "stop": "stop an agent",
        "status": "show agent and job status",
        "path": "print its worktree path",
    }
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description)
        if name == "auth":
            command.add_argument(
                "name",
                metavar="EXPERIMENT",
                nargs="?",
                help="accepted for compatibility; login applies to the whole project",
            )
        else:
            command.add_argument("name", metavar="EXPERIMENT", help="experiment name")
        if name == "goal":
            command.description = (
                "Omit TASK to show the current goal. Pass TASK to set or replace it.\n"
                "Revisions are sent to a running agent. Setting a goal does not start one."
            )
            command.formatter_class = argparse.RawDescriptionHelpFormatter
            command.epilog = (
                "Show:    caligo goal trial-1\n"
                'Set:     caligo goal trial-1 "Compare three learning rates"\n'
                'Revise:  caligo goal trial-1 "Try only two rates, then report"\n'
                "Start:   caligo start trial-1 --agent codex"
            )
            command.add_argument(
                "text",
                nargs="?",
                metavar="TASK",
                help="new or revised goal; quote multiple words (omit to show the current goal)",
            )
        if name in ("start", "attach", "auth"):
            command.add_argument(
                "--agent",
                choices=["codex", "claude"],
                help=(
                    "start or resume this agent, then attach to its conversation"
                    if name == "attach"
                    else "agent to " + ("start" if name == "start" else "authenticate")
                ),
            )
        if name == "attach":
            command.description = (
                "Join the running agent, or open a sandbox shell if no agent is running. "
                "Use --shell explicitly for human mode: edit experiment files and run "
                "caligo dev/run yourself, even while an agent is running."
            )
            command.epilog = (
                "Human mode: caligo attach trial-1 --shell. "
                "Type exit to leave the shell. In an agent conversation, Ctrl-b d detaches."
            )
            command.add_argument(
                "--shell",
                action="store_true",
                help="human mode: open an interactive sandbox shell for manual work",
            )
        if name == "auth":
            command.add_argument(
                "--import",
                dest="import_auth",
                action="store_true",
                help="copy only the selected CLI's file-based credentials",
            )
    for phase in ("dev", "run"):
        command = commands.add_parser(
            phase, help="snapshot, submit to Slurm, and stream output"
        )
        command.add_argument(
            "--no-wait", action="store_true", help="return after submission"
        )
        if phase == "dev":
            command.add_argument("command", nargs=argparse.REMAINDER)
    commands.add_parser("jobs", help="list the current experiment's jobs")
    logs = commands.add_parser("logs", help="stream a job's output")
    logs.add_argument("job_id")
    logs.add_argument("--no-follow", action="store_true")
    cancel = commands.add_parser("cancel", help="cancel a queued or running job")
    cancel.add_argument("job_id")
    commands.add_parser("_serve")
    watch = commands.add_parser("_watch")
    watch.add_argument("job_id")
    return root


def follow(fetch, *, wait=True):
    offset = 0
    last_status = None
    last_output = time.monotonic()
    while True:
        result = fetch(offset)
        now = time.monotonic()
        if result["text"]:
            sys.stdout.write(result["text"])
            sys.stdout.flush()
            last_output = now
        if wait and result["state"] not in jobs.TERMINAL:
            if result["state"] != last_status or now - last_output >= 30:
                print(
                    f"caligo: {result['state']}; waiting for job completion "
                    "(Ctrl-C disconnects without cancelling).",
                    file=sys.stderr,
                    flush=True,
                )
                last_output = now
            last_status = result["state"]
        previous, offset = offset, result["offset"]
        if not wait:
            if offset == previous:
                return 0
        elif result["state"] in jobs.TERMINAL and offset == previous:
            return result["returncode"]
        time.sleep(0.2)


def print_jobs(items):
    for job in items:
        print(f"{job['id']}\t{job['phase']}\t{job['state']}")


def inside(args):
    if args.project or args.experiment:
        raise Error("A sandbox can access only its own experiment.")
    if args.action in ("dev", "run"):
        command = getattr(args, "command", None)
        if command and command[0] == "--":
            command = command[1:]
        job_id = broker.request(
            "submit", phase=args.action, command=command, timeout=1000
        )
        print(job_id, flush=True)
        if args.no_wait:
            return 0
        return follow(
            lambda offset: broker.request("logs", job_id=job_id, offset=offset)
        )
    if args.action == "jobs":
        print_jobs(broker.request("jobs"))
        return 0
    if args.action == "logs":
        return follow(
            lambda offset: broker.request("logs", job_id=args.job_id, offset=offset),
            wait=not args.no_follow,
        )
    if args.action == "cancel":
        print(broker.request("cancel", job_id=args.job_id))
        return 0
    raise Error("Inside a sandbox, use caligo dev, run, jobs, logs, or cancel.")


def execute(args, argv):
    if os.environ.get("CALIGO_SANDBOX") == "1":
        return inside(args)
    if args.action == "image":
        from .images import build_coding_image

        print(build_coding_image())
        return 0
    if args.action == "init":
        project = Project(args.project or Path.cwd())
        print(project.init())
        print(
            "Edit caligo.toml, then commit the admin baseline before creating experiments."
        )
        return 0
    project = Project(args.project) if args.project else Project.discover()
    if not os.environ.get("CALIGO_REMOTE") and not args.action.startswith("_"):
        from .remote import forward

        result = forward(project, args, argv)
        if result is not None:
            return result
    if args.action == "create":
        experiment = project.create(args.name)
        print(experiment.worktree)
        return 0
    if args.action == "experiments":
        for path in sorted((project.state / "experiments").glob("*/experiment.json")):
            metadata = read_json(path)
            print(
                f"{metadata['name']}\t{metadata['baseline'][:12]}\t{metadata['worktree']}"
            )
        return 0
    if args.action in ("login", "auth"):
        result = sessions.authenticate(
            project, args.agent or "codex", import_auth=args.import_auth
        )
        if result == 0:
            print(f"Project login saved for {args.agent or 'codex'}; available to all experiments.")
        return result
    experiment = (
        project.experiment(args.name if hasattr(args, "name") else args.experiment)
        if hasattr(args, "name") or args.experiment
        else project.current()
    )
    if args.action == "goal":
        if args.text is None:
            print(experiment.goal())
        else:
            experiment.set_goal(args.text)
            delivered = sessions.steer(experiment, args.text)
            print(
                "Goal updated"
                + (
                    " and sent to the agent."
                    if delivered
                    else "; agent is not running."
                )
            )
    elif args.action == "start":
        state = sessions.start(experiment, args.agent)
        print(
            f"{experiment.name}: {state['agent']} running; attach with caligo attach {experiment.name}"
        )
    elif args.action == "attach":
        return sessions.attach(experiment, args.agent, shell=args.shell)
    elif args.action == "stop":
        sessions.stop(experiment)
        print("Agent stopped. Submitted Slurm jobs continue independently.")
    elif args.action == "status":
        state = read_json(experiment.state / "session.json", {})
        print(
            f"{experiment.name}: {state.get('agent', 'agent')} "
            + ("running" if sessions.running(experiment) else "stopped")
        )
        print("Goal: " + (experiment.goal() or "(not set)"))
        print_jobs(jobs.list_jobs(experiment))
    elif args.action == "path":
        print(experiment.worktree)
    elif args.action in ("dev", "run"):
        command = getattr(args, "command", None)
        if command and command[0] == "--":
            command = command[1:]
        job_id = jobs.submit(experiment, args.action, command)
        print(job_id, flush=True)
        if not args.no_wait:
            return follow(lambda offset: jobs.read_log(experiment, job_id, offset))
    elif args.action == "jobs":
        print_jobs(jobs.list_jobs(experiment))
    elif args.action == "logs":
        jobs.list_jobs(experiment)  # Recover a monitor after a controller restart.
        return follow(
            lambda offset: jobs.read_log(experiment, args.job_id, offset),
            wait=not args.no_follow,
        )
    elif args.action == "cancel":
        print(jobs.cancel(experiment, args.job_id))
    elif args.action == "_serve":
        broker.serve(experiment)
    elif args.action == "_watch":
        jobs.watch(experiment, args.job_id)
    return 0


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        return_code = execute(parser().parse_args(arguments), arguments)
    except (Error, OSError) as exc:
        print("caligo: " + str(exc), file=sys.stderr)
        return_code = 1
    except KeyboardInterrupt:
        print(
            "\nDisconnected. Submitted jobs and detached agents keep running.",
            file=sys.stderr,
        )
        return_code = 130
    sys.exit(return_code or 0)
