"""A session-scoped socket exposes run operations for exactly one worktree."""
from contextlib import contextmanager
import json
import base64
from pathlib import Path
import pkgutil
import socketserver
import tarfile
import tempfile
import threading

from .common import CaligoError, inventory, read_json


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
                            self.wfile.write(json.dumps({"stream": channel, "data": base64.b64encode(data).decode()}).encode() + b"\n")
                            self.wfile.flush()
                    result = {"exit_code": controller.exec_command(environment, request["argv"], emit)}
                elif action in {"switch", "create"}:
                    result = controller.request_switch(request["name"], request.get("mode"), request.get("addon"))
                elif action == "run":
                    result = controller.submit(environment, request.get("script"), agent)
                elif action == "list":
                    result = controller.list_attempts(environment)
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
                            result = {"logs": log.read_text(errors="replace") if log.exists() else ""}
                    elif action == "artifacts":
                        inventory(path)
                        self.wfile.write(b'{"ok": true}\n')
                        if hasattr(controller, "archive"):
                            controller.archive(request["run"], self.wfile)
                        else:
                            with tarfile.open(fileobj=self.wfile, mode="w|gz") as tar:
                                tar.add(path, arcname=path.name)
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
        (directory / "bashrc").write_text("""export PS1='caligo[${CALIGO_ATTEMPT}] \\w \\$ '
caligo() {
  \"$CALIGO_CLIENT\" \"$@\"
  local result=$?
  if [ \"$result\" -eq 75 ]; then exit 0; fi
  return \"$result\"
}
""")
        with socketserver.UnixStreamServer(str(directory / "control.sock"), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                yield directory
            finally:
                server.shutdown()
                thread.join()
