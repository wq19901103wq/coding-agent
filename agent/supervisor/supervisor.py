"""Supervisor orchestrator for multi-agent goals."""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from agent.config import Config
from agent.safety import CommandClass, classify_shell_command
from agent.supervisor.ipc import IPCServer
from agent.supervisor.models import Goal, GoalStatus, IPCMessage, MessageType
from agent.supervisor.persistence import GoalPersistence
from agent.supervisor.role_loader import RoleLoader
from agent.tools import ToolContext, ToolResult, get_tool

logger = logging.getLogger("agent.supervisor")

HEARTBEAT_INTERVAL_SECONDS = 5.0
WORKER_TIMEOUT_SECONDS = 60.0


@dataclasses.dataclass
class WorkerHandle:
    goal_id: str
    thread: threading.Thread
    process: subprocess.Popen | None
    last_heartbeat: float = dataclasses.field(default_factory=time.time)


class Supervisor:
    """Manages goals, spawns workers, and handles IPC."""

    def __init__(
        self,
        workspace: str,
        config: Config,
        socket_address: str | None = None,
        db_path: str | None = None,
        spawn_worker: Callable[[str, Goal, Config], subprocess.Popen | None] | None = None,
        confirm_callback: Callable[[str], bool] | None = None,
        goal_completed_callback: Callable[[Goal], None] | None = None,
    ):
        self.workspace = str(Path(workspace).resolve())
        self.config = config
        self.socket_address = socket_address or self._default_socket_path()
        self.db_path = db_path
        self.persistence = GoalPersistence(db_path)
        self.role_loader = RoleLoader()
        self.ipc = IPCServer(self.socket_address)
        self._spawn_worker = spawn_worker or self._default_spawn_worker
        self._confirm_callback = confirm_callback
        self._goal_completed_callback = goal_completed_callback
        self._workers: dict[str, WorkerHandle] = {}
        self._pending_assignments: list[Goal] = []
        self._lock = threading.Lock()
        self._shutdown = False
        self._watchdog_thread: threading.Thread | None = None

    def _default_socket_path(self) -> str:
        return f"/tmp/coding_agent_{uuid.uuid4().hex[:8]}.sock"

    def start(self) -> None:
        self.ipc.set_handler(self._handle_message)
        self.ipc.start()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        logger.info("supervisor started at %s", self.socket_address)

    def stop(self) -> None:
        self._shutdown = True
        self.ipc.stop()
        with self._lock:
            workers = list(self._workers.values())
        for handle in workers:
            self._kill_worker(handle)
        if self._watchdog_thread and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=2.0)

    def submit_goal(
        self,
        title: str,
        description: str,
        agent_role: str,
        parent_id: str | None = None,
        depends_on: list[str] | None = None,
    ) -> Goal:
        goal = Goal(
            id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            agent_role=agent_role,
            parent_id=parent_id,
            depends_on=depends_on or [],
        )
        self.persistence.create(goal)
        return goal

    def run_goal(self, goal_id: str) -> Goal | None:
        goal = self.persistence.get(goal_id)
        if goal is None:
            logger.error("goal %s not found", goal_id)
            return None

        self.persistence.update_status(goal_id, GoalStatus.IN_PROGRESS)
        with self._lock:
            self._pending_assignments.append(goal)
        process = self._spawn_worker(self.socket_address, goal, self.config)
        thread = threading.Thread(
            target=self._worker_monitor,
            args=(goal_id, process),
            daemon=True,
        )
        with self._lock:
            self._workers[goal_id] = WorkerHandle(
                goal_id=goal_id,
                thread=thread,
                process=process,
            )
        thread.start()
        return goal

    def _worker_monitor(self, goal_id: str, process: subprocess.Popen | None) -> None:
        """Monitor a worker subprocess until it exits."""
        if process is None:
            return
        try:
            process.wait(timeout=WORKER_TIMEOUT_SECONDS * 2)
        except subprocess.TimeoutExpired:
            logger.warning("worker for goal %s did not exit in time", goal_id)
            self._kill_worker_by_id(goal_id)
        finally:
            with self._lock:
                self._workers.pop(goal_id, None)
            fetched = self.persistence.get(goal_id)
            if fetched and fetched.status == GoalStatus.IN_PROGRESS:
                self.persistence.update_status(goal_id, GoalStatus.FAILED)

    def _handle_message(self, msg: IPCMessage) -> None:
        if msg.type == MessageType.READY:
            self._handle_ready(msg)
        elif msg.type == MessageType.STATUS_UPDATE:
            self._handle_status_update(msg)
        elif msg.type == MessageType.TOOL_REQUEST:
            self._handle_tool_request(msg)
        elif msg.type == MessageType.COMPLETE:
            self._handle_complete(msg)
        elif msg.type == MessageType.ERROR:
            self._handle_error(msg)
        elif msg.type == MessageType.HEARTBEAT:
            self._handle_heartbeat(msg)
        else:
            logger.debug("ignored message type %s", msg.type)

    def _handle_ready(self, msg: IPCMessage) -> None:
        with self._lock:
            if not self._pending_assignments:
                return
            goal = self._pending_assignments.pop(0)
        self._send_assignment(goal)

    def _send_assignment(self, goal: Goal) -> None:
        try:
            self.ipc.send_to_client(
                IPCMessage(
                    msg_id=str(uuid.uuid4()),
                    goal_id=goal.id,
                    type=MessageType.ASSIGN_GOAL,
                    payload={"goal": goal.model_dump()},
                )
            )
        except Exception:
            logger.exception("failed to send assignment")

    def _handle_status_update(self, msg: IPCMessage) -> None:
        if msg.goal_id is None:
            return
        status = msg.payload.get("status")
        if status == GoalStatus.IN_PROGRESS.value:
            self.persistence.update_status(msg.goal_id, GoalStatus.IN_PROGRESS)
        elif status == GoalStatus.DONE.value:
            self.persistence.update_status(msg.goal_id, GoalStatus.DONE)

    def _handle_tool_request(self, msg: IPCMessage) -> None:
        if msg.goal_id is None:
            return
        goal = self.persistence.get(msg.goal_id)
        tool_call_data = msg.payload.get("tool_call", {})
        from agent.llm.schema import ToolCall

        tool_call = ToolCall(**tool_call_data)
        result = self._execute_tool(tool_call, goal=goal)
        response = IPCMessage(
            msg_id=str(uuid.uuid4()),
            goal_id=msg.goal_id,
            type=MessageType.TOOL_RESULT,
            payload={
                "success": result.success,
                "output": result.output,
                "error": result.error,
                "metadata": result.metadata,
            },
        )
        try:
            self.ipc.send_to_client(response)
        except Exception:
            logger.exception("failed to send tool result")

    def _execute_tool(self, call: Any, goal: Goal | None = None) -> ToolResult:
        role_name = goal.agent_role if goal else "default"
        try:
            role = self.role_loader.get(role_name)
        except KeyError:
            return ToolResult(success=False, error=f"unknown role: {role_name}")

        allowed = role.allowed_tools
        forbidden = set(role.forbidden_tools)
        if allowed is not None and call.name not in allowed:
            return ToolResult(
                success=False,
                error=f"tool '{call.name}' is not allowed for role '{role_name}'",
            )
        if call.name in forbidden:
            return ToolResult(
                success=False,
                error=f"tool '{call.name}' is forbidden for role '{role_name}'",
            )

        if call.name == "execute_shell":
            command = call.arguments.get("command", "")
            classification = classify_shell_command(command)
            if classification == CommandClass.FORBIDDEN:
                return ToolResult(success=False, error="forbidden shell command")
            if classification == CommandClass.DANGEROUS:
                if not self.config.security.confirm_dangerous:
                    pass
                elif self._confirm_callback is not None:
                    prompt = (
                        f"Worker ({role_name}) wants to run dangerous shell command:\n"
                        f"  {command}\n"
                        "Allow? (y/n): "
                    )
                    if not self._confirm_callback(prompt):
                        return ToolResult(
                            success=False,
                            error="user denied dangerous shell command",
                        )
                else:
                    return ToolResult(
                        success=False,
                        error="dangerous shell command requires user confirmation",
                    )

        try:
            tool = get_tool(call.name)
            ctx = ToolContext(workspace=self.workspace)
            return tool.execute(call.arguments, ctx)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

    def _handle_complete(self, msg: IPCMessage) -> None:
        if msg.goal_id is None:
            return
        result = msg.payload.get("result", "")
        self.persistence.update_status(msg.goal_id, GoalStatus.DONE, result_summary=result)
        self._cleanup_worker(msg.goal_id)

    def _handle_error(self, msg: IPCMessage) -> None:
        if msg.goal_id is None:
            return
        error = msg.payload.get("error", "")
        self.persistence.append_error(msg.goal_id, error)
        self.persistence.update_status(msg.goal_id, GoalStatus.FAILED)
        self._cleanup_worker(msg.goal_id)

    def _handle_heartbeat(self, msg: IPCMessage) -> None:
        if msg.goal_id is None:
            return
        with self._lock:
            handle = self._workers.get(msg.goal_id)
            if handle:
                handle.last_heartbeat = time.time()

    def _cleanup_worker(self, goal_id: str) -> None:
        with self._lock:
            handle = self._workers.pop(goal_id, None)
        if handle and handle.process and handle.process.poll() is None:
            try:
                handle.process.terminate()
                handle.process.wait(timeout=2.0)
            except Exception:
                logger.exception("failed to terminate worker for goal %s", goal_id)

    def _kill_worker_by_id(self, goal_id: str) -> None:
        with self._lock:
            handle = self._workers.get(goal_id)
        if handle:
            self._kill_worker(handle)

    def _kill_worker(self, handle: WorkerHandle) -> None:
        if handle.process and handle.process.poll() is None:
            try:
                handle.process.kill()
                handle.process.wait(timeout=2.0)
            except Exception:
                logger.exception("failed to kill worker for goal %s", handle.goal_id)
        self.persistence.update_status(handle.goal_id, GoalStatus.FAILED)

    def _watchdog_loop(self) -> None:
        while not self._shutdown:
            time.sleep(HEARTBEAT_INTERVAL_SECONDS)
            now = time.time()
            with self._lock:
                handles = list(self._workers.values())
            for handle in handles:
                if now - handle.last_heartbeat > WORKER_TIMEOUT_SECONDS:
                    logger.warning("worker for goal %s timed out", handle.goal_id)
                    self._kill_worker(handle)
                    with self._lock:
                        self._workers.pop(handle.goal_id, None)

    def _default_spawn_worker(
        self, socket_address: str, goal: Goal, config: Config
    ) -> subprocess.Popen:
        cmd = [
            sys.executable,
            "-m",
            "agent.worker.worker_main",
            "--socket",
            socket_address,
            "--workspace",
            self.workspace,
            "--role",
            goal.agent_role,
        ]
        env = os.environ.copy()
        env["CODING_AGENT_LLM_API_KEY"] = config.llm.api_key or ""
        return subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _build_system_prompt(self, role_name: str | None = None) -> str:
        if role_name:
            role = self.role_loader.get(role_name)
            return role.system_prompt
        return "你是一个命令行 AI 编程助手。"
