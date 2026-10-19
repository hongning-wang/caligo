"""Native agent backends. This module runs only inside the agent container."""

import asyncio
import dataclasses
import json
import os
from pathlib import Path
import sys
import subprocess


def retryable(error):
    text = str(error).lower()
    return isinstance(error, (ConnectionError, TimeoutError)) or any(
        token in text
        for token in (
            "connection reset",
            "temporarily unavailable",
            "connection refused",
            "timed out",
        )
    )


def instructions():
    workspace = Path(os.environ.get("CALIGO_WORKSPACE", os.getcwd()))
    guide = workspace / "Agent.MD"
    context = (
        guide.read_text()
        if guide.exists()
        else (
            "Editable folders: "
            + os.environ.get("CALIGO_EDITABLE", "[]")
            + "; read-only inputs: "
            + os.environ.get("CALIGO_READABLE", "[]")
            + ". Respect existing file permissions. Ordinary shell commands run in your coding container. "
            "Use `caligo exec -- COMMAND` for development checks on the configured dev host, "
            "and `caligo run [executable/script.py]` to submit a frozen full experiment. "
            "Use `caligo status RUN_ID`, `caligo logs RUN_ID`, and `caligo artifacts RUN_ID DEST` "
            "to inspect results. Evaluation results are under evaluation/ and participant files "
            "under artifacts/. The evaluator is supplied only to a separate evaluation stage. "
            "Do not attempt to change project configuration, create other attempts, or modify read-only inputs. "
            "Save checkpoints regularly in `$CALIGO_OUTPUTS`; reload them after restart/requeue. "
        )
    )
    return (
        f"You are working on a Caligo experiment attempt. Your persistent workspace is {workspace}.\n"
        + context
        + "\n"
        "Experiments continue independently of your conversation turn; inspect existing jobs before submitting more. "
        "Before ending each turn, review the objective against the actual changes, checks, and experiment results. "
        "Call `caligo review complete 'evidence that the objective is met'` when all requested work is verified. "
        "Otherwise call `caligo review continue 'specific useful next action'`, "
        "`caligo review wait 'experiment results needed'` when experiments are still running, or "
        "`caligo review blocked 'what you need from the user'` when you cannot make useful progress. "
        "Inspect finished experiment logs and artifacts before deciding. Do not invent further work once the objective is met. "
        "Make the review your last action; if you change anything afterward, review again."
    )


class Codex:
    capabilities = {"steer": True, "interrupt": True, "resume": True}

    def __init__(self, emit):
        self.emit, self.pending, self.counter, self.turn = emit, {}, 0, None
        self.turn_ended = asyncio.Event()

    async def connect(self, backend_id=None, model=None):
        self.process = await asyncio.create_subprocess_exec(
            "codex",
            "app-server",
            "--listen",
            "stdio://",
            "-c",
            'approval_policy="never"',
            "-c",
            'sandbox_mode="danger-full-access"',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self.read())
        await self.rpc(
            "initialize",
            {"clientInfo": {"name": "caligo", "title": "Caligo", "version": "0.2.0"}},
        )
        await self.write({"method": "initialized"})
        if os.environ.get("OPENAI_API_KEY"):
            await self.rpc(
                "account/login/start",
                {"type": "apiKey", "apiKey": os.environ["OPENAI_API_KEY"]},
            )
        options = {
            "cwd": os.getcwd(),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "developerInstructions": instructions(),
        }
        if model:
            options["model"] = model
        if backend_id:
            options["threadId"] = backend_id
        result = await self.rpc(
            "thread/resume" if backend_id else "thread/start", options
        )
        self.backend_id = result["thread"]["id"]
        return self.backend_id

    async def write(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.counter += 1
        request = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request] = future
        await self.write({"id": request, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, 90)
        finally:
            self.pending.pop(request, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" not in message and message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(str(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                    continue
                self.emit("backend.event", message)
                method, params = message.get("method"), message.get("params", {})
                if "id" in message and method:
                    # No implicit approvals or silent requests for human input.
                    self.emit("agent.blocked", {"request": message})
                    await self.write(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32000,
                                "message": "Interactive requests require external steering",
                            },
                        }
                    )
                elif method == "turn/started":
                    self.turn = params["turn"]["id"]
                    self.turn_ended.clear()
                    self.emit("turn.started", {"id": self.turn})
                elif method == "turn/completed":
                    self.turn = None
                    self.turn_ended.set()
                    self.emit("turn.completed", params["turn"])
                elif method == "item/agentMessage/delta":
                    self.emit("agent.text", {"text": params.get("delta", "")})
                elif method == "item/commandExecution/outputDelta":
                    self.emit("tool.output", params)
        except Exception as error:
            self.emit("agent.error", {"error": str(error)})
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex app-server disconnected"))
            self.emit("agent.disconnected", {"retryable": True})

    async def send(self, text, steer=False):
        params = {
            "threadId": self.backend_id,
            "input": [{"type": "text", "text": text}],
        }
        if steer:
            if self.turn is None:
                raise RuntimeError("No active turn to steer")
            params["expectedTurnId"] = self.turn
        await self.rpc("turn/steer" if steer else "turn/start", params)

    async def interrupt(self):
        if self.turn:
            await self.rpc(
                "turn/interrupt", {"threadId": self.backend_id, "turnId": self.turn}
            )
            await asyncio.wait_for(self.turn_ended.wait(), 30)

    async def close(self):
        if self.process.returncode is None:
            self.process.terminate()
        await self.process.wait()
        await self.reader


