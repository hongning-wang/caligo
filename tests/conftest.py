from pathlib import Path
import shutil

import pytest


@pytest.fixture
def project(tmp_path):
    target = tmp_path / "project"
    shutil.copytree(Path(__file__).parents[1] / "examples/minimal", target, ignore=shutil.ignore_patterns(".caligo", ".research", "__pycache__"))
    return target
