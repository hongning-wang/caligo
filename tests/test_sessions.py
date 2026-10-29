"""Native goal delivery and waiting for quiet jobs."""

import io
import json
import shutil
import sys
import time
import unittest
from unittest.mock import Mock, patch

from caligo import cli, sessions
from caligo.util import Error
from tests.test_caligo import ProjectFixture


@unittest.skipUnless(shutil.which("tmux"), "requires tmux")
class NativeGoalTest(ProjectFixture):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("Fake native CLI did not receive the goal")

    def test_start_revise_reattach_and_resume_use_native_goal_for_both_agents(self):
        script = self.root / "fake-agent.py"
        received = self.experiment.state / "outputs/received"
        launches = self.experiment.state / "outputs/launches"
        script.write_text(f"""
import json, os, sys, tty
from pathlib import Path
if 'status' in sys.argv:
    sys.exit(0)
with open({str(launches)!r}, 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
tty.setraw(sys.stdin.fileno())
print('\\x1b[?2004h' + ('› ' if sys.argv[1] == 'codex' else '❯ '), flush=True)
while True:
    data = os.read(sys.stdin.fileno(), 65536)
    if not data:
        break
    with open({str(received)!r}, 'ab') as stream:
        stream.write(data)
""")
        backend = Mock()
        backend.command.side_effect = lambda command, **kwargs: [
            sys.executable, str(script), *command
        ]
        for agent in ("codex", "claude"):
            with (
                self.subTest(agent=agent),
                patch.object(sessions.broker, "ensure"),
                patch.object(sessions.sandbox, "prepare", return_value=backend),
                patch.object(sessions.sandbox, "runtime", return_value=backend),
                patch.object(sessions.sandbox, "require_agent"),
            ):
                received.unlink(missing_ok=True)
                launches.unlink(missing_ok=True)
                self.experiment.set_goal("Compare three rates.\nReport validation scores.")
                try:
                    sessions.start(self.experiment, agent)
                    self.wait_for(lambda: received.exists() and b"\r" in received.read_bytes())
                    first = received.read_bytes()
                    self.assertTrue(first.startswith(b"/goal \x1b[200~"), first)
                    self.assertIn(b"Compare three rates.", first)
                    self.assertIn(b"Report validation scores.", first)
                    self.assertTrue(first.endswith(b"\x1b[201~\r"), first)
                    self.assertIn(b"Keep the goal active", first)

                    sessions.start(self.experiment, agent)
                    self.assertEqual(len(launches.read_text().splitlines()), 1)
                    self.assertTrue(sessions.steer(self.experiment, "Try only two rates."))
                    self.wait_for(lambda: b"Try only two rates." in received.read_bytes())
                    revision = received.read_bytes()[len(first):]
                    self.assertTrue(revision.startswith(b"/goal \x1b[200~"), revision)
                    self.assertIn(b"Goal: Try only two rates.", revision)
                    sessions.stop(self.experiment)
                    self.assertFalse(sessions.steer(self.experiment, "Do not deliver."))

                    # A new process resumes history and reestablishes the native goal.
                    history = self.experiment.state / "home" / (
                        ".codex/sessions" if agent == "codex" else ".claude/projects"
                    )
                    history.mkdir(parents=True, exist_ok=True)
                    (history / "conversation.jsonl").touch()
                    self.experiment.set_goal("Finish the revised experiment.")
                    sessions.start(self.experiment, agent)
                    self.wait_for(lambda: b"Finish the revised experiment." in received.read_bytes())
                    commands = [json.loads(line) for line in launches.read_text().splitlines()]
                    self.assertEqual(len(commands), 2)
                    self.assertNotIn("Goal:", " ".join(commands[0]))
                    self.assertIn("resume" if agent == "codex" else "--continue", commands[1])
                    self.assertEqual(received.read_bytes().count(b"/goal "), 3)
                finally:
                    sessions.stop(self.experiment)

    def test_startup_dialog_does_not_receive_goal(self):
        with (
            patch.object(sessions, "running", return_value=True),
            patch.object(sessions, "run", return_value=Mock(stdout="Accept trust dialog?\n› 1. Yes")) as command,
            patch.object(sessions.time, "monotonic", side_effect=[0, 0, 61]),
            patch.object(sessions.time, "sleep"),
        ):
            with self.assertRaisesRegex(Error, "goal has not been sent"):
                sessions.wait_for_input(self.experiment, "codex")
        self.assertTrue(all("send-keys" not in call.args[0] for call in command.call_args_list))


class FollowJobTest(unittest.TestCase):
    def test_quiet_submission_queue_and_running_states_wait_until_terminal_result(self):
        states = iter(["submitting", "submitting", "queued", "running", "failed"])
        fetch = Mock(side_effect=lambda offset: {
            "state": next(states), "text": "", "offset": offset, "returncode": 7
        })
        stderr = io.StringIO()
        with (
            patch.object(cli.time, "sleep"),
            patch.object(cli.time, "monotonic", side_effect=[0, 0, 31, 32, 33, 34]),
            patch.object(cli.sys, "stderr", stderr),
        ):
            self.assertEqual(cli.follow(fetch), 7)
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(stderr.getvalue().count("submitting; waiting"), 2)
        self.assertIn("queued; waiting", stderr.getvalue())
        self.assertIn("running; waiting", stderr.getvalue())

    def test_terminal_output_is_drained_and_no_follow_returns_without_waiting(self):
        for wait, state in ((True, "completed"), (False, "running")):
            with self.subTest(wait=wait):
                responses = iter([
                    {"state": state, "text": "final result\n", "offset": 13, "returncode": 0},
                    {"state": state, "text": "", "offset": 13, "returncode": 0},
                ])
                output, status = io.StringIO(), io.StringIO()
                with (
                    patch.object(cli.time, "sleep"),
                    patch.object(cli.sys, "stdout", output),
                    patch.object(cli.sys, "stderr", status),
                ):
                    self.assertEqual(cli.follow(lambda offset: next(responses), wait=wait), 0)
                self.assertEqual(output.getvalue(), "final result\n")
                self.assertEqual(status.getvalue(), "")

    def test_interrupting_follower_does_not_cancel_job(self):
        with patch.object(cli.jobs, "cancel") as cancel:
            with self.assertRaises(KeyboardInterrupt):
                cli.follow(Mock(side_effect=KeyboardInterrupt))
            cancel.assert_not_called()
