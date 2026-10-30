import json
import os
import shutil
import shlex
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from caligo import config, sandbox, sessions
from caligo.util import Error, read_json, run, write_json
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
    def test_coding_datasets_override_preserves_job_datasets(self):
        settings = config.load(
            config.STARTER + '\n[datasets]\ntraining = "/remote/data"\n'
        )
        self.experiment.config = settings
        with self.assertRaisesRegex(Error, "Dataset is unavailable"):
            sandbox.mounts(self.experiment)
        settings["coding_datasets"] = {}
        self.assertFalse(
            any(
                target.startswith("/datasets/")
                for _, target, _ in sandbox.mounts(self.experiment)
            )
        )
        self.assertEqual(settings["datasets"], {"training": "/remote/data"})
        dataset = self.root / "local-data"
        dataset.mkdir()
        settings["coding_datasets"] = {"training": str(dataset)}
        self.assertIn(
            (dataset, "/datasets/training", "ro"), sandbox.mounts(self.experiment)
        )

    def test_invalid_coding_dataset_paths_are_rejected(self):
        for value in ('"relative"', '"/bad:path"', '"/bad,path"', "42"):
            with self.subTest(value=value), self.assertRaises(Error):
                config.load(
                    config.STARTER + f"\n[coding_datasets]\ntraining = {value}\n"
                )
        with self.assertRaises(Error):
            config.load("coding_datasets = []\n" + config.STARTER)

    def test_remote_retry_replaces_generated_config_and_preserves_experiments(self):
        from caligo import cli, remote

        # Reproduce an old failed attempt: init generated an untracked config
        # in a remote mirror whose commit did not yet contain caligo.toml.
        run(["git", "rm", "--cached", "caligo.toml"], cwd=self.root)
        run(["git", "commit", "-m", "Before Caligo configuration"], cwd=self.root)
        remote_root = self.project.state / "remote cache"
        remote_root.mkdir()
        repository = remote_root / "admin"
        run(["git", "clone", self.root, repository])
        remote_project = Project(repository)
        remote_project.init()
        marker = remote_project.state / "keep.txt"
        marker.write_text("persistent controller state")

        text = config.STARTER.replace('coding = "local"', 'coding = "cluster"')
        (self.root / "caligo.toml").write_text(text)
        self.commit()
        write_json(self.project.state / "remote.json", {
            "host": "cluster", "python": sys.executable, "root": str(remote_root),
        })

        # Exercise the actual bundle, shell commands, deployment and remote CLI
        # locally, without requiring an SSH server or a container runtime.
        with patch.object(remote, "SSH") as ssh_class:
            ssh = ssh_class.return_value
            ssh.host, ssh.options = "cluster", []
            ssh.script.side_effect = lambda script: run(["sh", "-c", script])
            ssh.run.side_effect = lambda argv, **kwargs: ssh.script(shlex.join(argv), **kwargs)
            ssh.put.side_effect = shutil.copyfile
            ssh.interactive.side_effect = lambda argv, **kwargs: run(argv).returncode
            argv = ["create", "experiment", "recovered"]
            self.assertEqual(
                remote.forward(self.project, cli.parser().parse_args(argv), argv), 0
            )
            first = remote_project.experiment("recovered")
            self.assertEqual((first.worktree / "caligo.toml").read_text(), text)
            result = first.worktree / "experiments/result.txt"
            result.write_text("keep this experiment edit")
            first.set_goal("Keep this goal")

            (self.root / "core.py").write_text("VALUE = 2\n")
            self.commit()
            argv = ["create", "experiment", "next"]
            self.assertEqual(
                remote.forward(self.project, cli.parser().parse_args(argv), argv), 0
            )
        second = remote_project.experiment("next")
        self.assertEqual((second.worktree / "core.py").read_text(), "VALUE = 2\n")
        self.assertEqual((first.worktree / "core.py").read_text(), "VALUE = 1\n")
        self.assertEqual(result.read_text(), "keep this experiment edit")
        self.assertEqual(first.goal(), "Keep this goal")
        self.assertEqual(marker.read_text(), "persistent controller state")
        first.snapshot("job-preserved")

    def test_missing_committed_config_explains_how_to_recover(self):
        run(["git", "rm", "--cached", "caligo.toml"], cwd=self.root)
        run(["git", "commit", "-m", "Remove configuration"], cwd=self.root)
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    run(["git", "add", "caligo.toml"], cwd=self.root)
                result = self.cli("create", "experiment", "retry", check=False)
                self.assertEqual(result.returncode, 1)
                self.assertIn("git add caligo.toml", result.stderr)
                self.assertIn('git commit -m "Add Caligo configuration"', result.stderr)
                self.assertIn("Staging alone is not enough", result.stderr)
                self.assertNotIn("fatal:", result.stderr)
                self.assertFalse((self.project.state / "experiments/retry").exists())
        self.commit()
        self.cli("create", "experiment", "retry")
        experiment = self.project.experiment("retry")
        self.assertEqual(
            (experiment.worktree / "caligo.toml").read_text(), config.STARTER
        )
        experiment.snapshot("job-recovered")

    def test_missing_committed_config_fails_before_contacting_remote(self):
        from caligo import cli, remote

        run(["git", "rm", "--cached", "caligo.toml"], cwd=self.root)
        run(["git", "commit", "-m", "Remove configuration"], cwd=self.root)
        (self.root / "caligo.toml").write_text(
            config.STARTER.replace('coding = "local"', 'coding = "cluster"')
        )
        argv = ["create", "experiment", "retry"]
        with patch.object(remote, "SSH") as ssh:
            with self.assertRaisesRegex(Error, "committed admin baseline"):
                remote.forward(self.project, cli.parser().parse_args(argv), argv)
            ssh.assert_not_called()

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
Path('/outputs/task.json').write_text(json.dumps(sys.argv))
with open('/outputs/starts', 'a') as f: f.write('started\\n')
tty.setraw(sys.stdin.fileno())
print('› ', flush=True)
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
        self.assertNotIn(self.experiment.goal(), json.loads(task.read_text()))
        steering = self.experiment.state / "outputs/steering.txt"
        for _ in range(50):
            if steering.exists() and self.experiment.goal() in steering.read_text():
                break
            time.sleep(0.1)
        self.assertTrue(steering.read_text().startswith('/goal '))
        self.assertIn(self.experiment.goal(), steering.read_text())
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
