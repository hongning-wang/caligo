import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import zipapp

from .common import CaligoError


class Transport:
    def __init__(self, host, python="python3"):
        if not host or host.startswith("-") or any(c.isspace() for c in host):
            raise CaligoError("Specify an SSH host alias in locations (or doctor --host)")
        self.host = host
        self.python = python
        self.ssh = ["ssh"]
        self.scp = ["scp"]
        if os.environ.get("CALIGO_SSH_CONFIG"):
            self.ssh += ["-F", os.environ["CALIGO_SSH_CONFIG"]]
            self.scp += ["-F", os.environ["CALIGO_SSH_CONFIG"]]
        self.ssh += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        self.scp += ["-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
        self.agent = None

    def checked(self, args, **kwargs):
        p = subprocess.run(args, capture_output=True, text=True, **kwargs)
        if p.returncode:
            raise CaligoError(p.stderr.strip() or p.stdout.strip() or "Remote command failed")
        return p.stdout

    def install(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "app"
            package = source / "caligo"
            package.mkdir(parents=True)
            revision = hashlib.sha256()
            for path in sorted(Path(__file__).parent.glob("*.py")):
                (package / path.name).write_bytes(path.read_bytes())
                revision.update(path.name.encode() + b"\0" + path.read_bytes())
            (source / "__main__.py").write_text("from caligo.__main__ import *\n")
            archive = Path(tmp) / "caligo.pyz"
            zipapp.create_archive(source, archive)
            key = revision.hexdigest()[:20]
            # Every command uses the exact code revision it shipped. Batch jobs
            # retain that revision even after the local CLI is upgraded.
            installer = """import os, pathlib, sys, tempfile
home = pathlib.Path.home()
target = home / '.local/share/caligo/agents' / sys.argv[1]
data = sys.stdin.buffer.read()
target.parent.mkdir(parents=True, exist_ok=True)
if not target.exists():
    fd, name = tempfile.mkstemp(dir=target.parent)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
    os.replace(name, target)
print(home)
"""
            p = subprocess.run(self.ssh + [self.host, shlex.join([self.python, "-c", installer, key + ".pyz"])], input=archive.read_bytes(), capture_output=True)
            if p.returncode:
                raise CaligoError(p.stderr.decode(errors="replace"))
            home = p.stdout.decode().strip()
            if not home.startswith("/") or "\n" in home:
                raise CaligoError("Remote shell did not return a valid home directory")
            self.agent = home + "/.local/share/caligo/agents/" + key + ".pyz"
        return home

    def rpc(self, request):
        p = subprocess.run(self.ssh + [self.host, shlex.join([self.python, self.agent, "_remote-rpc"])], input=json.dumps(request), capture_output=True, text=True)
        try:
            response = json.loads(p.stdout)
        except ValueError as e:
            raise CaligoError("Remote connection/controller failed: " + (p.stderr.strip() or p.stdout.strip())) from e
        if not response.get("ok"):
            raise CaligoError(response.get("error", "Remote operation failed"))
        return response["result"]

    def upload(self, archive):
        temp = self.checked(self.ssh + [self.host, "mktemp -d"]).strip()
        target = temp + "/project.tar.gz"
        self.checked(self.scp + [str(archive), self.host + ":" + target])
        return target

    def remove_upload(self, archive):
        self.checked(self.ssh + [self.host, shlex.join(["rm", "--", archive])])
        self.checked(self.ssh + [self.host, shlex.join(["rmdir", "--", str(Path(archive).parent)])])

    def download(self, root, project, run_id, archive):
        command = [self.python, self.agent, "_remote-archive", root, project, run_id]
        with open(archive, "wb") as out:
            p = subprocess.run(self.ssh + [self.host, shlex.join(command)], stdout=out, stderr=subprocess.PIPE)
        if p.returncode:
            raise CaligoError(p.stderr.decode(errors="replace"))
