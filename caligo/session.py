"""A session-scoped socket exposes run operations for exactly one worktree."""

from contextlib import contextmanager
import json
import base64
from pathlib import Path
import pkgutil
import socketserver
import tempfile
import threading
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import secrets
import socket

from .common import CaligoError, archive_records, read_json


@contextmanager
def session(controller, environment, agent, controls=True):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            try:
                request = json.loads(self.rfile.readline(65536))
                action = request["action"]
                if not controls:
                    raise CaligoError("Run caligo commands from the coding shell")
                if action == "exec":
                    output_lock = threading.Lock()

                    def emit(channel, data):
                        with output_lock:
                            try:
                                self.wfile.write(
                                    json.dumps(
                                        {
                                            "stream": channel,
                                            "data": base64.b64encode(data).decode(),
                                        }
                                    ).encode()
                                    + b"\n"
                                )
                                self.wfile.flush()
                            except (BrokenPipeError, ConnectionResetError):
                                pass

                    result = {
                        "exit_code": controller.exec_command(
                            environment, request["argv"], emit
                        )
                    }
                elif action in {"fork", "delete"}:
                    if getattr(controller, "agent_scope", False):
                        raise CaligoError("An agent is restricted to its own attempt")
                    manager = getattr(controller, "project_manager", None)
                    if manager is None:
                        raise CaligoError(
                            "Attempt management requires a project controller"
                        )
                    if action == "fork":
                        result = manager.fork(request["source"], request["name"])
                    else:
                        if manager.find(request["name"])["id"] == environment:
                            raise CaligoError(
                                "Exit this attempt's shell before deleting it"
                            )
                        result = manager.delete(request["name"])
                elif action in {"switch", "create"}:
                    if getattr(controller, "agent_scope", False):
                        raise CaligoError("An agent is restricted to its own attempt")
                    result = controller.request_switch(
                        request["name"],
                        request.get("mode"),
                        request.get("addon"),
                        request.get("goal"),
                        request.get("backend", "codex"),
                        not request.get("once", False),
                    )
                elif action == "review":
                    review = getattr(controller, "goal_review", None)
                    if review is None:
                        raise CaligoError("Goal reviews require an active agent thread")
                    result = review(request["decision"], request["summary"])
                elif action == "run":
                    result = controller.submit(
                        environment, request.get("script"), agent
                    )
                elif action == "list":
                    result = controller.list_attempts(environment)
                    if getattr(controller, "agent_scope", False):
                        result["attempts"] = [
                            attempt
                            for attempt in result["attempts"]
                            if attempt["current"]
                        ]
                else:
                    path = controller.run_dir(request["run"])
                    if read_json(path / "run.json")["environment"] != environment:
                        raise CaligoError("This run belongs to another worktree")
                    if action == "status":
                        result = controller.status(request["run"])
                    elif action == "cancel":
                        result = controller.cancel(request["run"])
                    elif action == "publish":
                        controller.publish(request["run"])
                        result = controller.status(request["run"], refresh=False)
                    elif action == "logs":
                        if hasattr(controller, "logs"):
                            result = controller.logs(request["run"])
                        else:
                            log = path / "logs.txt"
                            result = {
                                "logs": (
                                    log.read_text(errors="replace")
                                    if log.exists()
                                    else ""
                                )
                            }
                    elif action == "artifacts":
                        self.wfile.write(b'{"ok": true}\n')
                        if hasattr(controller, "archive"):
                            controller.archive(request["run"], self.wfile)
                        else:
                            archive_records(path, self.wfile)
                        return
                    else:
                        raise CaligoError("Unsupported sandbox operation")
                response = {"ok": True, "result": result}
            except (CaligoError, OSError, ValueError, KeyError) as error:
                response = {"ok": False, "error": str(error)}
            self.wfile.write(json.dumps(response).encode() + b"\n")

    # Unix sockets must be on the host's local filesystem, not an NFS home.
    with tempfile.TemporaryDirectory(prefix="caligo-session-", dir="/tmp") as tmp:
        directory = Path(tmp)
        (directory / "bin").mkdir()
        client = directory / "bin/caligo"
        client.write_bytes(pkgutil.get_data("caligo", "sandbox_cli.py"))
        client.chmod(0o755)
        (directory / "bashrc").write_text(
            """export PS1='caligo[${CALIGO_ATTEMPT}] \\w \\$ '
caligo() {
  \"$CALIGO_CLIENT\" \"$@\"
  local result=$?
  if [ \"$result\" -eq 75 ]; then exit 0; fi
  return \"$result\"
}
"""
        )

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        with Server(str(directory / "control.sock"), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            broker = None
            if getattr(controller, "agent_scope", False):
                token = secrets.token_urlsafe(32)
                requests = threading.BoundedSemaphore(8)

                class Bridge(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        if not requests.acquire(blocking=False):
                            self.send_error(429, "Too many active attempt operations")
                            return
                        try:
                            self.proxy()
                        finally:
                            requests.release()

                    def proxy(self):
                        if self.path != "/rpc" or not hmac.compare_digest(
                            self.headers.get("Authorization", ""), "Bearer " + token
                        ):
                            self.send_error(403)
                            return
                        try:
                            length = int(self.headers.get("Content-Length", "0"))
                            if not 0 < length <= 1024 * 1024:
                                raise ValueError("Invalid request size")
                            request = self.rfile.read(length)
                            # Exactly one JSON request; prohibit newline injection.
                            request = json.dumps(json.loads(request)).encode() + b"\n"
                            with socket.socket(socket.AF_UNIX) as connection:
                                connection.connect(str(directory / "control.sock"))
                                connection.sendall(request)
                                self.send_response(200)
                                self.send_header(
                                    "Content-Type", "application/octet-stream"
                                )
                                self.end_headers()
                                while chunk := connection.recv(65536):
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                        except (OSError, ValueError):
                            self.close_connection = True

                broker = ThreadingHTTPServer(("0.0.0.0", 0), Bridge)
                broker.daemon_threads = True
                from .common import write_json

                write_json(
                    directory / "broker.json",
                    {
                        "url": "http://host.docker.internal:"
                        + str(broker.server_port)
                        + "/rpc",
                        "token": token,
                    },
                )
                broker_thread = threading.Thread(
                    target=broker.serve_forever, daemon=True
                )
                broker_thread.start()
            try:
                yield directory
            finally:
                if broker:
                    broker.shutdown()
                    broker.server_close()
                    broker_thread.join()
                server.shutdown()
                thread.join()
