"""Direct-mode agent: LLM loop with in-process tool execution, zero IPC overhead.

Replaces the supervisor/worker/IPC pipeline for single-agent tasks (SWE-bench).
Model calls tools directly — no IPC round-trips, no worker crashes, no message
serialization overhead.  Same tool set, same LLM client, just faster.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agent.llm.client import LLMClient
from agent.llm.schema import Message, ToolCall, AssistantResponse
from agent.tools import TOOL_REGISTRY
from agent.tools.base import ToolContext, ToolResult

logger = logging.getLogger("agent.direct")


def _format_tool_result(result: ToolResult) -> str:
    """Format a tool result for the LLM conversation."""
    parts: list[str] = []
    if result.output:
        parts.append(result.output)
    if result.error:
        parts.append(f"[ERROR] {result.error}")
    if result.metadata:
        import json

        try:
            parts.append(json.dumps(result.metadata, ensure_ascii=False))
        except (TypeError, ValueError):
            pass
    return "\n".join(parts) if parts else "(no output)"


class DirectAgent:
    """Run a single-goal LLM agent with direct (in-process) tool execution."""

    def __init__(
        self,
        llm: LLMClient,
        workspace: str | Path,
        system_prompt: str,
        allowed_tools: list[str] | None = None,
    ):
        self.llm = llm
        self.workspace = Path(workspace).resolve()
        self.system_prompt = system_prompt
        # Build tool list
        all_tools = TOOL_REGISTRY
        if allowed_tools is None:
            self.tools = list(all_tools.values())
        else:
            self.tools = [
                t for name, t in all_tools.items() if name in set(allowed_tools)
            ]
        self.tool_names = [t.name for t in self.tools]
        self._tool_map = {t.name: t for t in self.tools}

    def run(self, goal_description: str, max_steps: int = 50) -> str:
        """Execute the agent loop and return the final answer or error message.

        Returns the agent's final text response (or error description).
        The caller is responsible for extracting the patch from the workspace
        via ``git diff`` after this method returns.
        """
        from agent.llm.parser import build_tools_payload

        ctx = ToolContext(workspace=str(self.workspace))

        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=goal_description),
        ]

        tools_schema = build_tools_payload(self.tools)

        for step in range(1, max_steps + 1):
            logger.info("step %d/%d: calling LLM", step, max_steps)
            try:
                response = self.llm.chat(messages, tools=tools_schema)
            except Exception as exc:
                logger.exception("LLM call failed at step %d", step)
                return f"LLM error at step {step}: {exc}"

            # Build assistant message
            assistant_msg = Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls if response.tool_calls else None,
            )
            messages.append(assistant_msg)

            # If no tool calls, model produced a final answer
            if not response.tool_calls:
                logger.info("agent finished at step %d (final answer)", step)
                return response.content or ""

            # Execute tool calls in sequence (model may request parallel, we
            # execute sequentially for simplicity — same as Claude Code)
            for call in response.tool_calls:
                args_str = ", ".join(f"{k}={str(v)[:80]}" for k, v in call.arguments.items())
                logger.info("tool call: %s(%s)", call.name, args_str)
                tool = self._tool_map.get(call.name)
                if tool is None:
                    logger.warning("unknown tool requested: %s", call.name)
                    result = ToolResult(
                        success=False,
                        error=f"unknown tool '{call.name}'. Available: {', '.join(self.tool_names)}",
                    )
                else:
                    try:
                        result = tool.execute(call.arguments, ctx)
                        logger.info(
                            "tool result: %s success=%s output_len=%s error=%s",
                            call.name,
                            result.success,
                            len(result.output or ""),
                            (result.error or "")[:100],
                        )
                    except Exception as exc:
                        logger.exception("tool %s raised an exception", call.name)
                        result = ToolResult(
                            success=False,
                            error=f"tool '{call.name}' failed: {exc}",
                        )

                messages.append(
                    Message(
                        role="tool",
                        content=_format_tool_result(result),
                        tool_call_id=call.id,
                    )
                )

        logger.warning("agent reached max steps (%d)", max_steps)
        return f"Reached maximum steps ({max_steps}) without final answer."
