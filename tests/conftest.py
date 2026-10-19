from pathlib import Path
import shutil

import pytest


@pytest.fixture
def project(tmp_path):
    target = tmp_path / "project"
    shutil.copytree(Path(__file__).parents[1] / "examples/minimal", target, ignore=shutil.ignore_patterns(".caligo", ".research", "__pycache__"))
    return target


@pytest.fixture(autouse=True)
def remove_test_acls(tmp_path, monkeypatch):
    """Release test-owned protection ACLs so pytest can remove its workspaces."""
    monkeypatch.setenv('CALIGO_STATE_DIR', str(tmp_path / 'service-state'))
    yield
    import subprocess
    import sys
    if sys.platform == "darwin":
        subprocess.run(["/bin/chmod", "-RN", str(tmp_path)], capture_output=True)
