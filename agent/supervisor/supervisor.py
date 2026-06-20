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
    timeout_seconds: float = WORKER_TIMEOUT_SECONDS


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
        self._client_assignments: dict[str, str] = {}  # client_id -> goal_id
        self._goal_clients: dict[str, str] = {}  # goal_id -> client_id
        self._lock = threading.Lock()
        self._shutdown = False
        self._watchdog_thread: threading.Thread | None = None
        self.heartbeat_interval_seconds = HEARTBEAT_INTERVAL_SECONDS
        self.worker_timeout_seconds = WORKER_TIMEOUT_SECONDS

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
        timeout_seconds: float | None = None,
    ) -> Goal:
        goal = Goal(
            id=str(uuid.uuid4())[:8],
            title=title,
            description=description,
            agent_role=agent_role,
            parent_id=parent_id,
            depends_on=depends_on or [],
            timeout_seconds=timeout_seconds,
        )
        self.persistence.create(goal)
        return goal

    def run_goal(self, goal_id: str) -> Goal | None:
        goal = self.persistence.get(goal_id)
        if goal is None:
            logger.error("goal %s not found", goal_id)
            return None
        if goal.status in (GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.CANCELLED):
            logger.warning("goal %s is already in terminal state %s", goal_id, goal.status.value)
            return goal

        self.persistence.update_status(goal_id, GoalStatus.IN_PROGRESS)
        with self._lock:
            self._pending_assignments.append(goal)
        process = self._spawn_worker(self.socket_address, goal, self.config)
        timeout = goal.timeout_seconds or self.worker_timeout_seconds
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
                timeout_seconds=timeout,
            )
        thread.start()
        return goal

    def cancel_goal(self, goal_id: str) -> bool:
        """Cancel a goal and terminate its worker if it is still running."""
        goal = self.persistence.get(goal_id)
        if goal is None:
            return False
        if goal.status in (GoalStatus.DONE, GoalStatus.FAILED, GoalStatus.CANCELLED):
            return True
        self.persistence.update_status(goal_id, GoalStatus.CANCELLED)
        self._cleanup_worker(goal_id)
        return True

    def _worker_monitor(self, goal_id: str, process: subprocess.Popen | None) -> None:
        """Monitor a worker subprocess until it exits."""
        if process is None:
            return
        with self._lock:
            handle = self._workers.get(goal_id)
        timeout = self.worker_timeout_seconds * 2
        if handle is not None:
            timeout = max(timeout, handle.timeout_seconds * 2)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("worker for goal %s did not exit in time", goal_id)
            self._kill_worker_by_id(goal_id)
        finally:
            with self._lock:
                self._workers.pop(goal_id, None)
                self._goal_clients.pop(goal_id, None)
            fetched = self.persistence.get(goal_id)
            if fetched and fetched.status == GoalStatus.IN_PROGRESS:
                self.persistence.update_status(goal_id, GoalStatus.FAILED)

    def _handle_message(self, msg: IPCMessage, client_id: str) -> None:
        if msg.type == MessageType.READY:
            self._handle_ready(msg, client_id)
        elif msg.type == MessageType.STATUS_UPDATE:
            self._handle_status_update(msg)
        elif msg.type == MessageType.TOOL_REQUEST:
            self._handle_tool_request(msg, client_id)
        elif msg.type == MessageType.COMPLETE:
            self._handle_complete(msg, client_id)
        elif msg.type == MessageType.ERROR:
            self._handle_error(msg, client_id)
        elif msg.type == MessageType.HEARTBEAT:
            self._handle_heartbeat(msg, client_id)
        else:
            logger.debug("ignored message type %s", msg.type)

    def _goal_id_for(self, msg: IPCMessage, client_id: str) -> str | None:
        if msg.goal_id is not None:
            return msg.goal_id
        with self._lock:
            return self._client_assignments.get(client_id)

    def _handle_ready(self, msg: IPCMessage, client_id: str) -> None:
        worker_role = msg.payload.get("role", "default")
        with self._lock:
            matching_idx: int | None = None
            for i, goal in enumerate(self._pending_assignments):
                if goal.agent_role == worker_role:
                    matching_idx = i
                    break
            if matching_idx is None:
                logger.debug("no pending goal for role %s", worker_role)
                return
            goal = self._pending_assignments[matching_idx]
        try:
            self._send_assignment(goal, client_id)
        except Exception:
            logger.exception("failed to assign goal %s to client %s", goal.id, client_id)
            return
        with self._lock:
            try:
                self._pending_assignments.pop(matching_idx)
            except IndexError:
                pass
            self._client_assignments[client_id] = goal.id
            self._goal_clients[goal.id] = client_id

    def _send_assignment(self, goal: Goal, client_id: str) -> None:
        self.ipc.send_to_client(
            IPCMessage(
                msg_id=str(uuid.uuid4()),
                goal_id=goal.id,
                type=MessageType.ASSIGN_GOAL,
                payload={"goal": goal.model_dump()},
            ),
            client_id=client_id,
        )

    def _handle_status_update(self, msg: IPCMessage) -> None:
        if msg.goal_id is None:
            return
        status = msg.payload.get("status")
        if status == GoalStatus.IN_PROGRESS.value:
            self.persistence.update_status(msg.goal_id, GoalStatus.IN_PROGRESS)
        elif status == GoalStatus.DONE.value:
            self.persistence.update_status(msg.goal_id, GoalStatus.DONE)

    def _handle_tool_request(self, msg: IPCMessage, client_id: str) -> None:
        goal_id = self._goal_id_for(msg, client_id)
        if goal_id is None:
            logger.warning("tool request from client %s has no goal_id", client_id)
            return
        goal = self.persistence.get(goal_id)
        if goal is None:
            logger.warning("tool request for unknown goal %s", goal_id)
            return
        tool_call_data = msg.payload.get("tool_call", {})
        from agent.llm.schema import ToolCall

        tool_call = ToolCall(**tool_call_data)
        logger.info(
            "goal %s executing tool %s(args=%s) for client %s",
            goal_id,
            tool_call.name,
            tool_call.arguments,
            client_id,
        )
        result = self._execute_tool(tool_call, goal=goal)
        logger.info(
            "goal %s tool %s result: success=%s output_len=%s error=%s",
            goal_id,
            tool_call.name,
            result.success,
            len(result.output or ""),
            result.error,
        )
        response = IPCMessage(
            msg_id=str(uuid.uuid4()),
            goal_id=goal_id,
            type=MessageType.TOOL_RESULT,
            payload={
                "success": result.success,
                "output": result.output,
                "error": result.error,
                "metadata": result.metadata,
            },
        )
        try:
            self.ipc.send_to_client(response, client_id=client_id)
        except Exception:
            logger.exception("failed to send tool result to client %s", client_id)

    def _execute_tool(self, call: Any, goal: Goal | None = None) -> ToolResult:
        if goal is None:
            return ToolResult(success=False, error="no goal context for tool execution")
        role_name = goal.agent_role
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

        try:
            tool = get_tool(call.name)
            ctx = ToolContext(workspace=self.workspace)
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))

        if call.name == "execute_shell":
            command = call.arguments.get("command", "")
            logger.info(
                "classifying shell command for goal %s: %s", goal.id if goal else None, command
            )
            classification = classify_shell_command(command)
            logger.info("shell command classification: %s", classification.name)
            if classification == CommandClass.FORBIDDEN:
                return ToolResult(success=False, error="forbidden shell command")
            if classification == CommandClass.DANGEROUS:
                if not self.config.security.confirm_dangerous:
                    # YOLO mode: execute without asking.
                    return tool.execute_forced(call.arguments, ctx)
                if self._confirm_callback is not None:
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
                    return tool.execute_forced(call.arguments, ctx)
                return ToolResult(
                    success=False,
                    error="dangerous shell command requires user confirmation",
                )
            return tool.execute(call.arguments, ctx)

        return tool.execute(call.arguments, ctx)

    def _handle_complete(self, msg: IPCMessage, client_id: str) -> None:
        goal_id = self._goal_id_for(msg, client_id)
        if goal_id is None:
            return
        result = msg.payload.get("result", "")
        self.persistence.update_status(goal_id, GoalStatus.DONE, result_summary=result)
        self._cleanup_worker(goal_id)
        if self._goal_completed_callback:
            try:
                goal = self.persistence.get(goal_id)
                if goal is not None:
                    self._goal_completed_callback(goal)
            except Exception:
                logger.exception("goal completed callback failed")

    def _handle_error(self, msg: IPCMessage, client_id: str) -> None:
        goal_id = self._goal_id_for(msg, client_id)
        if goal_id is None:
            return
        error = msg.payload.get("error", "")
        self.persistence.append_error(goal_id, error)
        self.persistence.update_status(goal_id, GoalStatus.FAILED)
        self._cleanup_worker(goal_id)

    def _handle_heartbeat(self, msg: IPCMessage, client_id: str) -> None:
        goal_id = msg.goal_id
        if goal_id is None:
            goal_id = self._client_assignments.get(client_id)
        if goal_id is None:
            return
        with self._lock:
            handle = self._workers.get(goal_id)
            if handle:
                handle.last_heartbeat = time.time()

    def _cleanup_worker(self, goal_id: str) -> None:
        with self._lock:
            handle = self._workers.pop(goal_id, None)
            client_id = self._goal_clients.pop(goal_id, None)
            if client_id is not None:
                self._client_assignments.pop(client_id, None)
        if handle and handle.process and handle.process.poll() is None:
            try:
                handle.process.terminate()
                handle.process.wait(timeout=2.0)
            except Exception:
                logger.exception("failed to terminate worker for goal %s", goal_id)
        if client_id is not None:
            self.ipc._cleanup_client(client_id)

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

    def _watchdog_loop(self) -> None:
        while not self._shutdown:
            time.sleep(self.heartbeat_interval_seconds)
            now = time.time()
            with self._lock:
                handles = list(self._workers.values())
            for handle in handles:
                if now - handle.last_heartbeat > handle.timeout_seconds:
                    logger.warning("worker for goal %s timed out", handle.goal_id)
                    self._kill_worker(handle)
                    with self._lock:
                        self._workers.pop(handle.goal_id, None)
                        client_id = self._goal_clients.pop(handle.goal_id, None)
                        if client_id is not None:
                            self._client_assignments.pop(client_id, None)
                    fetched = self.persistence.get(handle.goal_id)
                    if fetched and fetched.status == GoalStatus.IN_PROGRESS:
                        self.persistence.update_status(handle.goal_id, GoalStatus.FAILED)

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
        env["PYTHONUNBUFFERED"] = "1"

        log_dir = Path.home() / ".coding-agent" / "workers"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{goal.id}.log"
        log_file = log_path.open("a", encoding="utf-8")

        config_json = config.model_dump_json()
        log_dir = Path.home() / ".coding-agent" / "workers"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{goal.id}.log"
        log_file = log_path.open("a", encoding="utf-8")

        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            text=True,
        )

        def _forward_worker_output() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                log_file.write(line)
                log_file.flush()

        threading.Thread(target=_forward_worker_output, daemon=True).start()

        if proc.stdin is not None:
            try:
                proc.stdin.write(config_json)
                proc.stdin.write("\n")
                proc.stdin.close()
            except OSError:
                logger.exception("failed to send config to worker for goal %s", goal.id)
        return proc

    def _build_system_prompt(self, role_name: str | None = None) -> str:
        if role_name:
            role = self.role_loader.get(role_name)
            return role.system_prompt
        return "你是一个命令行 AI 编程助手。"
