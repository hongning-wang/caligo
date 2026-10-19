"""Persistent, project-scoped agent service and its HTTP/SSE interface."""

import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

from .agents import BACKENDS
from .common import (
    CaligoError,
    digest,
    lock,
    read_json,
    valid_id,
    write_json,
)
from .containers import Container
from .store import Store
from .thread_runner import ThreadRunner


def service_directory(controller):
    # Keep SQLite WAL and live service metadata off shared experiment storage.
    # Sites with network-mounted homes must set CALIGO_STATE_DIR to local disk.
    base = Path(os.environ.get("CALIGO_STATE_DIR", Path.home() / ".local/state/caligo"))
    return (
        base.expanduser().resolve()
        / digest([str(controller.root), controller.project.name])[:24]
    )


class Service:
    def __init__(self, controller, state, directory=None, runtime_factory=Container):
        from .workflow import Workflow, Endpoint

        self.controller, self.state = controller, state
        self.directory = Path(directory or service_directory(controller))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.store = Store(self.directory / "threads.sqlite3")
        self.runtime_factory = runtime_factory
        self.workers, self.controls, self.scopes, self.guard = (
            {},
            {},
            {},
            threading.RLock(),
        )
        self.retryable = {}
        self.closing = False
        # On remote coding hosts this is a new host-local run registry. The CLI
        # discovers its run routes through service-request, never through mounts.
        directory = state.get("client_directory") or self.directory / "client"
        self.workflow = Workflow(
            state,
            directory,
            coding_override=Endpoint(
                "local",
                controller.root,
                controller.project.name,
                state["spec"]["remote"],
            ),
        )

    def scoped_workflow(self, thread):
        from .workflow import Workflow

        return Workflow(
            self.state,
            self.workflow.directory,
            coding_override=self.workflow.coding_override,
            agent_scope=True,
            thread_id=thread["id"],
            event_sink=lambda kind, data: self.store.append(thread["id"], kind, data),
        )

    def create(self, attempt_id, backend, goal, continuous=True, model=None):
        if backend not in BACKENDS:
            raise CaligoError("Backend must be codex or claude")
        attempt = self.controller.environment(attempt_id)
        if attempt["mode"] != "agent":
            raise CaligoError("Threads require an agent attempt")
        if not isinstance(goal, str) or not goal.strip():
            raise CaligoError("An agent thread requires a goal")
        if type(continuous) is not bool:
            raise CaligoError("continuous must be a boolean")
        if model is not None:
            if (
                not isinstance(model, str)
                or not model.strip()
                or model.startswith("-")
                or any(c in model for c in "\n\0")
            ):
                raise CaligoError("Enter a valid model ID")
            model = model.strip()
        else:
            model = self.controller.spec.get("agent", {}).get("model")
        # Check Docker/image availability before leaving a durable STARTING record.
        from .containers import image

        resolved = None
        if self.runtime_factory is Container:
            with lock(self.controller.project / "agent-image.lock"):
                attempt = self.controller.environment(attempt_id)
                resolved = image(self.controller.spec, attempt.get("agent_image"))
                attempt["agent_image"] = resolved
                write_json(
                    self.controller.project / "attempts" / (attempt_id + ".json"),
                    attempt,
                )
        thread = self.store.create(
            attempt_id, backend, goal, continuous, resolved, model
        )
        self.store.enqueue(thread["id"], goal, author="operator")
        self.launch(thread["id"])
        return thread

    def launch(self, thread_id):
        with self.guard:
            if thread_id in self.workers and self.workers[thread_id].is_alive():
                return
            self.controls[thread_id] = queue.Queue()
            worker = threading.Thread(
                target=self.supervise, args=(thread_id,), daemon=True
            )
            self.workers[thread_id] = worker
            worker.start()

    def supervise(self, thread_id):
        for attempt in range(6):
            self.retryable[thread_id] = False
            self.run(thread_id)
            state = self.store.thread(thread_id)["state"]
            if self.closing or state != "FAILED" or not self.retryable.get(thread_id):
                return
            if attempt == 5:
                self.store.update(thread_id, state="BLOCKED")
                self.store.append(thread_id, "runtime.retry_exhausted", {})
                return
            delay = min(30, 2**attempt)
            if not self.store.transition(thread_id, ["FAILED"], "RETRYING"):
                return
            self.store.append(
                thread_id, "runtime.retry", {"attempt": attempt + 1, "delay": delay}
            )
            until = time.monotonic() + delay
            while (
                time.monotonic() < until
                and self.store.thread(thread_id)["state"] == "RETRYING"
                and not self.closing
            ):
                time.sleep(0.1)
            state = self.store.thread(thread_id)["state"]
            if state == "STOPPING":
                self.store.update(thread_id, state="STOPPED")
                return
            if state not in {"RETRYING", "WAITING", "STARTING"} or self.closing:
                return
            self.store.update(thread_id, state="STARTING")

    def recover(self):
        for thread in self.store.threads():
            if thread["state"] not in {
                "STOPPED",
                "COMPLETED",
                "FAILED",
                "BLOCKED",
                "PAUSED",
            }:
                self.store.recover(thread["id"])
                if thread["state"] in {"STOPPING", "COMPLETING"}:
                    self.store.update(thread["id"], state=thread["state"])
                else:
                    self.store.update(thread["id"], state="STARTING")
                self.launch(thread["id"])

    def message(self, thread_id, values):
        thread = self.store.thread(thread_id)
        delivery = values.get("delivery", "queue")
        if (
            delivery == "steer"
            and not BACKENDS[thread["backend"]].capabilities["steer"]
        ):
            raise CaligoError(
                "This backend supports queued messages and interruption, not active steering"
            )
        result = self.store.enqueue(
            thread_id,
            values["text"],
            delivery,
            values.get("author", "operator"),
            values.get("id"),
        )
        return result

    def control(self, thread_id, action, text=None):
        thread = self.store.thread(thread_id)
        if action not in {"interrupt", "pause", "resume", "stop"}:
            raise CaligoError("Unknown thread control")
        if thread["state"] in {"STOPPED", "COMPLETING", "COMPLETED"}:
            if action == "stop":
                return thread
            raise CaligoError("Thread is finished; create a new thread on the attempt")
        with self.guard:
            alive = thread_id in self.workers and self.workers[thread_id].is_alive()
            if action == "resume":
                if thread["state"] in {"STARTING", "RUNNING", "WAITING"}:
                    return thread
                if thread["state"] == "PAUSING":
                    raise CaligoError(
                        "Interruption is still in progress; resume after it is applied"
                    )
                self.store.update(
                    thread_id,
                    state="WAITING" if alive else "STARTING",
                    automatic_turns=0,
                )
                self.launch(thread_id)
            elif action == "stop":
                self.store.update(thread_id, state="STOPPING" if alive else "STOPPED")
            else:
                active = alive and thread_id in self.scopes
                self.store.update(thread_id, state="PAUSING" if active else "PAUSED")
                if text:
                    self.store.enqueue(thread_id, text)
                if active:
                    self.controls[thread_id].put(
                        {"action": "interrupt", "resume": bool(text)}
                    )
                elif text:
                    self.store.update(thread_id, state="STARTING")
                    self.launch(thread_id)
        return self.store.thread(thread_id)

    def run(self, thread_id):
        ThreadRunner(self, thread_id).run()

    def dispatch(self, method, path, values=None):
        values = values or {}
        parsed = urlsplit(path)
        parts = parsed.path.strip("/").split("/")
        if method == "GET" and parts == ["health"]:
            from . import __version__

            return {"ok": True, "version": __version__, "protocol": 1}
        if method == "POST" and parts == ["configuration"]:
            if (
                values["project"] != self.state["project"]
                or values["spec"] != self.state["spec"]
            ):
                raise CaligoError(
                    "Service configuration must match its pinned project revision"
                )
            self.state = values
            self.workflow.state = values
            return {"updated": True}
        if method == "POST" and parts == ["shutdown"]:
            self.close()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return {"stopping": True}
        if parts == ["threads"]:
            if method == "GET":
                return {
                    "threads": self.store.threads(),
                    "capabilities": {
                        key: value.capabilities for key, value in BACKENDS.items()
                    },
                }
            if method == "POST":
                return self.create(
                    values["attempt"],
                    values["backend"],
                    values["goal"],
                    values.get("continuous", True),
                    values.get("model"),
                )
        if len(parts) >= 2 and parts[0] == "threads":
            thread_id = valid_id(parts[1])
            if method == "GET" and len(parts) == 2:
                return dict(
                    self.store.thread(thread_id), cursor=self.store.cursor(thread_id)
                )
            if method == "GET" and parts[2:] == ["events"]:
                after = parse_qs(parsed.query).get("after", ["0"])[0]
                return {"events": self.store.events(thread_id, int(after))}
            if method == "POST" and parts[2:] == ["messages"]:
                return self.message(thread_id, values)
            if parts[2:3] == ["runs"]:
                thread = self.store.thread(thread_id)
                if method == "GET" and len(parts) == 3:
                    return {
                        "runs": [
                            self.workflow.status(run["id"])
                            for run in self.workflow.list(thread["attempt"])["runs"]
                            if run.get("thread") == thread_id
                        ]
                    }
                if len(parts) < 4:
                    raise CaligoError("Unsupported API operation")
                run_id = valid_id(parts[3])
                record = read_json(self.workflow.run_dir(run_id) / "run.json")
                if record.get("thread") != thread_id:
                    raise CaligoError("This run belongs to another thread")
                if method == "GET" and len(parts) == 4:
                    return self.workflow.status(run_id)
                if method == "GET" and parts[4:] == ["logs"]:
                    return self.workflow.logs(run_id)
                if method == "POST" and parts[4:] == ["cancel"]:
                    return self.workflow.cancel(run_id)
            if method == "POST" and len(parts) == 3 and parts[2] in {"run", "exec"}:
                scope = self.scopes.get(thread_id)
                if scope is None:
                    raise CaligoError("Resume the thread before executing commands")
                thread = self.store.thread(thread_id)
                if thread["state"] in {
                    "STOPPED",
                    "STOPPING",
                    "COMPLETING",
                    "COMPLETED",
                    "FAILED",
                }:
                    raise CaligoError(
                        "Resume an active thread before executing commands"
                    )
                if parts[2] == "run":
                    return scope.submit(thread["attempt"], values.get("script"))
                return {
                    "exit_code": scope.exec_command(thread["attempt"], values["argv"])
                }
            if method == "POST" and len(parts) == 3:
                return self.control(thread_id, parts[2], values.get("text"))
        if method == "GET" and len(parts) == 2 and parts[0] == "runs":
            run_id = valid_id(parts[1])
            return {
                "record": read_json(self.workflow.run_dir(run_id) / "run.json"),
                "route": read_json(self.workflow.run_dir(run_id) / "route.json"),
            }
        raise CaligoError("Unsupported API operation")

    def close(self):
        for thread in self.store.threads():
            if thread["state"] not in {
                "STOPPED",
                "STOPPING",
                "COMPLETING",
                "COMPLETED",
                "FAILED",
                "BLOCKED",
            }:
                self.store.update(thread["id"], state="PAUSED")
        # Stop runtimes by signalling worker exit, but retain resumable thread state.
        self.closing = True
        for thread_id, worker in list(self.workers.items()):
            if worker.is_alive():
                self.controls[thread_id].put({"action": "interrupt"})
        # Finish teardown before the daemon exits; otherwise a paused thread's
        # still-running container would outlive an intentional service shutdown.
        for worker in list(self.workers.values()):
            if worker is not threading.current_thread():
                worker.join(timeout=15)


