"""Captured, interactive, and detached host processes."""

import subprocess
import threading
from .errors import Error


def run(args, *, cwd=None, input=None, check=True, timeout=None):
    try:
        result = subprocess.run(
            [str(a) for a in args],
            check=False,
            cwd=cwd,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Error(f"{args[0]}: {exc}") from exc
    if check and result.returncode:
        raise Error(
            result.stderr.strip()
            or result.stdout.strip()
            or f"{args[0]} exited with status {result.returncode}"
        )
    return result


def detached(args, log, env):
    process = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        env=env,
        start_new_session=True,
    )
    # Retain and reap the child without tying its lifetime to the caller.
    threading.Thread(target=process.wait, daemon=True).start()
    return process


def interactive(args):
    """Inherit terminal streams and return the child exit status."""
    return subprocess.call([str(arg) for arg in args])
