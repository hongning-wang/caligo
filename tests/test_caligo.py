import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from caligo import config, sandbox, sessions
from caligo.util import Error, read_json, run
from caligo.workspace import Project


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="caligo-test-")
        self.root = Path(self.temporary.name)
        self.project = Project(self.root)
        self.project.init()
        (self.root / "caligo.toml").write_text(config.STARTER)
        (self.root / "evaluation").mkdir()
        (self.root / "evaluation/run.py").write_text("print('evaluation')\n")
        (self.root / "core.py").write_text("VALUE = 1\n")
        (self.root / "experiments").mkdir()
        (self.root / "experiments/config.json").write_text('{"rate": 0.1}\n')
        run(["git", "config", "user.name", "Caligo Test"], cwd=self.root)
        run(["git", "config", "user.email", "caligo-test@localhost"], cwd=self.root)
        self.commit()
        self.experiment = self.project.create("trial")

    def commit(self):
        run(["git", "add", "."], cwd=self.root)
        run(["git", "commit", "-m", "baseline"], cwd=self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, *arguments, check=True):
        return run(
            [sys.executable, "-m", "caligo", "--project", self.root, *arguments],
            check=check,
        )


class WorkspaceTest(ProjectFixture):
    def test_experiment_edits_do_not_change_the_admin_or_other_experiments(self):
        second = self.project.create("other")
        path = self.experiment.worktree / "experiments/config.json"
        path.write_text('{"rate": 0.5}\n')
        self.assertEqual(
            json.loads((second.worktree / "experiments/config.json").read_text())[
                "rate"
            ],
            0.1,
        )
        self.assertEqual(
            json.loads((self.root / "experiments/config.json").read_text())["rate"], 0.1
        )

    def test_protected_edits_additions_deletions_and_empty_directories_are_rejected(
        self,
    ):
        for operation in ("edit", "delete", "add", "mkdir"):
            with self.subTest(operation=operation):
                core = self.experiment.worktree / "core.py"
                added = self.experiment.worktree / "new"
                if operation == "edit":
                    core.write_text("changed")
                elif operation == "delete":
                    core.unlink()
                elif operation == "add":
                    added.write_text("new")
                else:
                    added.mkdir()
                with self.assertRaises(Error):
                    self.experiment.snapshot("job-" + operation)
                core.write_text("VALUE = 1\n")
                if added.is_dir():
                    added.rmdir()
                else:
                    added.unlink(missing_ok=True)

    def test_escaping_symlinks_and_replaced_mounts_are_rejected(self):
        link = self.experiment.worktree / "experiments/leak"
        link.symlink_to(self.root / "core.py")
        with self.assertRaises(Error):
            self.experiment.snapshot("job-link")
        link.unlink()
        shutil.rmtree(self.experiment.worktree / "experiments")
        (self.experiment.worktree / "experiments").symlink_to(
            "evaluation", target_is_directory=True
        )
        with self.assertRaises(Error):
            self.experiment.snapshot("job-mount")

    def test_policy_is_pinned_to_admin_commit(self):
        (self.root / "caligo.toml").write_text(
            config.STARTER.replace('["experiments"]', '["core"]')
        )
        second = self.project.create("pinned")
        (second.worktree / "core.py").write_text("changed")
        with self.assertRaises(Error):
            second.snapshot("job-fixed-policy")

    def test_goal_and_job_access_are_scoped(self):
        self.cli("goal", "trial", "Try three variants.")
        self.cli("create", "experiment", "other")
        self.assertEqual(
            self.cli("goal", "trial").stdout.strip(), "Try three variants."
        )
        self.assertEqual(self.cli("goal", "other").stdout.strip(), "")
        self.assertNotEqual(
            self.cli(
                "-e", "other", "logs", "job-unknown", "--no-follow", check=False
            ).returncode,
            0,
        )


