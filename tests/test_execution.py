"""Process boundaries, shipped runtime, and standalone cluster compatibility."""

import ast
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from caligo import apptainer, process, remote, remote_job
from caligo.errors import Error
from caligo.transport import SSH


class ExecutionTest(unittest.TestCase):
    def test_captured_commands_preserve_arguments_input_and_status(self):
        arguments = ["with spaces", "a'b", "$HOME; touch unwanted", "line\nbreak"]
        code = "import sys,json; print(json.dumps(sys.argv[1:])); print(sys.stdin.read(),file=sys.stderr); sys.exit(7)"
        result = process.run(
            [sys.executable, "-c", code, *arguments], input="hello", check=False
        )
        self.assertEqual(json.loads(result.stdout), arguments)
        self.assertEqual(result.stderr.strip(), "hello")
        self.assertEqual(result.returncode, 7)
        with self.assertRaisesRegex(Error, "hello"):
            process.run([sys.executable, "-c", code], input="hello")

    def test_missing_executable_timeout_and_interactive_streams(self):
        with self.assertRaises(Error):
            process.run(["/nonexistent/caligo-program"])
        with self.assertRaises(Error):
            process.run(
                [sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.05
            )
        with patch.object(process.subprocess, "call", return_value=9) as call:
            self.assertEqual(process.interactive(["program", Path("/with spaces")]), 9)
        call.assert_called_once_with(["program", "/with spaces"])

    def test_ssh_argv_and_local_shortcut_preserve_literals(self):
        with patch.dict(os.environ, CALIGO_CODING_HOST="test-host"):
            ssh = SSH("test-host")
        arguments = ["a b", "a'b", "$HOME; echo bad", "line\nbreak"]
        result = ssh.run(
            [
                sys.executable,
                "-c",
                "import json,sys;print(json.dumps(sys.argv[1:]))",
                *arguments,
            ]
        )
        self.assertEqual(json.loads(result.stdout), arguments)
        ssh.local = False
        with patch("caligo.transport.run") as run:
            ssh.run(["program", *arguments], timeout=17)
        self.assertEqual(
            shlex.split(run.call_args.args[0][-1]), ["program", *arguments]
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 17)

    def test_deployed_package_runs_outside_source_checkout(self):
        with tempfile.TemporaryDirectory(prefix="caligo deployed ") as directory:
            root = Path(directory)
            project = SimpleNamespace(state=root)
            ssh = Mock()
            ssh.script.side_effect = lambda script, **kwargs: process.run(
                ["bash", "-lc", script], **kwargs
            )
            ssh.put.side_effect = shutil.copyfile
            cached = {"root": str(root)}
            runtime = remote.deploy_runtime(project, ssh, cached)
            result = process.run(
                [
                    "env",
                    "PYTHONPATH=" + runtime,
                    sys.executable,
                    "-c",
                    "import caligo,caligo.sessions.auth,caligo.sandbox.images,caligo.jobs.monitor; print(caligo.__file__)",
                ],
                cwd=root,
            )
            self.assertTrue(result.stdout.strip().startswith(runtime))
            self.assertTrue((Path(runtime) / "caligo/coding/Dockerfile").is_file())
            remote.deploy_runtime(project, ssh, cached)
            self.assertEqual(ssh.put.call_count, 1)

    def test_cluster_helpers_standalone_and_python36_syntax(self):
        package = Path(remote_job.__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            for name in ("remote_job.py", "image_cache.py", "apptainer.py"):
                source = package / name
                ast.parse(source.read_text(), feature_version=(3, 6))
                shutil.copyfile(source, Path(directory) / name)
            # Isolated interpreter avoids accidentally importing the local package.
            result = process.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    'import sys;sys.path.insert(0,sys.argv[1]);import remote_job; print(remote_job.partitions("cpu|up|8|(null)", {"cpus":2,"gpus":0}))',
                    directory,
                ]
            )
            self.assertEqual(result.stdout.strip(), "['cpu']")

    def test_apptainer_filters_only_runtime_overrides(self):
        env = {
            "PATH": "/bin",
            "CUDA_VISIBLE_DEVICES": "2",
            "APPTAINER_BIND": "/host",
            "SINGULARITYENV_SECRET": "bad",
        }
        self.assertEqual(
            apptainer.clean_environment(env),
            {"PATH": "/bin", "CUDA_VISIBLE_DEVICES": "2"},
        )
        self.assertIn("--containall", apptainer.exec_args("/private home"))

    def test_detached_process_preserves_environment_and_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "process.log"
            with path.open("w") as stream:
                child = process.detached(
                    [
                        sys.executable,
                        "-c",
                        'import os;print(os.environ["CALIGO_TEST_VALUE"])',
                    ],
                    stream,
                    dict(os.environ, CALIGO_TEST_VALUE="detached value"),
                )
                self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(path.read_text().strip(), "detached value")

    def test_cluster_workload_keeps_isolation_gpu_and_exit_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "decision.json").write_text('{"run":true}')
            spec = {
                "image": "test.sif",
                "command": ["python3", "experiment.py"],
                "phase": "dev",
                "image_digest": "sha256:test",
                "config": {
                    "writable": ["experiments"],
                    "datasets": {"train": "/data/train"},
                    "dev": {"hardware": {"gpus": 1}},
                },
            }
            with (
                patch.dict(
                    os.environ,
                    SLURM_JOB_ID="123",
                    CUDA_VISIBLE_DEVICES="2",
                    APPTAINER_BIND="/host",
                ),
                patch.object(remote_job.subprocess, "call", return_value=7) as call,
            ):
                self.assertEqual(remote_job.execute(root, spec), 7)
            args = call.call_args.args[0]
            self.assertIn("--containall", args)
            self.assertIn("--nv", args)
            self.assertIn("CUDA_VISIBLE_DEVICES=2", args)
            self.assertIn(str(root / "source") + ":/workspace:ro", args)
            self.assertIn(
                str(root / "source/experiments") + ":/workspace/experiments:rw", args
            )
            self.assertIn("/data/train:/datasets/train:ro", args)
            self.assertNotIn("APPTAINER_BIND", call.call_args.kwargs["env"])
            self.assertEqual(
                json.loads((root / "exit.json").read_text())["returncode"], 7
            )
