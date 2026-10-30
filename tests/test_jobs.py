"""Controller scheduling, recovery, and cancellation behavior."""

import base64
from unittest.mock import patch

from caligo.jobs import monitor, submission
from caligo.errors import Error
from caligo.util import read_json, write_json
from tests.test_caligo import ProjectFixture


class JobMonitorTest(ProjectFixture):
    def job(self, state="queued"):
        folder = self.experiment.snapshot("job-monitor")
        candidates = [
            {"host": host, "root": "/scratch/" + host, "offset": 0}
            for host in ("first", "second")
        ]
        job = {"id": "job-monitor", "state": state, "candidates": candidates}
        write_json(folder / "job.json", job)
        (folder / "output.log").touch()
        return folder, job

    def test_one_winner_retry_and_final_output_drain(self):
        folder, job = self.job()
        calls = []
        observations = 0

        def remote(candidate, operation, *args):
            nonlocal observations
            calls.append((candidate["host"], operation))
            if len(calls) == 1:
                raise Error("temporary SSH outage")
            if operation == "grant":
                self.assertEqual(read_json(folder / "job.json")["winner"], 0)
            if operation == "status":
                observations += 1
                if observations <= 2:
                    return {"ready": True, "state": "RUNNING", "log": "", "offset": 0}
                if observations == 3:
                    return {
                        "ready": True,
                        "exit": {"returncode": 0},
                        "log": base64.b64encode(b"first\n").decode(),
                        "offset": 6,
                    }
                if observations == 4:
                    return {"log": base64.b64encode(b"last\n").decode(), "offset": 11}
                return {"log": "", "offset": 11}
            if operation == "artifacts":
                return {"path": "/scratch/artifacts.tar.gz"}
            return {}

        with (
            patch.object(monitor, "remote", side_effect=remote),
            patch.object(monitor.time, "sleep"),
            patch.object(monitor, "SSH") as ssh,
            patch.object(monitor, "publish_logs"),
        ):
            ssh.return_value.get.side_effect = lambda source, target: (
                target.write_bytes(b"artifacts")
            )
            monitor.watch(self.experiment, job["id"])
        result = read_json(folder / "job.json")
        self.assertEqual(result["state"], "completed")
        self.assertEqual(
            [host for host, operation in calls if operation == "grant"], ["first"]
        )
        self.assertEqual(
            [host for host, operation in calls if operation == "cancel"], ["second"]
        )
        self.assertNotIn("monitor_error", result)
        self.assertEqual((folder / "output.log").read_bytes(), b"first\nlast\n")
        self.assertEqual((folder / "artifacts.tar.gz").read_bytes(), b"artifacts")

    def test_explicit_cancellation_cancels_all_candidates(self):
        folder, job = self.job()
        write_json(folder / "cancel.json", {})
        with (
            patch.object(monitor, "remote") as remote,
            patch.object(monitor, "publish_logs"),
        ):
            monitor.watch(self.experiment, job["id"])
        self.assertEqual(remote.call_count, 2)
        self.assertTrue(all(call.args[1] == "cancel" for call in remote.call_args_list))
        self.assertEqual(read_json(folder / "job.json")["state"], "cancelled")

    def test_interrupted_submission_is_recovered_by_cancelling_allocations(self):
        folder, job = self.job("submitting")
        with patch.object(monitor, "remote") as remote:
            monitor.watch(self.experiment, job["id"])
        self.assertEqual(remote.call_count, 2)
        self.assertEqual(read_json(folder / "job.json")["state"], "failed")

    def test_failed_submission_cleans_up_uploaded_candidates(self):
        self.experiment.config["dev"]["hosts"] = ["first"]
        with (
            patch.object(submission, "SSH") as ssh,
            patch.object(
                submission, "remote", side_effect=[Error("prepare failed"), {}]
            ) as remote,
        ):
            ssh.return_value.script.return_value.stdout = "/scratch\n"
            with self.assertRaisesRegex(Error, "prepare failed"):
                submission.submit(self.experiment, "dev", ["echo", "hello"])
        self.assertEqual(
            [call.args[1] for call in remote.call_args_list], ["prepare", "cancel"]
        )
        records = list((self.experiment.state / "jobs").glob("*/job.json"))
        self.assertEqual(read_json(records[0])["state"], "failed")

    def test_recovered_granted_winner_preserves_failure_status(self):
        folder, job = self.job("running")
        job.update(winner=1, granted=True)
        job["candidates"][0]["cancelled"] = True
        write_json(folder / "job.json", job)

        def remote(candidate, operation, *args):
            self.assertEqual(candidate["host"], "second")
            if operation == "status":
                return {
                    "ready": True,
                    "log": "",
                    "offset": 0,
                    "exit": {"returncode": 7},
                }
            self.assertEqual(operation, "artifacts")
            return {"path": "/artifacts.tar.gz"}

        with (
            patch.object(monitor, "remote", side_effect=remote),
            patch.object(monitor, "SSH"),
            patch.object(monitor, "publish_logs"),
        ):
            monitor.watch(self.experiment, job["id"])
        result = read_json(folder / "job.json")
        self.assertEqual(result["winner"], 1)
        self.assertEqual(result["returncode"], 7)
        self.assertEqual(result["state"], "failed")
