"""Workspace guidance follows the pinned project and reaches native agents."""

from pathlib import Path

from caligo.agents import instructions
from test_harness import bootstrap


def test_new_workspace_guide_uses_toml_and_reaches_agent(
    project, tmp_path, monkeypatch
):
    manifest = project / "project.toml"
    manifest.write_text(
        manifest.read_text()
        .replace('"src"', '"model"')
        .replace('"data"', '"dataset"')
        .replace('"executable"', '"scripts"')
        .replace('"executable/train.py"', '"scripts/train.py"')
        .replace('dev = "local"', 'dev = "devbox"')
        .replace('experiments = "local"', 'experiments = "cluster"')
    )
    for old, new in (("src", "model"), ("data", "dataset"), ("executable", "scripts")):
        (project / old).rename(project / new)
    controller = bootstrap(project, tmp_path, monkeypatch)
    attempt = controller.create("agent", "baseline")
    workspace = Path(attempt["workspace"])
    guide = workspace / "Agent.MD"
    text = guide.read_text()
    for value in (
        "model/",
        "scripts/",
        "dataset/",
        "devbox",
        "cluster",
        "scripts/train.py",
        "caligo exec",
        "caligo run",
        "checkpoint",
    ):
        assert value in text
    controller.check_workspace(workspace)
    monkeypatch.setenv("CALIGO_WORKSPACE", str(workspace))
    assert text in instructions()
