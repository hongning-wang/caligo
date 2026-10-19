"""Run one agent thread while the service owns its durable state and controls."""

from contextlib import nullcontext
import json
import queue
import threading
import time

from .common import CaligoError, identifier, read_json
from .session import session

MAX_AUTOMATIC_TURNS = 20


class ThreadRunner:
    def __init__(self, service, thread_id):
        self.service = service
        self.store = service.store
        self.controller = service.controller
        self.thread_id = thread_id
        self.thread = self.store.thread(thread_id)
        self.workflow = service.scoped_workflow(self.thread)
        self.ready = threading.Event()
        self.busy = threading.Event()
        self.last_end = time.monotonic()
        self.inflight = {}
        self.inflight_lock = threading.RLock()
        self.runtime = None
        self.watching = threading.Event()
        self.turn_lock = threading.RLock()
        self.review = None
        self.review_requested = False
        self.workflow.goal_review = self.review_goal

    def inspect_runs(self):
        from .scheduler import TERMINAL

        active = []
        for path in self.workflow.directory.glob("records/*/run.json"):
            record = read_json(path)
            if record.get("thread") != self.thread_id:
                continue
            status = self.workflow.status(record["id"])
            terminal = not status.get("poll_error") and (
                status["state"]
                in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY"}
                or status.get("scheduler", {}).get("state") in TERMINAL
            )
            if not terminal:
                active.append(record["id"])
            if not status.get("poll_error"):
                summary = {
                    key: status[key]
                    for key in ("state", "exit_code", "restart_count")
                    if key in status
                }
                self.store.observe_run(self.thread_id, record["id"], summary, terminal)
        return active

    def review_goal(self, decision, summary):
        if decision not in {"complete", "continue", "wait", "blocked"}:
            raise CaligoError("Review must be complete, continue, wait, or blocked")
        if not isinstance(summary, str) or not summary.strip():
            raise CaligoError("Explain the review with evidence or a next action")
        with self.turn_lock:
            if not self.busy.is_set():
                raise CaligoError("Review the objective during an active turn")
            active = self.inspect_runs()
            if decision == "complete" and (
                active or self.store.pending(self.thread_id)
            ):
                raise CaligoError(
                    "Inspect outstanding experiment results and queued instructions before completing"
                )
            if decision == "wait" and not active:
                raise CaligoError(
                    "No experiments are pending; continue, complete, or explain what is blocked"
                )
            self.review = {"decision": decision, "summary": summary.strip()}
            self.store.append(self.thread_id, "goal.reviewed", self.review)
            return {"accepted": True, "applies": "after this turn"}

    def watch_runs(self):
        while not self.watching.wait(1):
            try:
                self.inspect_runs()
            except (OSError, ValueError, CaligoError):
                continue

    def handle_event(self, message):
        with self.turn_lock:
            self._handle_event(message)

    def _handle_event(self, message):
        kind, data = message["type"], message.get("data", {})
        self.store.append(self.thread_id, kind, data, origin="backend")
        current = self.store.thread(self.thread_id)["state"]
        if kind == "agent.ready":
            if data.get("backend_id"):
                self.store.update(self.thread_id, backend_id=data["backend_id"])
            self.ready.set()
            self.store.transition(self.thread_id, ["STARTING"], "WAITING")
        elif kind == "backend.identity":
            self.store.update(self.thread_id, backend_id=data["id"])
        elif kind == "turn.started":
            self.busy.set()
            self.workflow.turn_id = data.get("id")
            self.store.transition(
                self.thread_id, ["STARTING", "WAITING", "RUNNING"], "RUNNING"
            )
        elif kind == "turn.completed":
            if data.get("status", "completed") != "completed":
                self.review = None
                self.review_requested = False
            if data.get("status") == "failed":
                self.service.retryable[self.thread_id] = False
            self.busy.clear()
            self.last_end = time.monotonic()
            if current not in {
                "PAUSED",
                "PAUSING",
                "STOPPING",
                "BLOCKED",
                "FAILED",
                "COMPLETING",
                "COMPLETED",
            }:
                status = data.get("status", "completed")
                self.store.transition(
                    self.thread_id,
                    ["RUNNING", "WAITING"],
                    {"interrupted": "PAUSED", "failed": "FAILED"}.get(
                        status, "WAITING"
                    ),
                )
        elif kind in {"agent.error", "agent.blocked", "agent.disconnected"}:
            if current not in {
                "STOPPING",
                "STOPPED",
                "COMPLETING",
                "COMPLETED",
                "PAUSED",
                "PAUSING",
            } and not (kind == "agent.disconnected" and current == "FAILED"):
                self.service.retryable[self.thread_id] = self.ready.is_set() and bool(
                    data.get("retryable", False)
                )
            self.busy.clear()
            if current not in {
                "STOPPING",
                "STOPPED",
                "COMPLETING",
                "COMPLETED",
                "PAUSED",
            }:
                self.store.transition(
                    self.thread_id,
                    ["STARTING", "WAITING", "RUNNING"],
                    "BLOCKED" if kind == "agent.blocked" else "FAILED",
                )
        elif kind in {"command.ack", "command.failed"}:
            with self.inflight_lock:
                command = self.inflight.pop(data["id"], None)
            if command:
                if command["action"] == "input":
                    self.store.message_state(
                        self.thread_id,
                        data["id"],
                        "DELIVERED" if kind == "command.ack" else "FAILED",
                    )
                    if kind == "command.failed":
                        self.busy.clear()
                        self.store.transition(
                            self.thread_id,
                            ["STARTING", "WAITING", "RUNNING"],
                            "BLOCKED",
                        )
                elif command["action"] == "interrupt":
                    self.store.transition(
                        self.thread_id,
                        ["PAUSING"],
                        ("WAITING" if command.get("resume") else "PAUSED")
                        if kind == "command.ack"
                        else "BLOCKED",
                    )

    def read_output(self, stream, stderr=False):
        try:
            for line in iter(stream.readline, ""):
                if stderr:
                    self.store.append(self.thread_id, "runtime.stderr", {"text": line})
                else:
                    self.handle_event(json.loads(line))
        except Exception as error:
            self.store.append(self.thread_id, "runtime.error", {"error": str(error)})
            self.store.update(self.thread_id, state="FAILED")

    def send(self, command):
        with self.inflight_lock:
            self.inflight[command["id"]] = command
        self.runtime.send(command)

    def deliver_input(self):
        with self.turn_lock:
            self._deliver_input()

    def block_goal(self, summary):
        if self.store.transition(self.thread_id, ["WAITING"], "BLOCKED"):
            self.store.append(self.thread_id, "goal.blocked", {"summary": summary})
            self.review = None
            self.review_requested = False

    def _deliver_input(self):
        if self.store.thread(self.thread_id)["state"] not in {"WAITING", "RUNNING"}:
            return
        pending = self.store.pending(self.thread_id)
        if pending:
            message = (
                next(
                    (item for item in pending if item["delivery"] == "steer"),
                    pending[0],
                )
                if self.busy.is_set()
                else pending[0]
            )
            if not self.busy.is_set() or message["delivery"] == "steer":
                automatic = message["author"] in {"caligo", "scheduler"}
                count = self.store.thread(self.thread_id)["automatic_turns"]
                if (
                    automatic
                    and not self.busy.is_set()
                    and count >= MAX_AUTOMATIC_TURNS
                ):
                    self.block_goal(
                        "Reached 20 automatic turns. Review progress and resume to continue."
                    )
                    return
                delivery = message["delivery"] if self.busy.is_set() else "queue"
                self.review = None
                self.review_requested = message["author"] == "caligo"
                self.store.update(
                    self.thread_id, automatic_turns=count + 1 if automatic else 0
                )
                self.store.message_state(self.thread_id, message["id"], "SENDING")
                if delivery == "queue":
                    self.busy.set()
                self.send(
                    {
                        "action": "input",
                        "id": message["id"],
                        "text": message["text"],
                        "delivery": delivery,
                    }
                )
        elif (
            not self.busy.is_set()
            and time.monotonic() - self.last_end
            >= self.controller.spec.get("agent", {}).get("continuation_delay", 2)
        ):
            if self.review:
                decision = self.review["decision"]
                if decision == "complete":
                    if not self.inspect_runs():
                        self.store.complete_goal(self.thread_id, self.review["summary"])
                    return
                if decision == "blocked":
                    self.block_goal(self.review["summary"])
                    return
                if decision == "wait":
                    # The watcher will enqueue a result and wake the agent once.
                    return
            if not self.thread["continuous"]:
                return
            if not self.review and self.review_requested:
                self.block_goal(
                    "The agent did not record an objective review. Send an instruction and resume."
                )
                return
            count = self.store.thread(self.thread_id)["automatic_turns"]
            if count >= MAX_AUTOMATIC_TURNS:
                self.block_goal(
                    "Reached 20 automatic turns. Review progress and resume to continue."
                )
                return
            self.store.enqueue(
                self.thread_id,
                "Review the objective: "
                + self.thread["goal"]
                + "\nInspect actual changes, checks, and experiment results. "
                "If done, call `caligo review complete 'evidence'` and stop. "
                "Otherwise make useful progress and call `caligo review continue 'next action'`, "
                "`caligo review wait 'pending experiments'`, or `caligo review blocked 'help needed'` before ending the turn. "
                "Do not add work beyond the objective.",
                author="caligo",
            )
            self.last_end = time.monotonic()

    def poll(self, process):
        started = time.monotonic()
        while process.poll() is None and not self.service.closing:
            state = self.store.thread(self.thread_id)["state"]
            if state in {"STOPPING", "COMPLETING", "FAILED"}:
                break
            if not self.ready.is_set():
                if time.monotonic() - started > 120:
                    raise CaligoError("Agent did not initialize within 120 seconds")
                time.sleep(0.05)
                continue
            try:
                control = self.service.controls[self.thread_id].get_nowait()
            except queue.Empty:
                control = None
            if control:
                control["id"] = identifier("control-")
                self.send(control)
            if state not in {"PAUSED", "PAUSING", "BLOCKED"}:
                self.deliver_input()
            time.sleep(0.05)

    def cleanup(self):
        self.watching.set()
        if self.runtime:
            self.runtime.stop()
        self.service.scopes.pop(self.thread_id, None)
        with self.inflight_lock:
            for command in self.inflight.values():
                if command["action"] == "input":
                    self.store.message_state(
                        self.thread_id, command["id"], "DELIVERY_UNKNOWN"
                    )

    def run(self):
        try:
            attempt = self.controller.environment(self.thread["attempt"])
            with session(self.workflow, attempt["id"], None) as directory:
                self.runtime = self.service.runtime_factory(
                    self.controller, attempt, self.thread, directory
                )
                if hasattr(self.runtime, "frozen"):
                    self.workflow.freeze = self.runtime.frozen
                else:
                    self.workflow.freeze = nullcontext
                self.service.scopes[self.thread_id] = self.workflow
                self.runtime.stop()  # Reconcile an orphan from a prior service process.
                if self.thread["state"] in {"STOPPING", "COMPLETING"}:
                    self.store.update(
                        self.thread_id,
                        state="COMPLETED"
                        if self.thread["state"] == "COMPLETING"
                        else "STOPPED",
                    )
                    return
                process = self.runtime.start()
                self.store.append(
                    self.thread_id, "runtime.started", {"image": self.runtime.resolved}
                )
                readers = [
                    threading.Thread(
                        target=self.read_output, args=(process.stdout,), daemon=True
                    ),
                    threading.Thread(
                        target=self.read_output,
                        args=(process.stderr, True),
                        daemon=True,
                    ),
                ]
                for reader in readers:
                    reader.start()
                watcher = threading.Thread(target=self.watch_runs, daemon=True)
                watcher.start()
                self.poll(process)
                self.runtime.stop()
                for reader in readers:
                    reader.join(timeout=5)
                current = self.store.thread(self.thread_id)["state"]
                if current == "STOPPING":
                    self.store.update(self.thread_id, state="STOPPED")
                elif current == "COMPLETING":
                    self.store.update(self.thread_id, state="COMPLETED")
                elif current not in {"FAILED", "BLOCKED", "PAUSED"}:
                    self.store.update(self.thread_id, state="FAILED")
        except Exception as error:
            self.store.append(self.thread_id, "runtime.error", {"error": str(error)})
            self.store.update(self.thread_id, state="FAILED")
        finally:
            self.cleanup()
