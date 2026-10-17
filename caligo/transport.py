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
    def __init__(self, host, python=None):
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

    def select_python(self):
        # Probe before uploading or importing Caligo. This tiny script must be
        # parseable by old system Pythons so they can report their version.
        candidates = [self.python] if self.python is not None else [
            "python3", "python3.14", "python3.13", "python3.12", "python3.11",
        ]
        probe = """import json, sys
if sys.version_info < (3, 11):
    sys.stderr.write('%s is Python %s; Caligo requires Python 3.11+\\n' % (sys.executable, sys.version.split()[0]))
    sys.exit(1)
print(json.dumps({'executable': sys.executable}))
"""
        command = (
            "for candidate in " + shlex.join(candidates) + "; do\n"
            '  if command -v "$candidate" >/dev/null 2>&1; then\n'
            '    "$candidate" -c ' + shlex.quote(probe) + " && exit 0\n"
            "  fi\ndone\nexit 1"
        )
        result = subprocess.run(self.ssh + [self.host, command], capture_output=True, text=True)
        if result.returncode == 255:
            raise CaligoError("SSH connection to %s failed: %s" % (self.host, result.stderr.strip()))
        if result.returncode:
            raise CaligoError(
                "No usable Python 3.11+ on %s (tried %s). Install Python 3.11+ on that host "
                "or set [remote].python to its executable in project.toml. "
                "An explicit remote.python is used without fallback.\n%s"
                % (self.host, ", ".join(candidates), result.stderr.strip())
            )
        try:
            executable = json.loads(result.stdout)["executable"]
            if not isinstance(executable, str) or not executable.startswith("/") or "\n" in executable:
                raise ValueError("Expected an absolute interpreter path")
        except (ValueError, KeyError, TypeError) as error:
            raise CaligoError("Invalid Python probe response from " + self.host) from error
        # Keep the selected interpreter for installation, RPC, shells and jobs.
        self.python = executable

    def install(self):
        self.select_python()
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