class Claude:
    # Queue and interrupt are supported. Do not claim Codex-style mid-turn steer.
    capabilities = {"steer": False, "interrupt": True, "resume": True}

    def __init__(self, emit):
        self.emit, self.response, self.backend_id = emit, None, None

    async def connect(self, backend_id=None, model=None):
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        options = ClaudeAgentOptions(
            cwd=os.getcwd(),
            permission_mode="bypassPermissions",
            resume=backend_id,
            model=model,
            include_partial_messages=True,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": instructions(),
            },
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()
        self.backend_id = backend_id
        return backend_id

    async def send(self, text, steer=False):
        if steer:
            raise RuntimeError(
                "Claude adapter supports queue and interrupt, not active steering"
            )
        await self.client.query(text)
        self.emit("turn.started", {})
        self.response = asyncio.create_task(self.receive())

    async def receive(self):
        from claude_agent_sdk import ResultMessage, SystemMessage, StreamEvent

        try:
            async for message in self.client.receive_response():
                payload = (
                    dataclasses.asdict(message)
                    if dataclasses.is_dataclass(message)
                    else str(message)
                )
                self.emit(
                    "backend.event",
                    {"type": type(message).__name__, "message": payload},
                )
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    self.backend_id = message.data["session_id"]
                    self.emit("backend.identity", {"id": self.backend_id})
                if isinstance(message, StreamEvent):
                    delta = message.event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        self.emit("agent.text", {"text": delta.get("text", "")})
                if isinstance(message, ResultMessage):
                    self.backend_id = message.session_id
                    self.emit("backend.identity", {"id": self.backend_id})
                    reason = getattr(message, "terminal_reason", "") or ""
                    status = (
                        "interrupted"
                        if reason.startswith("aborted_")
                        else ("failed" if message.is_error else "completed")
                    )
                    self.emit("turn.completed", {"status": status, "result": payload})
        except Exception as error:
            self.emit(
                "agent.error", {"error": str(error), "retryable": retryable(error)}
            )

    async def interrupt(self):
        await self.client.interrupt()
        # Drain the interrupted ResultMessage before accepting the next query.
        if self.response:
            await self.response

    async def close(self):
        if self.response and not self.response.done():
            await self.interrupt()
        await self.client.disconnect()


BACKENDS = {"codex": Codex, "claude": Claude}


async def worker(backend, backend_id=None, model=None):
    def emit(kind, data):
        print(json.dumps({"type": kind, "data": data}, default=str), flush=True)

    agent = BACKENDS[backend](emit)
    try:
        # This executes inside the container and touches only its private home.
        for key, value in [
            ("user.name", "Caligo agent"),
            ("user.email", "caligo-agent@localhost"),
        ]:
            if (
                subprocess.run(
                    ["git", "config", "--global", "--get", key], capture_output=True
                ).returncode
                == 1
            ):
                subprocess.run(["git", "config", "--global", key, value], check=True)
        identity = await agent.connect(backend_id, model)
        emit(
            "agent.ready", {"backend_id": identity, "capabilities": agent.capabilities}
        )
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            try:
                if command["action"] == "input":
                    await agent.send(
                        command["text"], command.get("delivery") == "steer"
                    )
                elif command["action"] == "interrupt":
                    await agent.interrupt()
                elif command["action"] == "stop":
                    break
                else:
                    raise ValueError("Unsupported worker command")
                emit("command.ack", {"id": command["id"]})
            except Exception as error:
                emit("command.failed", {"id": command.get("id"), "error": str(error)})
    except Exception as error:
        emit("agent.error", {"error": str(error), "retryable": retryable(error)})
        return 1
    finally:
        if hasattr(agent, "process") or hasattr(agent, "client"):
            await agent.close()
    return 0
