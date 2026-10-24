"""Opt-in SSH coding-host test: python -m tests.integration_coding [SSH_HOST]."""

import shlex
import sys
import tempfile
from pathlib import Path

from caligo import config
from caligo.transport import SSH
from caligo.util import read_json, run
from caligo.workspace import Project


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "orcd"
    root = Path(tempfile.mkdtemp(prefix="caligo-coding-"))
    project = Project(root)
    project.init()
    text = config.STARTER
    text = text.replace('coding = "local"', 'coding = "' + host + '"')
    (root / "caligo.toml").write_text(text)
    (root / "experiments").mkdir()
    (root / "experiments/config").write_text("baseline")
    (root / "evaluation").mkdir()
    (root / "evaluation/run.py").write_text("print('fixed')")
    run(["git", "config", "user.name", "Caligo Test"], cwd=root)
    run(["git", "config", "user.email", "caligo-test@localhost"], cwd=root)
    run(["git", "add", "."], cwd=root)
    run(["git", "commit", "-m", "baseline"], cwd=root)
    base = [sys.executable, "-m", "caligo", "--project", str(root)]
    print("Project:", root, flush=True)
    print(run([*base, "create", "experiment", "remote-trial"]).stdout, flush=True)
    state = read_json(project.state / "remote.json")
    ssh = SSH(host)
    package = state["root"] + "/.caligo-runtime/" + state["code_id"]
    script = """
from pathlib import Path
from caligo.workspace import Project
from caligo import sandbox
p=Project(PROJECT)
e=p.experiment('remote-trial')
binary=e.state/'home/.local/bin/codex'
binary.parent.mkdir(parents=True, exist_ok=True)
binary.write_text("#!/usr/bin/env python3\\nimport os,sys,tty\\nfrom pathlib import Path\\nif 'login' in sys.argv: sys.exit(0)\\nPath('/outputs/task').write_text(sys.argv[-1])\\nprint('ready',flush=True)\\ntty.setraw(sys.stdin.fileno())\\nwhile True:\\n data=os.read(sys.stdin.fileno(),4096)\\n if not data: break\\n with open('/outputs/steer','ab') as f: f.write(data)\\n")
binary.chmod(0o755)
backend=sandbox.prepare(e)
other=p.create('cache-peer')
sandbox.prepare(other)
from caligo.util import read_json
import os
assert os.path.samefile(read_json(e.state/'image.json')['path'],read_json(other.state/'image.json')['path'])
assert not (e.state/'image.sif').exists() and not (other.state/'image.sif').exists()
print('SHARED_IMAGE_OK')
from caligo.util import run
code="from pathlib import Path; assert Path.home() == Path('/home/worker') and not (Path.home()/'.ssh').exists(); Path('/workspace/experiments/new').write_text('ok'); print('REMOTE_ISOLATION_OK')"
print(run(backend.command(['python3','-c',code])).stdout)
""".replace("PROJECT", repr(state["repository"]))
    prefix = [
        "env",
        "PYTHONPATH=" + package,
        "CALIGO_RUNTIME=apptainer",
        state["python"],
    ]
    print(
        ssh.command(shlex.join([*prefix, "-c", script]), timeout=900).stdout, flush=True
    )
    print(
        run(
            [*base, "goal", "remote-trial", "Write an experiment result and wait."]
        ).stdout,
        flush=True,
    )
    print(
        run([*base, "start", "remote-trial", "--agent", "codex"], timeout=900).stdout,
        flush=True,
    )
    print(
        run([*base, "goal", "remote-trial", "Try a second configuration."]).stdout,
        flush=True,
    )
    verify = """
import time
from pathlib import Path
folder=Path(PROJECT)/'.caligo/experiments/remote-trial/outputs'
for _ in range(50):
 if (folder/'steer').exists(): break
 time.sleep(.1)
assert 'Write an experiment result' in (folder/'task').read_text()
assert 'Try a second configuration.' in (folder/'steer').read_text()
print('REMOTE_SESSION_OK')
""".replace("PROJECT", repr(state["repository"]))
    print(ssh.command(shlex.join([*prefix, "-c", verify])).stdout, flush=True)
    print(run([*base, "stop", "remote-trial"]).stdout, flush=True)
    print(
        "PASS: remote worktree, contained coding environment, detached session, and steering."
    )


if __name__ == "__main__":
    main()
