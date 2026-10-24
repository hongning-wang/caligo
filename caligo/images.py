"""Build once and reuse the coding tools for humans and agents."""

import hashlib
import json
import sys
from pathlib import Path

from .image_cache import ensure_sif
from .util import Error, locked, run

DEFAULT_CODING_IMAGE = "caligo-coding:local"
RECIPE = Path(__file__).with_name("coding")


def recipe_digest():
    digest = hashlib.sha256()
    for path in sorted(RECIPE.iterdir()):
        if path.is_file():
            digest.update(path.name.encode() + path.read_bytes())
    return digest.hexdigest()


def build_coding_image():
    print(
        "Building the shared coding image (reused by all experiments)...",
        file=sys.stderr,
        flush=True,
    )
    run(
        [
            "docker",
            "build",
            "--label",
            "caligo.recipe=" + recipe_digest(),
            "--tag",
            DEFAULT_CODING_IMAGE,
            RECIPE,
        ],
        timeout=1800,
    )
    return DEFAULT_CODING_IMAGE


def docker_image(image):
    cache = Path.home() / ".cache/caligo"
    with locked(cache / "docker-images.lock"):
        result = run(["docker", "image", "inspect", image], check=False)
        present = json.loads(result.stdout)[0] if result.returncode == 0 else None
        if image == DEFAULT_CODING_IMAGE:
            label = (
                (present["Config"].get("Labels") or {}).get("caligo.recipe")
                if present
                else None
            )
            if label != recipe_digest():
                build_coding_image()
                present = json.loads(run(["docker", "image", "inspect", image]).stdout)[
                    0
                ]
        elif present is None:
            run(["docker", "pull", image], timeout=900)
            present = json.loads(run(["docker", "image", "inspect", image]).stdout)[0]
        return present["Id"]


def apptainer_image(image):
    try:
        if image == DEFAULT_CODING_IMAGE:
            return ensure_sif(
                image, definition=RECIPE / "image.def", recipe=recipe_digest()
            )
        return ensure_sif(image)
    except RuntimeError as exc:
        raise Error(str(exc)) from exc
