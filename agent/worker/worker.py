"""Worker agent that runs in a separate process and executes a single goal."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable

from agent.llm import LLMClient, Message, ToolCall, build_tools_payload
from agent.llm.schema import AssistantResponse
from agent.supervisor.ipc import IPCClient, IPCError
from agent.supervisor.models import (
    AgentRole,
    Goal,
    GoalStatus,
    IPCMessage,
    MessageType,
)
from agent.supervisor.role_loader import RoleLoader
from agent.tools import ToolResult

logger = logging.getLogger("agent.worker")


class Worker:
    """A worker process that executes one goal under a specific role."""

    def __init__(
        self,
        socket_address: str,
        workspace: str,
        llm_client: LLMClient,
        role: AgentRole,
        input_func: Callable[[str], str] | None = None,
    ):
        self.socket_address = socket_address
        self.workspace = workspace
        self.llm = llm_client
        self.role = role
        self.input_func = input_func
        self.ipc = IPCClient(socket_address)
        self.goal: Goal | None = None

    @classmethod
    def from_role_name(
        cls,
        socket_address: str,
        workspace: str,
        llm_client: LLMClient,
        role_name: str,
        roles_dir: str | None = None,
    ) -> Worker:
        loader = RoleLoader(roles_dir)
        role = loader.get(role_name)
        return cls(socket_address, workspace, llm_client, role)

    def run(self) -> None:
        """Connect to supervisor, wait for a goal, and execute it."""
        self.ipc.connect()
        logger.info("worker connected to supervisor at %s", self.socket_address)

        # Wait for ASSIGN_GOAL.
        assign_msg = self._wait_for(MessageType.ASSIGN_GOAL)
        if assign_msg is None:
            logger.error("worker did not receive assignment")
            return

        self.goal = Goal(**assign_msg.payload["goal"])
        logger.info("worker received goal %s", self.goal.id)

        self._send_status(GoalStatus.IN_PROGRESS)

        try:
            result = self._execute_goal()
            self._send_complete(result)
        except Exception as exc:
            logger.exception("goal execution failed")
            self._send_error(str(exc))
        finally:
            self.ipc.close()

    def _execute_goal(self) -> str:
        """Run the LLM agent loop for the assigned goal."""
        messages: list[Message] = [
            Message(role="system", content=self._build_system_prompt()),
            Message(role="user", content=self._build_user_prompt()),
        ]

        tools_schema = self._build_tools_schema()
        max_steps = self.role.max_steps_per_turn or self.llm.config.max_steps_per_turn

        for _step in range(max_steps):
            response = self.llm.chat(messages, tools=tools_schema)
            messages.append(self._assistant_message(response))

            if not response.tool_calls:
                return response.content or ""

            for call in response.tool_calls:
                if call.name == "ask_user" and self.input_func:
                    result = self._handle_ask_user(call)
                else:
                    result = self._request_tool_execution(call)
                messages.append(
                    Message(
                        role="tool",
                        content=_format_tool_result(result),
                        tool_call_id=call.id,
                    )
                )

        return "Reached maximum steps without final answer."

    def _build_system_prompt(self) -> str:
        base = self.role.system_prompt
        return (
            f"{base}\n\n"
            f"当前工作目录：{self.workspace}\n"
            f"你的角色：{self.role.name}\n"
            f"你被允许使用的工具：{self._allowed_tool_names()}\n"
        )

    def _build_user_prompt(self) -> str:
        if self.goal is None:
            return ""
        return f"目标：{self.goal.title}\n描述：{self.goal.description}\n请使用工具完成该目标。"

    def _build_tools_schema(self) -> list[dict[str, Any]]:
        from agent.tools import TOOL_REGISTRY

        allowed = self._allowed_tool_names()
        tools = [tool for name, tool in TOOL_REGISTRY.items() if name in allowed]
        return build_tools_payload(tools)

    def _allowed_tool_names(self) -> set[str]:
        from agent.tools import TOOL_REGISTRY

        all_tools = set(TOOL_REGISTRY.keys())
        if self.role.allowed_tools is not None:
            names = set(self.role.allowed_tools)
        else:
            names = all_tools
        names -= set(self.role.forbidden_tools)
        return names

    def _handle_ask_user(self, call: ToolCall) -> ToolResult:
        question = call.arguments.get("question", "")
        options = call.arguments.get("options")
        if options:
            prompt = f"{question}\n选项：{', '.join(options)}\n请输入："
        else:
            prompt = f"{question}\n请输入："
        answer = self.input_func(prompt) if self.input_func else ""
        return ToolResult(success=True, output=answer)

    def _request_tool_execution(self, call: ToolCall) -> ToolResult:
        request = IPCMessage(
            msg_id=str(uuid.uuid4()),
            goal_id=self.goal.id if self.goal else None,
            type=MessageType.TOOL_REQUEST,
            payload={
                "tool_call": call.model_dump(),
            },
        )
        self.ipc.send(request)
        response = self._wait_for(MessageType.TOOL_RESULT)
        if response is None:
            return ToolResult(success=False, error="no response from supervisor")
        payload = response.payload
        return ToolResult(
            success=payload.get("success", False),
            output=payload.get("output"),
            error=payload.get("error"),
            metadata=payload.get("metadata"),
        )

    def _wait_for(self, msg_type: MessageType, timeout: float = 30.0) -> IPCMessage | None:
        try:
            while True:
                msg = self.ipc.receive(timeout=timeout)
                if msg is None:
                    return None
                if msg.type == msg_type:
                    return msg
                # Ignore unexpected messages in phase 1.
                logger.debug("unexpected message type: %s", msg.type)
        except IPCError:
            return None

    def _send_status(self, status: str) -> None:
        self.ipc.send(
            IPCMessage(
                msg_id=str(uuid.uuid4()),
                goal_id=self.goal.id if self.goal else None,
                type=MessageType.STATUS_UPDATE,
                payload={"status": status},
            )
        )

    def _send_complete(self, result: str) -> None:
        self.ipc.send(
            IPCMessage(
                msg_id=str(uuid.uuid4()),
                goal_id=self.goal.id if self.goal else None,
                type=MessageType.COMPLETE,
                payload={"result": result},
            )
        )

    def _send_error(self, error: str) -> None:
        self.ipc.send(
            IPCMessage(
                msg_id=str(uuid.uuid4()),
                goal_id=self.goal.id if self.goal else None,
                type=MessageType.ERROR,
                payload={"error": error},
            )
        )

    @staticmethod
    def _assistant_message(response: AssistantResponse) -> Message:
        return Message(
            role="assistant",
            content=response.content or "（无内容）",
            tool_calls=response.tool_calls,
        )


def _format_tool_result(result: ToolResult) -> str:
    import json

    return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
