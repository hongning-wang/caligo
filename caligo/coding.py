"""Small adapter to the existing Sandbox Runtime; no VM or image per attempt."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .common import CaligoError, write_json


def runtime_module():
    executable = shutil.which("srt")
    candidates = []
    if executable:
        candidates.append(Path(executable).resolve().parent / "index.js")
    candidates.append(Path(sys.prefix) / "caligo-tools/node_modules/@anthropic-ai/sandbox-runtime/dist/index.js")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise CaligoError("Coding shells require Sandbox Runtime and Node >=22.12: "
                        "npm install -g @anthropic-ai/sandbox-runtime@0.0.79")


def launch(controller, attempt, argv, session_dir, variables):
    module = runtime_module()
    node = shutil.which("node")
    if not node:
        raise CaligoError("Node >=22.12 is required for coding shells")
    workspace = Path(attempt["workspace"]).resolve()
    private = Path(attempt["git_dir"]).parent
    session_dir = Path(session_dir).resolve()
    home, temp = private / "home", session_dir / "tmp"
    home.mkdir(exist_ok=True)
    temp.mkdir(exist_ok=True)
    outputs = Path(variables["CALIGO_OUTPUTS"]).resolve()
    spec = controller.spec
    areas = spec["editable"] + [spec["executable"]]
    if attempt["mode"] == "supervisor":
        areas += spec["readable"]
    # System installations are shared read-only. Personal tool binaries on PATH
    # are available without exposing the rest of the user's home or projects.
    readable = [workspace, private, outputs, session_dir]
    readable += [Path("/private/var/select"), Path("/var/select")]
    readable += [Path(p) for p in ("/bin", "/sbin", "/usr", "/lib", "/lib64", "/System", "/Library",
                                   "/opt/homebrew", "/etc", "/private/etc", "/dev", "/proc",
                                   "/private/var/db", "/private/var/run", "/private/var/select") if Path(p).exists()]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder and Path(folder).is_dir() and Path(folder).resolve() not in workspace.parents:
            readable.append(Path(folder).resolve())
    for executable in (sys.executable, node):
        if executable:
            readable.append(Path(executable).resolve().parent.parent)
    shell = ["bash", "--noprofile", "--rcfile", str(session_dir / "bashrc")]
    network = {"allowedDomains": [], "deniedDomains": [],
               "allowUnixSockets": [str(session_dir), str(private), variables["CALIGO_SOCKET"]],
               "allowAllUnixSockets": sys.platform != "darwin"}
    context = dict(home=home, temp=temp, session_dir=session_dir, workspace=workspace,
                   spec=spec, env=variables, readable=readable, network=network,
                   shell=shell)
    from .addons import prepare
    prepare(attempt.get("addons", []), context)
    policy = {
        "filesystem": {
            "denyRead": ["/"],
            # macOS checks symlink paths as well as their destinations (e.g.
            # /etc -> /private/etc). Keep both or ordinary config lookups fail.
            "allowRead": sorted({str(q) for p in readable for q in (p.absolute(), p.resolve())}),
            "allowWrite": [str(workspace / area) for area in areas] + [str(private), str(outputs), str(temp)],
            "denyWrite": [],
        },
        "network": network,
        "allowPty": True,
    }
    settings = session_dir / "sandbox.json"
    write_json(settings, policy)
    runner = session_dir / "sandbox.mjs"
    runner.write_text('''import { readFileSync } from 'node:fs';
import { spawn } from 'node:child_process';
const { SandboxManager } = await import(process.argv[2]);
const config = JSON.parse(readFileSync(process.argv[3], 'utf8'));
try {
  // Networking is available for coding tools; filesystem access is the boundary.
  await SandboxManager.initialize(config, async () => true, false);
  const wrapped = await SandboxManager.wrapWithSandbox(process.argv[4]);
  const child = spawn('/bin/bash', ['-c', wrapped], { stdio: 'inherit' });
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
    process.on(signal, () => child.kill(signal));
  }
  const code = await new Promise((resolve, reject) => {
    child.on('error', reject);
    child.on('exit', (code, signal) => resolve(code ?? (signal === 'SIGINT' ? 130 : 1)));
  });
  process.exitCode = code;
} finally {
  SandboxManager.cleanupAfterCommand();
  await SandboxManager.reset();
}
''')
    variables.update(HOME=str(home), TMPDIR=str(temp),
                     CLAUDE_CODE_TMPDIR=str(temp),
                     BASH_ENV=str(session_dir / "bashrc"), CALIGO_ATTEMPT=attempt["name"],
                     CALIGO_CLIENT=str(session_dir / "bin/caligo"), CALIGO_SOCKET=str(session_dir / "control.sock"))
    if sys.platform == "darwin":
        # /usr/bin/git is an Xcode-selection shim. Resolve the installed binary
        # outside the restricted shell rather than granting access to GUI setup.
        resolved = subprocess.run(["/usr/bin/xcrun", "--find", "git"], capture_output=True, text=True)
        if resolved.returncode == 0:
            variables["PATH"] = str(Path(resolved.stdout.strip()).parent) + os.pathsep + variables["PATH"]
    # Do not inherit host Git overrides that could redirect operations elsewhere.
    for key in list(variables):
        if key.startswith("GIT_") and key not in {"GIT_DIR", "GIT_WORK_TREE"}:
            variables.pop(key)
    variables["GIT_CONFIG_NOSYSTEM"] = "1"
    variables["GIT_ATTR_NOSYSTEM"] = "1"
    command = argv or shell
    return subprocess.call([node, str(runner), module.as_uri(), str(settings), shlex.join(command)],
                           cwd=workspace / spec["executable"], env=variables)
