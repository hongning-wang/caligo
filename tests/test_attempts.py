"""User-facing attempt workflow, including a real isolated macOS shell."""
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import time

import pytest

from caligo.attempts import Project
from caligo.common import CaligoError, read_json
from caligo.coding import runtime_module


def test_names_are_project_scoped_and_duplicates_preserve_work(project, tmp_path):
    first = Project(project / "project.toml")
    baseline = first.create("baseline", "user")
    marker = Path(baseline["workspace"]) / "src/marker"
    marker.write_text("keep this")
    second = first.create("main", "user")
    with pytest.raises(CaligoError, match="already exists"):
        first.create("baseline", "user")
    with pytest.raises(CaligoError, match="Unknown attempt"):
        first.select("missing")
    assert first.read()["active_attempt"] == second["id"]
    first.select("baseline")
    assert marker.read_text() == "keep this"
    assert not (Path(second["workspace"]) / "src/marker").exists()
    another = tmp_path / "another-project"
    shutil.copytree(project, another, ignore=shutil.ignore_patterns(".caligo"))
    other = Project(another / "project.toml")
    other.create("baseline", "user")
    assert [a["name"] for a in other.workflow().list_attempts()["attempts"]] == ["baseline"]
    with pytest.raises(CaligoError, match="Unknown attempt"):
        other.select("main")
    with pytest.raises(CaligoError, match="Invalid identifier"):
        first.create("../escape", "user")


class Terminal:
    def __init__(self, command, cwd):
        self.master, slave = pty.openpty()
        self.process = subprocess.Popen(command, cwd=cwd, stdin=slave, stdout=slave, stderr=slave,
                                        start_new_session=True)
        os.close(slave)
        self.transcript = ""

    def until(self, marker):
        text = ""
        deadline = time.monotonic() + 20
        while marker not in text and time.monotonic() < deadline:
            if select.select([self.master], [], [], 0.2)[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                text += data.decode(errors="replace")
        self.transcript += text
        assert marker in text, self.transcript
        return text

    def send(self, command, marker):
        os.write(self.master, (command + "\n").encode())
        return self.until(marker)

    def close(self):
        if self.process.poll() is None:
            os.write(self.master, b"exit\n")
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        os.close(self.master)


@pytest.mark.skipif(sys.platform != "darwin", reason="Live sandbox verified on macOS")
def test_real_shell_create_switch_isolation_and_exec(project):
    try:
        runtime_module()
    except CaligoError:
        pytest.skip("Install srt for live coding-shell verification")
    cli = [sys.executable, "-m", "caligo"]
    terminal = Terminal(cli + ["create", "baseline", "--mode", "user"], project)
    try:
        terminal.until("caligo[baseline]")
        terminal.send("printf retained > ../src/marker", "caligo[baseline]")
        terminal.send("caligo create dropout --mode user", "caligo[dropout]")
        listing = terminal.send("caligo list", "caligo[dropout]")
        assert "* dropout" in listing and "baseline" in listing
        project_state = Project(project / "project.toml")
        baseline = project_state.find("baseline")
        dropout = project_state.find("dropout")
        assert not (Path(dropout["workspace"]) / "src/marker").exists()
        # The shell cannot access neighbors, the project definition, fixed
        # evaluation, or shared Git metadata; user-readable data stays read-only.
        forbidden = [Path(baseline["workspace"]) / "src/marker", Path(baseline["git_dir"]) / "config",
                     project / "project.toml", project / "fixed/eval.py"]
        probe = Path(dropout["workspace"]) / "executable/probe.py"
        probe.write_text('''from pathlib import Path
import os
workspace = Path(os.environ["CALIGO_WORKSPACE"])
assert {p.name for p in workspace.iterdir()} == {"src", "data", "executable"}
for path in ''' + repr([str(p) for p in forbidden]) + ''':
    try:
        Path(path).read_text()
    except (PermissionError, FileNotFoundError):
        pass
    else:
        raise AssertionError("Readable outside attempt: " + path)
for path in (workspace / "undeclared", workspace / "data/values.json"):
    try:
        path.write_text("should fail")
    except PermissionError:
        pass
    else:
        raise AssertionError("Writable protected path: " + str(path))
print("ISOLATION_OK")
''')
        output = terminal.send("python3 probe.py", "caligo[dropout]")
        assert "ISOLATION_OK" in output and "Traceback" not in output
        terminal.send("caligo switch baseline", "caligo[baseline]")
        output = terminal.send("cat ../src/marker; echo; git status --short", "caligo[baseline]")
        assert "retained" in output and "marker" in output and "fatal:" not in output
        # Remote execution is an explicit copy even with dev=local. Preserve
        # stdout/stderr/exit status without writing remote changes into coding.
        output = terminal.send("caligo exec -- sh -c 'echo dev > ../src/marker; echo DEV_OUT; echo DEV_ERR >&2; exit 7'; echo RESULT:$?", "caligo[baseline]")
        assert "DEV_OUT\r\n" in output and "DEV_ERR\r\n" in output and "RESULT:7" in output
        assert (Path(baseline["workspace"]) / "src/marker").read_text() == "retained"
        output = terminal.send("caligo switch missing; echo RESULT:$?", "caligo[baseline]")
        assert "Unknown attempt" in output and "RESULT:1" in output
    finally:
        terminal.close()
    assert terminal.process.returncode == 0
