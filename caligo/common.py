import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone


class CaligoError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(prefix):
    return prefix + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:10]


def valid_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,100}", value):
        raise CaligoError("Invalid identifier: %r" % value)
    return value


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(f.name, path)


@contextlib.contextmanager
def lock(path, shared=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # NFS requires a readable descriptor for a shared flock.
    with path.open("a+") as f:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield


def run(argv, **kwargs):
    result = subprocess.run([str(x) for x in argv], capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise CaligoError("%s failed (%s): %s" % (argv[0], result.returncode, result.stderr.strip() or result.stdout.strip()))
    return result.stdout.strip()


def stream_command(argv, emit, **kwargs):
    """Stream both pipes without buffering an entire dev command's output."""
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **kwargs) as process:
        def forward(pipe, channel):
            while True:
                data = os.read(pipe.fileno(), 8192)
                if not data:
                    break
                emit(channel, data)
        readers = [threading.Thread(target=forward, args=(pipe, channel), daemon=True)
                   for pipe, channel in ((process.stdout, "stdout"), (process.stderr, "stderr"))]
        for reader in readers:
            reader.start()
        for reader in readers:
            reader.join()
        return process.wait()


def inventory(root):
    """Hash regular files, modes, and empty directories; reject external links."""
    root = Path(root)
    result = {}
    if root.is_symlink() or not root.is_dir():
        raise CaligoError("Expected a real directory: %s" % root)
    for p in sorted(root.rglob("*")):
        info = p.lstat()
        relative = p.relative_to(root).as_posix()
        if p.name == ".git" or p.is_symlink() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise CaligoError("Symlinks, nested .git, and special files are not supported: %s" % p)
        record = {"mode": stat.S_IMODE(info.st_mode), "type": "dir" if p.is_dir() else "file"}
        if p.is_file():
            digest = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            record.update(sha256=digest.hexdigest(), size=info.st_size)
        result[relative] = record
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def copy_tree(source, target):
    before = inventory(source)
    shutil.copytree(source, target)
    if inventory(source) != before or inventory(target) != before:
        raise CaligoError("Files changed while snapshotting %s; retry after edits finish" % source)
    return before


def is_bucket(value):
    return bool(re.match(r"^(s3|gs|r2)://|^[A-Za-z][A-Za-z0-9_-]*:", value))


def rclone_path(value):
    if value.startswith("s3://"):
        return ":s3,provider=AWS,env_auth=true:" + value[5:]
    if value.startswith("gs://"):
        return ":gcs:" + value[5:]
    if value.startswith("r2://"):
        raise CaligoError("Use a configured rclone remote for R2, e.g. r2:bucket/prefix")
    return value


def transfer(source, target):
    # Copy, never sync: neither input buckets nor existing output keys are deleted.
    run(["rclone", "copy", rclone_path(str(source)), rclone_path(str(target))])
