"""Stage declared project files for validation and revision creation."""

from pathlib import Path

from .common import CaligoError, copy_tree, is_bucket, write_json
from .manifest import declared_areas


def agent_guide(spec):
    editable = ", ".join(
        f"`{area}/`" for area in spec["editable"] + [spec["executable"]]
    )
    readable = ", ".join(
        [*(f"`{area}/`" for area in spec["readable"]), "workspace root"]
    )
    default = f" (default: `{spec['execute']}`)" if spec.get("execute") else ""
    return (
        "# Agent\n\n"
        f"- Edit: {editable}; preserve existing read-only files. Paths are relative to the workspace root.\n"
        f"- Read only: {readable}. The evaluator is separate.\n"
        f"- `caligo exec -- COMMAND`: dev checks on `{spec['locations']['dev']}`; edits to the copy do not sync back.\n"
        f"- `caligo run [SCRIPT]`: submit a frozen experiment on `{spec['locations']['experiments']}`{default}.\n"
        "- `caligo status RUN_ID`, `caligo logs RUN_ID`: inspect runs before submitting more.\n"
        "- `caligo artifacts RUN_ID DEST`: download evidence (`evaluation/` results, `artifacts/` outputs).\n"
        "- `caligo cancel RUN_ID`: cancel; `caligo publish RUN_ID`: export results.\n"
        "- Agent threads: `caligo review complete|continue|wait|blocked 'reason'` before ending each turn.\n"
        "- Save checkpoints regularly in `$CALIGO_OUTPUTS`; reload them after restart/requeue.\n"
    )


def prepare(manifest, spec, destination):
    """Stage only declared filesystem contents, never source Git history."""
    destination = Path(destination)
    seed = destination / "seed"
    seed.mkdir(parents=True)
    fixed = (manifest.parent / spec["fixed"]).resolve()
    for area in declared_areas(spec):
        source = spec["inputs"].get(area, area)
        if is_bucket(source):
            (seed / area).mkdir()
        else:
            path = Path(source).expanduser()
            if not path.is_absolute():
                path = manifest.parent / path
            resolved = path.resolve()
            if (
                resolved == fixed
                or fixed in resolved.parents
                or resolved in fixed.parents
            ):
                raise CaligoError("An exposed input overlaps fixed setup: " + str(path))
            if path.exists():
                copy_tree(path, seed / area)
            else:
                raise CaligoError("Declared input folder does not exist: " + str(path))
    if fixed.exists():
        copy_tree(fixed, destination / "fixed")
    else:
        if spec.get("eval"):
            raise CaligoError("Fixed setup folder does not exist: " + str(fixed))
        (destination / "fixed").mkdir()
    if spec.get("eval") and not (destination / "fixed" / spec["eval"]).is_file():
        raise CaligoError("eval script is missing from fixed setup: " + spec["eval"])
    if spec.get("execute") and not is_bucket(
        spec["inputs"].get(spec["executable"], "")
    ):
        if not (seed / spec["execute"]).is_file():
            raise CaligoError(
                "Default execute script does not exist: " + spec["execute"]
            )
    write_json(destination / "project.json", spec)
