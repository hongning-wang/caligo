"""Project login, credential isolation, and compatibility with remote projects."""

import os
import shutil
import signal
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from caligo import cli, config, remote, sandbox, sessions
from caligo.util import read_json, read_private, run, write_json, write_private
from caligo.workspace import Project
from tests.test_caligo import ProjectFixture


class ProjectLoginTest(ProjectFixture):
    def test_login_and_legacy_auth_do_not_require_an_experiment(self):
        for arguments, agent in (
            (["login", "codex"], "codex"),
            (["login", "claude"], "claude"),
            (["auth", "--agent", "codex"], "codex"),
            (["auth", "trial", "--agent", "claude"], "claude"),
        ):
            argv = ["--project", str(self.root), *arguments]
            with (
                self.subTest(arguments=arguments),
                patch.object(sessions, "authenticate", return_value=0) as authenticate,
                patch.object(Project, "experiment") as experiment,
            ):
                self.assertEqual(cli.execute(cli.parser().parse_args(argv), argv), 0)
                self.assertEqual(authenticate.call_args.args[0].root, self.project.root)
                self.assertEqual(authenticate.call_args.args[1], agent)
                experiment.assert_not_called()

    def test_import_and_relogin_share_only_selected_credentials_with_experiments(self):
        host_home = self.root / "host-home"
        host_home.mkdir()
        second = self.project.create("other")
        for agent in ("codex", "claude"):
            with self.subTest(agent=agent):
                relative = sessions.credential_path(agent)
                write_private(host_home, relative, b"test credentials")
                write_private(
                    host_home, ".codex/sessions/private.jsonl", b"host history"
                )
                with patch.object(Path, "home", return_value=host_home):
                    self.assertEqual(
                        sessions.authenticate(self.project, agent, import_auth=True), 0
                    )
                project_home = self.project.state / "auth/home"
                write_private(project_home, ".codex/config.toml", b"project settings")
                for experiment in (self.experiment, second):
                    write_private(
                        experiment.state / "home",
                        ".codex/sessions/own.jsonl",
                        b"own history",
                    )
                    sessions.share_project_credentials(experiment, agent)
                    target = experiment.state / "home" / relative
                    self.assertEqual(target.read_bytes(), b"test credentials")
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                    self.assertFalse(
                        (experiment.state / "home/.codex/config.toml").exists()
                    )
                    self.assertFalse(
                        (
                            experiment.state / "home/.codex/sessions/private.jsonl"
                        ).exists()
                    )
                    self.assertEqual(
                        (
                            experiment.state / "home/.codex/sessions/own.jsonl"
                        ).read_bytes(),
                        b"own history",
                    )
                if agent == "codex":
                    source = project_home / relative
                    for experiment in (self.experiment, second):
                        self.assertTrue(
                            source.samefile(experiment.state / "home" / relative)
                        )
                    # Match Codex's in-place token refresh writes.
                    (second.state / "home" / relative).write_bytes(b"refreshed token")
                    self.assertEqual(source.read_bytes(), b"refreshed token")
                    self.assertEqual(
                        (self.experiment.state / "home" / relative).read_bytes(),
                        b"refreshed token",
                    )
                write_private(project_home, relative, b"new account")
                sessions.share_project_credentials(self.experiment, agent)
                self.assertEqual(
                    read_private(self.experiment.state / "home", relative),
                    b"new account",
                )
        folder = self.experiment.snapshot("job-no-credentials")
        self.assertFalse((folder / "source/.caligo").exists())
        self.assertFalse((folder / "source/.codex").exists())

    def test_existing_experiment_credentials_survive_without_project_login(self):
        relative = sessions.credential_path("codex")
        write_private(self.experiment.state / "home", relative, b"legacy login")
        sessions.share_project_credentials(self.experiment, "codex")
        self.assertEqual(
            read_private(self.experiment.state / "home", relative), b"legacy login"
        )

    def test_credential_sharing_rejects_a_symlink_outside_the_experiment(self):
        relative = sessions.credential_path("codex")
        project_home = self.project.state / "auth/home"
        project_home.mkdir(parents=True)
        write_private(project_home, relative, b"test credentials")
        outside = self.root / "outside"
        outside.mkdir()
        (self.experiment.state / "home/.codex").symlink_to(outside)
        with self.assertRaises(OSError):
            sessions.share_project_credentials(self.experiment, "codex")
        self.assertEqual(list(outside.iterdir()), [])

    def test_login_sandbox_has_no_experiment_files_or_datasets(self):
        text = config.STARTER + '\n[datasets]\ntraining = "/unavailable"\n'
        (self.root / "caligo.toml").write_text(text)
        with patch.object(sandbox, "prepare") as prepare:
            sandbox.prepare_auth(self.project)
        context = prepare.call_args.args[0]
        mounts = sandbox.mounts(context)
        self.assertEqual(context.state, self.project.state / "auth")
        self.assertFalse(
            any(target.startswith("/datasets/") for _, target, _ in mounts)
        )
        self.assertFalse(
            any(source == self.experiment.worktree for source, _, _ in mounts)
        )
        self.assertEqual(list(context.worktree.iterdir()), [context.worktree / ".git"])

    def test_login_before_remote_mirror_creation_preserves_credentials(self):
        remote_root = self.root / "remote cache"
        remote_root.mkdir()
        host_home = self.root / "host-home"
        host_home.mkdir()
        write_private(host_home, ".codex/auth.json", b"test remote login")
        text = config.STARTER.replace('coding = "local"', 'coding = "cluster"')
        (self.root / "caligo.toml").write_text(text)
        self.commit()
        write_json(
            self.project.state / "remote.json",
            {
                "host": "cluster",
                "python": sys.executable,
                "root": str(remote_root),
            },
        )
        with (
            patch.dict(os.environ, HOME=str(host_home)),
            patch.object(remote, "SSH") as ssh_class,
            patch.object(remote.subprocess, "call") as call,
        ):
            ssh = ssh_class.return_value
            ssh.host, ssh.options = "cluster", []
            ssh.command.side_effect = lambda script, **kwargs: run(
                ["sh", "-c", script], **kwargs
            )
            ssh.put.side_effect = shutil.copyfile
            call.side_effect = lambda argv: run(["sh", "-c", argv[-1]]).returncode
            argv = ["login", "codex", "--import"]
            self.assertEqual(
                remote.forward(self.project, cli.parser().parse_args(argv), argv), 0
            )
            self.assertIn("-tt", call.call_args.args[0])
            remote_project = Project(remote_root / "admin")
            self.assertEqual(
                read_private(remote_project.state / "auth/home", ".codex/auth.json"),
                b"test remote login",
            )
            argv = ["create", "experiment", "after-login"]
            self.assertEqual(
                remote.forward(self.project, cli.parser().parse_args(argv), argv), 0
            )
        experiment = remote_project.experiment("after-login")
        sessions.share_project_credentials(experiment, "codex")
        self.assertEqual(
            read_private(experiment.state / "home", ".codex/auth.json"),
            b"test remote login",
        )


