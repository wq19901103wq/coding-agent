"""Supervisor orchestrator for multi-agent goals."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from agent.config import Config
from agent.safety import CommandClass, classify_shell_command
from agent.supervisor.ipc import IPCServer
from agent.supervisor.models import Goal, GoalStatus, IPCMessage, MessageType
from agent.supervisor.persistence import GoalPersistence
from agent.supervisor.role_loader import RoleLoader
from agent.tools import ToolContext, get_tool

logger = logging.getLogger("agent.supervisor")


class Supervisor:
    """Manages goals, spawns workers, and handles IPC."""

    def __init__(
        self,
        workspace: str,
        config: Config,
        socket_address: str | None = None,
        db_path: str | None = None,
        spawn_worker: Callable[[str, Goal, Config], None] | None = None,
        confirm_callback: Callable[[str], bool] | None = None,
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
        self._active_worker_thread: threading.Thread | None = None
        self._pending_assignment: Goal | None = None
        self._lock = threading.Lock()
        self._shutdown = False

    def _default_socket_path(self) -> str:
        return f"/tmp/coding_agent_{uuid.uuid4().hex[:8]}.sock"

    def start(self) -> None:
        self.ipc.set_handler(self._handle_message)
        self.ipc.start()
        logger.info("supervisor started at %s", self.socket_address)

    def stop(self) -> None:
        self._shutdown = True
        self.ipc.stop()
        if self._active_worker_thread and self._active_worker_thread.is_alive():
            self._active_worker_thread.join(timeout=2.0)

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
            self._pending_assignment = goal
        self._active_worker_thread = threading.Thread(
            target=self._spawn_worker,
            args=(self.socket_address, goal, self.config),
            daemon=True,
        )
        self._active_worker_thread.start()
        return goal

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
        elif msg.type == MessageType.NEED_CONFIRM:
            self._handle_need_confirm(msg)
        else:
            logger.debug("ignored message type %s", msg.type)

    def _handle_ready(self, msg: IPCMessage) -> None:
        with self._lock:
            goal = self._pending_assignment
            self._pending_assignment = None
        if goal is None:
            return
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

    def _execute_tool(self, call: Any, goal: Goal | None = None) -> Any:
        from agent.tools import ToolResult

        # Role-based tool permission check.
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

        # Safety check for shell commands.
        if call.name == "execute_shell":
            command = call.arguments.get("command", "")
            classification = classify_shell_command(command)
            if classification == CommandClass.FORBIDDEN:
                return ToolResult(success=False, error="forbidden shell command")
            if classification == CommandClass.DANGEROUS:
                if not self.config.security.confirm_dangerous:
                    # YOLO mode: proceed.
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

    def _handle_error(self, msg: IPCMessage) -> None:
        if msg.goal_id is None:
            return
        error = msg.payload.get("error", "")
        self.persistence.append_error(msg.goal_id, error)
        self.persistence.update_status(msg.goal_id, GoalStatus.FAILED)

    def _handle_need_confirm(self, msg: IPCMessage) -> None:
        # Phase 1: auto-approve all confirmations.
        response = IPCMessage(
            msg_id=str(uuid.uuid4()),
            goal_id=msg.goal_id,
            type=MessageType.USER_INPUT,
            payload={"answer": "y"},
        )
        try:
            self.ipc.send_to_client(response)
        except Exception:
            logger.exception("failed to send confirmation")

    def _default_spawn_worker(self, socket_address: str, goal: Goal, config: Config) -> None:
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
        if config.history.db_path:
            cmd.extend(["--config", str(Path(config.history.db_path).parent / "config.toml")])
        env = os.environ.copy()
        env["CODING_AGENT_LLM_API_KEY"] = config.llm.api_key or ""
        subprocess.Popen(cmd, env=env)

    def _build_system_prompt(self, role_name: str | None = None) -> str:
        if role_name:
            role = self.role_loader.get(role_name)
            return role.system_prompt
        return "你是一个命令行 AI 编程助手。"