def http_server(service, token):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def authorized(self):
            return hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            )

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            if not self.authorized():
                self.reply(401, {"error": "Unauthorized"})
                return
            try:
                values = None
                if self.command == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024 * 1024:
                        raise CaligoError("Request body must be 1 to 1048576 bytes")
                    values = json.loads(self.rfile.read(length))
                    if not isinstance(values, dict):
                        raise CaligoError("Expected a JSON object")
                if (
                    self.command == "GET"
                    and "/events" in self.path
                    and "text/event-stream" in self.headers.get("Accept", "")
                ):
                    return self.sse()
                self.reply(200, service.dispatch(self.command, self.path, values))
            except (CaligoError, KeyError, ValueError, OSError) as error:
                self.reply(400, {"error": str(error)})

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def sse(self):
            parsed = urlsplit(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "threads" or parts[2] != "events":
                raise CaligoError("Invalid event stream path")
            thread = service.store.thread(valid_id(parts[1]))
            after = int(
                self.headers.get(
                    "Last-Event-ID", parse_qs(parsed.query).get("after", ["0"])[0]
                )
            )
            if after < 0:
                raise CaligoError("Event cursor must be nonnegative")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while not service.closing:
                    events = service.store.events(thread["id"], after)
                    for event in events:
                        self.wfile.write(
                            (
                                "id: %s\ndata: %s\n\n"
                                % (event["seq"], json.dumps(event))
                            ).encode()
                        )
                        after = event["seq"]
                    if not events:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    service.server = server
    return server


def request(controller, method, path, values=None):
    directory = service_directory(controller)
    try:
        address = read_json(directory / "address.json")
        token = (directory / "token").read_text().strip()
        req = Request(
            address["url"] + path,
            data=None if values is None else json.dumps(values).encode(),
            method=method,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
        )
        timeout = 86400 if path.endswith(("/exec", "/run")) else 15
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        raise CaligoError(json.load(error).get("error", str(error))) from error
    except (URLError, FileNotFoundError, ConnectionError) as error:
        raise CaligoError(
            "Agent service unavailable; use caligo service start: " + str(error)
        ) from error


def run_record(controller, run_id):
    record = _run_workflow(controller).run_dir(run_id)
    return {
        "record": read_json(record / "run.json"),
        "route": read_json(record / "route.json"),
    }


def _run_workflow(controller):
    from .workflow import Workflow

    directory = service_directory(controller)
    state = read_json(directory / "config.json")
    return Workflow(state, state.get("client_directory") or directory / "client")


def archive_run(controller, run_id, output):
    _run_workflow(controller).archive(run_id, output)


def start(controller, state):
    directory = service_directory(controller)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with lock(directory / "start.lock"):
        try:
            request(controller, "GET", "/health")
            request(controller, "POST", "/configuration", state)
            write_json(directory / "config.json", state)
            (directory / "config.json").chmod(0o600)
            return {
                "running": True,
                "directory": str(directory),
                **read_json(directory / "address.json"),
            }
        except CaligoError:
            pass
        write_json(directory / "config.json", state)
        (directory / "config.json").chmod(0o600)
        invocation = Path(sys.argv[0]).resolve()
        argv = (
            [sys.executable, str(invocation)]
            if invocation.suffix == ".pyz"
            else [sys.executable, "-m", "caligo"]
        )
        argv += ["_service", str(controller.root), controller.project.name]
        env = dict(os.environ)
        env["PYTHONPATH"] = (
            str(Path(__file__).parent.parent) + os.pathsep + env.get("PYTHONPATH", "")
        )
        with (directory / "service.log").open("ab") as output:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=output,
                start_new_session=True,
                env=env,
            )
        for _ in range(100):
            if process.poll() is not None:
                raise CaligoError(
                    "Agent service failed; inspect " + str(directory / "service.log")
                )
            try:
                request(controller, "GET", "/health")
                return {
                    "running": True,
                    "directory": str(directory),
                    **read_json(directory / "address.json"),
                }
            except CaligoError:
                time.sleep(0.05)
        raise CaligoError(
            "Timed out starting agent service; inspect "
            + str(directory / "service.log")
        )


def main(root, project):
    from .remote import Controller

    controller = Controller(root, project)
    directory = service_directory(controller)
    with lock(directory / "daemon.lock"):
        service = Service(controller, read_json(directory / "config.json"))
        token_path = directory / "token"
        if not token_path.exists():
            token_path.write_text(secrets.token_urlsafe(32))
            token_path.chmod(0o600)
        server = http_server(service, token_path.read_text().strip())
        write_json(
            directory / "address.json",
            {"url": "http://127.0.0.1:" + str(server.server_port), "pid": os.getpid()},
        )

        def shutdown(*args):
            service.close()
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        service.recover()
        try:
            server.serve_forever()
        finally:
            service.close()
            server.server_close()
