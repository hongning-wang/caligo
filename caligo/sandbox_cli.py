#!/usr/bin/env python3
"""Standalone sandbox client: no host credentials, filesystem, or shell RPC."""
import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(prog="caligo", description="Experiments for this sandbox's worktree")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("list", help="List attempts in the current project")
    commands.add_parser("switch", help="Enter another attempt").add_argument("name")
    create = commands.add_parser("create", help="Create and enter an attempt")
    create.add_argument("name")
    create.add_argument("--mode", required=True, choices=["supervisor", "user"])
    create.add_argument("--addon", action="append", default=[], help="Enable a development tool (repeatable)")
    commands.add_parser("exec", help="Execute a command on dev").add_argument("argv", nargs=argparse.REMAINDER)
    commands.add_parser("run", help="Submit a frozen copy of this worktree").add_argument("script", nargs="?")
    for name in ("status", "logs", "cancel", "publish", "artifacts"):
        command = commands.add_parser(name)
        command.add_argument("run")
        if name == "artifacts":
            command.add_argument("destination", nargs="?", default=".")
    args = parser.parse_args()
    if args.action in {"create", "switch"} and os.environ.get("CALIGO_NESTED_SESSION"):
        parser.error("Stop the editor and change attempts from the parent Caligo shell")
    if args.action == "exec" and args.argv[:1] == ["--"]:
        args.argv = args.argv[1:]
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(os.environ["CALIGO_SOCKET"])
            connection.sendall(json.dumps(vars(args)).encode() + b"\n")
            with connection.makefile("rb") as response:
                while True:
                    header = json.loads(response.readline())
                    if "stream" not in header:
                        break
                    stream = sys.stdout.buffer if header["stream"] == "stdout" else sys.stderr.buffer
                    stream.write(base64.b64decode(header["data"]))
                    stream.flush()
                if not header["ok"]:
                    raise ValueError(header["error"])
                if args.action == "artifacts":
                    target = Path(args.destination).resolve()
                    if (target / args.run).exists():
                        raise ValueError("Destination already exists: " + str(target / args.run))
                    target.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory() as tmp:
                        archive = Path(tmp) / "run.tar.gz"
                        with archive.open("wb") as output:
                            shutil.copyfileobj(response, output)
                        with tarfile.open(archive) as tar:
                            for member in tar.getmembers():
                                name = Path(member.name)
                                if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                                    raise ValueError("Unsafe archive member")
                            if hasattr(tarfile, "data_filter"):
                                tar.extractall(tmp, filter="data")
                            else:
                                tar.extractall(tmp)
                        shutil.move(str(Path(tmp) / args.run), target / args.run)
                    result = {"path": str(target / args.run)}
                else:
                    result = header["result"]
        if args.action in {"switch", "create"}:
            print("Entering attempt: " + result["switch"], file=sys.stderr)
            sys.exit(75)
        elif args.action == "exec":
            sys.exit(result["exit_code"])
        elif args.action == "list":
            print("Project: " + result["project"])
            for attempt in result["attempts"]:
                print("%s %s  (%s)" % ("*" if attempt["current"] else " ", attempt["name"], attempt["mode"]))
        elif args.action == "logs":
            print(result["logs"], end="")
        else:
            print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError) as error:
        print("caligo: " + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
