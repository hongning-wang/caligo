"""Real Linux mount/permission checks (opt in with CALIGO_TEST_DOCKER=1)."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from caligo import runtime, scheduler
from caligo.common import read_json, run
from test_harness import bootstrap


pytestmark = [pytest.mark.integration, pytest.mark.skipif(os.environ.get("CALIGO_TEST_DOCKER") != "1", reason="set CALIGO_TEST_DOCKER=1 to use Docker")]


def test_experiment_uses_frozen_inputs_and_read_only_fixed_setup(project, tmp_path, monkeypatch):
    (project / "fixed/secret.txt").write_text("fixed-secret-do-not-expose")
    c = bootstrap(project, tmp_path, monkeypatch)
    image = run(["docker", "image", "inspect", "--format", "{{.Id}}", "python:3.12-bookworm"])
    provenance = read_json(c.project / "provenance.json")
    provenance["image"]["resolved"] = image
    from caligo.common import write_json
    write_json(c.project / "provenance.json", provenance)
    user = c.create("user", "baseline")
    w = Path(user["workspace"])
    monkeypatch.setattr(scheduler, "submit", lambda *args: "42")
    result = c.submit(user["id"], None, "/agent.pyz")
    (w / "src/model.py").write_text("def predict(value): return 999\n")
    assert c.execute(result["id"]) == 0
    artifacts = c.run_dir(result["id"]) / "artifacts"
    assert json.loads((artifacts / "predictions.json").read_text()) == [2, 4, 6]
    assert json.loads((artifacts / "metrics.json").read_text())["passed"]
    spec = c.spec
    command = runtime.command(spec, image, w, ["sh", "-c", "echo change > /fixed/secret.txt"], fixed=c.project / "fixed")
    assert subprocess.call(command) != 0
    assert (c.project / "fixed/secret.txt").read_text() == "fixed-secret-do-not-expose"
