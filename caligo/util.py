"""Small process and state helpers shared by the CLI and controller."""

import contextlib
import fcntl
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path


class Error(Exception):
    """An actionable error to display without a traceback."""


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


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def locked(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def identifier(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise Error("Names must use 1-64 letters, digits, underscores, or hyphens.")
    return value


def timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


@contextlib.contextmanager
def private_parent(root, relative):
    """Anchor controller access to a sandbox home without following its symlinks."""
    parts = Path(relative).parts
    if not parts or any(part in ("/", "..") for part in parts):
        raise Error("Expected a relative private-home path.")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def write_private(root, relative, content, mode=0o600):
    with private_parent(root, relative) as (fd, name):
        temporary = ".caligo-" + uuid.uuid4().hex
        output = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=fd,
        )
        try:
            with os.fdopen(output, "wb") as stream:
                stream.write(content.encode() if isinstance(content, str) else content)
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def read_private(root, relative):
    with private_parent(root, relative) as (fd, name):
        try:
            source = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
            )
        except FileNotFoundError:
            return None
        with os.fdopen(source, "rb") as stream:
            import stat

            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise Error("Private configuration must be a regular file.")
            return stream.read(1024 * 1024)
