"""Preserve seed permissions and apply the project's write policy."""

from pathlib import Path
import stat
import subprocess
import sys
import json
import shutil

from .common import CaligoError
from .manifest import declared_areas


def protect(path):
    """Protect against ordinary writes, unlink, and editor atomic replacement."""
    path = Path(path)
    path.chmod(stat.S_IMODE(path.stat().st_mode) & ~0o222)
    if sys.platform == "darwin":
        # POSIX read-only files can still be replaced in a writable parent.
        # An inherited deny-delete ACL also protects atomic editor saves.
        result = subprocess.run(
            [
                "/bin/chmod",
                "+a",
                "everyone deny delete,write,append,writeattr,writeextattr",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise CaligoError("Cannot protect %s: %s" % (path, result.stderr.strip()))


def apply(workspace, spec, seed_modes):
    workspace = Path(workspace)
    for area in declared_areas(spec):
        folder = workspace / area
        records = seed_modes.get(area, {})
        for path in sorted(
            [folder, *folder.rglob("*")], key=lambda p: len(p.parts), reverse=True
        ):
            key = path.relative_to(folder).as_posix()
            original = records.get(key, {}).get(
                "mode", stat.S_IMODE(path.stat().st_mode)
            )
            path.chmod(original)
            if area in spec["readable"] or not original & 0o222:
                protect(path)
    # A writable parent would allow replacing a whole protected input folder.
    workspace.chmod(stat.S_IMODE(workspace.stat().st_mode) & ~0o222)


def human_command(workspace, spec, argv, extra_protected=()):
    """Enforce writes only; retain ordinary host reads, tools, and networking."""
    workspace = Path(workspace).resolve()
    writable = [workspace / area for area in spec["editable"] + [spec["executable"]]]
    protected = [
        Path(path).resolve() for path in extra_protected if Path(path).exists()
    ]
    for area in writable:
        for path in [area, *area.rglob("*")]:
            if not path.stat().st_mode & 0o200:
                protected.append(path)
    if sys.platform == "darwin":
        exceptions = " ".join(
            "(require-not (subpath %s))" % json.dumps(str(path)) for path in writable
        )
        policy = (
            "(version 1) (allow default)\n(deny file-write* (require-all (subpath %s) %s))\n"
            % (json.dumps(str(workspace)), exceptions)
        )
        for path in protected:
            policy += "(deny file-write* (%s %s))\n" % (
                "subpath" if path.is_dir() else "literal",
                json.dumps(str(path)),
            )
        return ["/usr/bin/sandbox-exec", "-p", policy, *argv]
    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if not bwrap:
            raise CaligoError(
                "Human write protection on Linux requires bubblewrap (bwrap); install it before entering an attempt"
            )
        result = [
            bwrap,
            "--die-with-parent",
            "--bind",
            "/",
            "/",
            "--ro-bind",
            str(workspace),
            str(workspace),
        ]
        for path in writable:
            result += ["--bind", str(path), str(path)]
        for path in protected:
            result += ["--ro-bind", str(path), str(path)]
        return result + ["--", *argv]
    raise CaligoError(
        "Human write protection requires macOS sandbox-exec or Linux bubblewrap"
    )
