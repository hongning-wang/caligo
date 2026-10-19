"""Local browser interface for a project's existing agent services."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import mimetypes
from pathlib import Path
import shutil
import tempfile
import threading
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser

from .common import CaligoError, valid_id
from .manifest import load


class Application:
    def __init__(self, project):
        self.project = project
        self.workflows = {}

    def agents(self):
        state = (
            self.project.read()
            if self.project.state_path.exists()
            else {"spec": load(self.project.manifest), "attempts": []}
        )
        services, agents, capabilities = {}, [], {}
        for attempt in state["attempts"]:
            if attempt["mode"] != "agent":
                continue
            key = attempt.get("controller_project", state["project"])
            if key not in services:
                try:
                    services[key] = self.project.service(attempt["name"])
                except (CaligoError, OSError) as error:
                    services[key] = {"threads": [], "error": str(error)}
            service = services[key]
            capabilities.update(service.get("capabilities", {}))
            agents.append(
                {
                    "id": attempt["id"],
                    "name": attempt["name"],
                    "threads": [
                        t for t in service["threads"] if t["attempt"] == attempt["id"]
                    ],
                    "error": service.get("error"),
                }
            )
        return {
            "project": state["spec"]["name"],
            "agents": agents,
            "capabilities": capabilities,
            "default_model": state["spec"].get("agent", {}).get("model"),
        }

    def create(self, values):
        goal = values["goal"]
        if not isinstance(goal, str) or not goal.strip():
            raise CaligoError("Enter an objective")
        backend = values.get("backend", "codex")
        if backend not in {"codex", "claude"}:
            raise CaligoError("Choose Codex or Claude")
        continuous = values.get("continuous", True)
        if type(continuous) is not bool:
            raise CaligoError("Work until done must be true or false")
        if values.get("attempt"):
            attempt = self.project.find(values["attempt"])
        else:
            attempt = self.project.create(values["name"], "agent")
        thread = self.project.start_agent(
            attempt["name"],
            backend,
            goal,
            continuous,
            model=values.get("model") or None,
        )
        return {"attempt": attempt["id"], "thread": thread}

    def thread_request(self, attempt_id, thread_id, method, suffix="", values=None):
        attempt = self.project.find(valid_id(attempt_id))
        path = "/threads/" + valid_id(thread_id)
        return self.project.service(attempt["name"], method, path + suffix, values)

    def download(self, attempt_id, run_id, output):
        if attempt_id not in self.workflows:
            self.workflows[attempt_id] = self.project.workflow(attempt_id)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "run.tar.gz"
            self.workflows[attempt_id].coding.download(
                "service-archive", valid_id(run_id), archive
            )
            with archive.open("rb") as source:
                shutil.copyfileobj(source, output)


def create_server(project, port=0):
    app = Application(project)
    assets = Path(__file__).resolve().parent.parent / "ui" / "out"
    if not assets.parent.is_dir():
        assets = files("caligo.web").joinpath("out")
    if not assets.joinpath("index.html").is_file():
        raise CaligoError(
            "Build the UI first: pnpm --dir ui install && pnpm --dir ui build"
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            self.send_bytes(json.dumps(value).encode(), "application/json", status)

        def send_bytes(self, data, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            try:
                parsed = urlsplit(self.path)
                if self.command == "GET" and not parsed.path.startswith("/api/"):
                    name = unquote(parsed.path).lstrip("/") or "index.html"
                    if ".." in Path(name).parts or "\\" in name:
                        self.reply({"error": "Not found"}, 404)
                        return
                    asset = assets.joinpath(name)
                    if not asset.is_file():
                        self.reply({"error": "Not found"}, 404)
                        return
                    kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    self.send_bytes(asset.read_bytes(), kind)
                    return
                values = None
                if self.command == "POST":
                    origin = self.headers.get("Origin")
                    if origin and origin != "http://" + self.headers["Host"]:
                        raise CaligoError("Open the UI using its local URL")
                    values = json.loads(
                        self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    )
                parts = parsed.path.strip("/").split("/")
                if parts == ["api", "agents"]:
                    self.reply(
                        app.agents() if self.command == "GET" else app.create(values)
                    )
                    return
                if (
                    len(parts) >= 5
                    and parts[:2] == ["api", "agents"]
                    and parts[3] == "threads"
                ):
                    attempt_id, thread_id = valid_id(parts[2]), valid_id(parts[4])
                    if self.command == "GET" and parts[5:] == ["events"]:
                        after = int(
                            self.headers.get("Last-Event-ID")
                            or parse_qs(parsed.query).get("after", ["0"])[0]
                        )
                        self.events(attempt_id, thread_id, after)
                        return
                    if (
                        self.command == "GET"
                        and len(parts) == 8
                        and parts[5] == "runs"
                        and parts[7] == "download"
                    ):
                        run_id = valid_id(parts[6])
                        app.thread_request(
                            attempt_id, thread_id, "GET", "/runs/" + run_id
                        )
                        with tempfile.TemporaryFile() as archive:
                            app.download(attempt_id, run_id, archive)
                            length = archive.tell()
                            archive.seek(0)
                            self.send_response(200)
                            self.send_header("Content-Type", "application/gzip")
                            self.send_header(
                                "Content-Disposition",
                                'attachment; filename="%s.tar.gz"' % run_id,
                            )
                            self.send_header("Content-Length", str(length))
                            self.end_headers()
                            shutil.copyfileobj(archive, self.wfile)
                        return
                    suffix = "/" + "/".join(parts[5:]) if len(parts) > 5 else ""
                    self.reply(
                        app.thread_request(
                            attempt_id, thread_id, self.command, suffix, values
                        )
                    )
                    return
                self.reply({"error": "Not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except (CaligoError, OSError, ValueError, KeyError, TypeError) as error:
                self.reply({"error": str(error)}, 400)

        def events(self, attempt_id, thread_id, after):
            # Fetch before sending headers so connection errors remain ordinary API errors.
            result = app.thread_request(
                attempt_id, thread_id, "GET", "/events?after=" + str(after)
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while not self.server.closing.is_set():
                    for event in result["events"]:
                        self.wfile.write(
                            (
                                "id: %s\ndata: %s\n\n"
                                % (event["seq"], json.dumps(event))
                            ).encode()
                        )
                        after = event["seq"]
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    if len(result["events"]) < 500 and self.server.closing.wait(1):
                        return
                    result = app.thread_request(
                        attempt_id, thread_id, "GET", "/events?after=" + str(after)
                    )
            except (BrokenPipeError, ConnectionResetError, CaligoError, OSError):
                pass  # EventSource reconnects with its last received sequence ID.

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.closing = threading.Event()
    return server


def serve(project, port=0, open_browser=True):
    server = create_server(project, port)
    url = "http://127.0.0.1:%d" % server.server_port
    print(url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.closing.set()
        server.server_close()
