"""Run the real bootstrap/probe/archive code through a local SSH stand-in."""
from pathlib import Path
import sys

import pytest

from caligo.common import CaligoError
from caligo.manifest import load
from caligo.transport import Transport


@pytest.fixture
def remote(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    ssh = tmp_path / "ssh"
    ssh.write_text("#!" + sys.executable + "\nimport os, sys\nos.execv('/bin/sh', ['sh', '-c', sys.argv[-1]])\n")
    ssh.chmod(0o755)
    # Simulate the version reported by an old system interpreter, while running
    # the actual preflight script (no mock of subprocess or the probe result).
    old = binaries / "python3"
    old.write_text("#!" + sys.executable + "\nimport sys\nsys.version_info = (3, 6, 8)\nsys.version = '3.6.8'\nexec(sys.argv[2])\n")
    old.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binaries))
    transport = Transport("test-host")
    transport.ssh = [str(ssh)]
    return transport, home, binaries


def test_old_default_selects_modern_python_and_runs_shipped_agent(remote):
    transport, home, binaries = remote
    modern = binaries / "python3.12"
    modern.symlink_to(sys.executable)
    assert transport.install() == str(home)
    assert transport.python == str(modern)
    assert Path(transport.agent).is_file()
    # RPC remains usable after the default discovery name disappears.
    (binaries / "python3").unlink()
    result = transport.rpc({"action": "probe"})
    assert result["home"] == str(home)
    assert result["python"] == sys.version


def test_unsupported_host_fails_before_installing(remote):
    transport, home, _ = remote
    with pytest.raises(CaligoError, match=r"No usable Python 3.11\+ on test-host") as error:
        transport.install()
    assert "Python 3.6.8" in str(error.value)
    assert "project.toml" in str(error.value)
    assert not (home / ".local").exists()
    assert transport.agent is None


@pytest.mark.parametrize("explicit", ["python3", "missing-python"])
def test_explicit_interpreter_never_silently_falls_back(remote, explicit):
    transport, home, binaries = remote
    (binaries / "python3.12").symlink_to(sys.executable)
    transport.python = explicit
    with pytest.raises(CaligoError, match="without fallback"):
        transport.install()
    assert not (home / ".local").exists()


def test_explicit_executable_path_with_spaces(remote):
    transport, home, binaries = remote
    executable = binaries / "custom python"
    executable.symlink_to(sys.executable)
    transport.python = str(executable)
    assert transport.install() == str(home)
    assert transport.python == str(executable)
    assert transport.rpc({"action": "probe"})["home"] == str(home)


def test_ssh_failure_is_not_reported_as_a_python_requirement(remote):
    transport, _, _ = remote
    transport.ssh = ["/bin/sh", "-c", "echo 'Permission denied (publickey)' >&2; exit 255"]
    with pytest.raises(CaligoError, match=r"SSH connection to test-host failed: Permission denied"):
        transport.install()


@pytest.mark.parametrize("value", ['""', '123', '"-python"', '"python\\n"'])
def test_invalid_remote_python_is_rejected_at_manifest_load(project, value):
    path = project / "project.toml"
    path.write_text(path.read_text().replace('[remote]', '[remote]\npython = ' + value))
    with pytest.raises(CaligoError, match="remote.python must name"):
        load(path)