@unittest.skipUnless(
    os.environ.get("CALIGO_TEST_DOCKER") == "1",
    "set CALIGO_TEST_DOCKER=1 for real containers",
)
class DockerIntegrationTest(ProjectFixture):
    def tearDown(self):
        try:
            sessions.stop(self.experiment)
            marker = read_json(self.experiment.state / "controller.json", {})
            if marker:
                try:
                    os.kill(marker["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
        finally:
            super().tearDown()

    def test_runtime_enforces_file_boundaries_and_controller_scope(self):
        other = self.project.create("hidden")
        (other.worktree / "experiments/secret").write_text("private")
        dataset = self.root / "host-data"
        dataset.mkdir()
        (dataset / "samples").write_text("shared data")
        self.experiment.config["datasets"] = {"training": str(dataset)}
        backend = sandbox.prepare(self.experiment)
        script = f"""
import os
from pathlib import Path
assert not Path({str(other.worktree)!r}).exists()
assert not Path({str(self.root)!r}).exists()
assert not Path('/var/run/docker.sock').exists()
assert Path('/datasets/training/samples').read_text() == 'shared data'
for target in ['/workspace/core.py', '/workspace/evaluation/run.py', '/workspace/new-file']:
    try:
        Path(target).write_text('forbidden')
    except OSError:
        pass
    else:
        raise AssertionError('Writable protected path: ' + target)
Path('/workspace/experiments/result').write_text('ok')
Path('/outputs/result').write_text('ok')
try:
    Path('/datasets/training/samples').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('Shared dataset is writable')
print('ISOLATION_OK')
"""
        self.assertIn(
            "ISOLATION_OK", run(backend.command(["python3", "-c", script])).stdout
        )
        from caligo import broker

        broker.ensure(self.experiment)
        result = run(backend.command(["sh", "-lc", "caligo jobs"]))
        self.assertEqual(result.stdout, "")
        result = run(
            backend.command(["caligo", "--experiment", "hidden", "jobs"]), check=False
        )
        self.assertNotEqual(result.returncode, 0)

    def test_agent_runs_without_attach_and_receives_steering(self):
        binary = self.experiment.state / "home/.local/bin/codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("""#!/usr/bin/env python3
import json, os, sys, tty
from pathlib import Path
if 'login' in sys.argv:
    sys.exit(0)
Path('/outputs/task.json').write_text(json.dumps(sys.argv[-1]))
with open('/outputs/starts', 'a') as f: f.write('started\\n')
print('ready', flush=True)
tty.setraw(sys.stdin.fileno())
data = b''
while True:
    part = os.read(sys.stdin.fileno(), 4096)
    if not part:
        break
    data += part
    Path('/outputs/steering.txt').write_bytes(data)
""")
        binary.chmod(0o755)
        self.cli("goal", "trial", "Compare three rates, then stop.")
        self.cli("start", "trial", "--agent", "codex")
        task = self.experiment.state / "outputs/task.json"
        for _ in range(50):
            if task.exists():
                break
            time.sleep(0.1)
        self.assertIn(self.experiment.goal(), json.loads(task.read_text()))
        self.cli("start", "trial", "--agent", "codex")
        self.assertEqual(
            (self.experiment.state / "outputs/starts").read_text().splitlines(),
            ["started"],
        )
        self.cli("goal", "trial", "Try only two rates.")
        steering = self.experiment.state / "outputs/steering.txt"
        for _ in range(50):
            if steering.exists() and "Try only two" in steering.read_text():
                break
            time.sleep(0.1)
        self.assertIn("Try only two rates.", steering.read_text())
        self.cli("stop", "trial")
        self.assertIn("stopped", self.cli("status", "trial").stdout)

    def test_coding_tools_are_shared_while_experiment_homes_remain_private(self):
        second = self.project.create("other")
        first_backend = sandbox.prepare(self.experiment)
        second_backend = sandbox.prepare(second)
        try:
            for backend in (first_backend, second_backend):
                run(
                    backend.command(
                        [
                            "bash",
                            "-c",
                            "for tool in bash vim tmux git rg python3 node codex claude; do command -v $tool || exit 1; done",
                        ]
                    )
                )
            run(first_backend.command(["bash", "-c", "echo private > ~/.test-marker"]))
            run(second_backend.command(["bash", "-c", "test ! -e ~/.test-marker"]))
            first_image = run(
                ["docker", "inspect", "--format", "{{.Image}}", first_backend.name]
            ).stdout
            second_image = run(
                ["docker", "inspect", "--format", "{{.Image}}", second_backend.name]
            ).stdout
            self.assertEqual(first_image, second_image)
        finally:
            second_backend.stop()


if __name__ == "__main__":
    unittest.main()