@unittest.skipUnless(
    os.environ.get("CALIGO_TEST_DOCKER") == "1",
    "set CALIGO_TEST_DOCKER=1 for real containers",
)
class ProjectLoginDockerTest(ProjectFixture):
    def test_one_project_login_starts_two_private_agent_sessions(self):
        second = self.project.create("other")
        fake_cli = """#!/usr/bin/env python3
import os, sys, tty
from pathlib import Path
credential = Path.home() / '.codex/auth.json'
if 'login' in sys.argv:
    if 'status' in sys.argv:
        sys.exit(0 if credential.read_text() == 'test login' else 1)
    credential.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text('test login')
    sys.exit(0)
Path('/outputs/started').write_text(credential.read_text())
tty.setraw(sys.stdin.fileno())
print('› ', flush=True)
while os.read(sys.stdin.fileno(), 4096):
    pass
"""
        auth_home = self.project.state / "auth/home"
        auth_home.mkdir(parents=True)
        for home in (auth_home, self.experiment.state / "home", second.state / "home"):
            write_private(home, ".local/bin/codex", fake_cli, mode=0o755)
        try:
            # The test runner has no terminal; keep stdin without requesting a TTY.
            with patch.object(
                sessions.subprocess,
                "call",
                side_effect=lambda argv: (
                    run(["-i" if arg == "-it" else arg for arg in argv]).returncode
                ),
            ):
                self.assertEqual(sessions.authenticate(self.project, "codex"), 0)
            for experiment in (self.experiment, second):
                experiment.set_goal("Wait for human input.")
                sessions.start(experiment, "codex")
                self.assertTrue(sessions.running(experiment))
                result = run(
                    sandbox.runtime(experiment).command(
                        [
                            "python3",
                            "-c",
                            "from pathlib import Path; assert (Path.home()/'.codex/auth.json').read_text() == 'test login'; assert not (Path.home()/'.codex/sessions').exists()",
                        ]
                    )
                )
                self.assertEqual(result.returncode, 0)
            run(
                sandbox.runtime(self.experiment).command(
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; (Path.home()/'.codex/auth.json').write_text('refreshed')",
                    ]
                )
            )
            self.assertEqual((auth_home / ".codex/auth.json").read_text(), "refreshed")
            run(
                sandbox.runtime(second).command(
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; assert (Path.home()/'.codex/auth.json').read_text() == 'refreshed'",
                    ]
                )
            )
        finally:
            for experiment in (self.experiment, second):
                sessions.stop(experiment)
                marker = read_json(experiment.state / "controller.json", {})
                if marker:
                    try:
                        os.kill(marker["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
