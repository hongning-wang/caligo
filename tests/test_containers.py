"""Real Linux mount/permission checks (opt in with CALIGO_TEST_DOCKER=1)."""

import json
import os
from pathlib import Path
import subprocess

import pytest

from caligo import runtime, scheduler
from caligo.common import read_json, run
from test_harness import bootstrap


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("CALIGO_TEST_DOCKER") != "1",
        reason="set CALIGO_TEST_DOCKER=1 to use Docker",
    ),
]


def test_experiment_uses_frozen_inputs_and_read_only_fixed_setup(
    project, tmp_path, monkeypatch
):
    (project / "fixed/secret.txt").write_text("fixed-secret-do-not-expose")
    script = project / "executable/train.py"
    script.write_text(
        "import os\nfrom pathlib import Path\nassert not Path('/fixed').exists()\nassert 'CALIGO_FIXED' not in os.environ\n"
        + script.read_text()
        + "\n(outputs / 'metrics.json').write_text('{\"passed\": false}')\n"
    )
    c = bootstrap(project, tmp_path, monkeypatch)
    image = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "python:3.12-bookworm"]
    )
    provenance = read_json(c.project / "provenance.json")
    provenance["image"]["resolved"] = image
    from caligo.common import write_json

    write_json(c.project / "provenance.json", provenance)
    user = c.create("human", "baseline")
    w = Path(user["workspace"])
    monkeypatch.setattr(scheduler, "submit", lambda *args: "42")
    result = c.submit(user["id"], None, "/agent.pyz")
    (w / "src/model.py").write_text("def predict(value): return 999\n")
    assert c.execute(result["id"]) == 0
    artifacts = c.run_dir(result["id"]) / "artifacts"
    assert json.loads((artifacts / "predictions.json").read_text()) == [2, 4, 6]
    assert json.loads((artifacts / "metrics.json").read_text())["passed"] is False
    assert json.loads(
        (c.run_dir(result["id"]) / "evaluation/metrics.json").read_text()
    )["passed"]
    spec = c.spec
    command = runtime.command(
        spec,
        image,
        w,
        ["sh", "-c", "echo change > /fixed/secret.txt"],
        fixed=c.project / "fixed",
    )
    assert subprocess.call(command) != 0
    assert (c.project / "fixed/secret.txt").read_text() == "fixed-secret-do-not-expose"


def test_artifact_links_cannot_resolve_inside_evaluator(project, tmp_path, monkeypatch):
    (project / "executable/train.py").write_text(
        "import os\nfrom pathlib import Path\n(Path(os.environ['CALIGO_OUTPUTS'])/'predictions.json').symlink_to('/fixed/eval.py')\n"
    )
    c = bootstrap(project, tmp_path, monkeypatch)
    from caligo.common import write_json

    provenance = read_json(c.project / "provenance.json")
    provenance["image"]["resolved"] = run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", "python:3.12-bookworm"]
    )
    write_json(c.project / "provenance.json", provenance)
    attempt = c.create("human", "links")
    monkeypatch.setattr(scheduler, "submit", lambda *args: "42")
    record = c.submit(attempt["id"], None, "/agent.pyz")
    assert c.execute(record["id"]) != 0
    outcome = read_json(c.run_dir(record["id"]) / "outcome.json")
    assert "Symlinks" in outcome["error"]
    assert "evaluation_exit_code" not in outcome
    import io
    import tarfile
    from caligo.common import archive_records

    exported = io.BytesIO()
    archive_records(c.run_dir(record["id"]), exported)
    exported.seek(0)
    with tarfile.open(fileobj=exported) as archive:
        names = archive.getnames()
        assert any(name.endswith("/outcome.json") for name in names)
        assert not any(name.endswith("/predictions.json") for name in names)
