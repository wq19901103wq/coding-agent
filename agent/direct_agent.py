"""Direct-mode agent: LLM loop with in-process tool execution, zero IPC overhead.

Replaces the supervisor/worker/IPC pipeline for single-agent tasks (SWE-bench).
Model calls tools directly — no IPC round-trips, no worker crashes, no message
serialization overhead.  Same tool set, same LLM client, just faster.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agent.llm.client import LLMClient
from agent.llm.schema import Message, ToolCall
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
        log_path: str | Path | None = None,
        conda_env: str | None = None,
    ):
        self.llm = llm
        self.workspace = Path(workspace).resolve()
        self.conda_env = conda_env
        # Build tool list
        all_tools = TOOL_REGISTRY
        if allowed_tools is None:
            self.tools = list(all_tools.values())
        else:
            self.tools = [t for name, t in all_tools.items() if name in set(allowed_tools)]
        self.tool_names = [t.name for t in self.tools]
        self._tool_map = {t.name: t for t in self.tools}
        self.system_prompt = self._build_system_prompt(system_prompt)
        self.log_path = Path(log_path) if log_path else None
        # Track which files have been read since the last edit.
        self._file_read_state: dict[str, str] = {}
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            # Start fresh log file for each run.
            self.log_path.write_text("", encoding="utf-8")

    def _build_system_prompt(self, base: str) -> str:
        return (
            f"{base}\n\n"
            f"当前工作目录：{self.workspace}\n"
            "你必须在此目录下工作。不要 cd 到其他目录（如 /home/user）。\n"
            "使用相对路径访问文件（如 src/_pytest/junitxml.py），"
            "不要传完整绝对路径。\n"
            f"你被允许使用的工具：{', '.join(self.tool_names)}\n"
            "当存在专用工具（read_file/str_replace_file/code_search/glob_search）时，"
            "绝对不要用 execute_shell 做同样的事。\n"
        )

    def _ensure_file_read_before_edit(
        self,
        call: ToolCall,
        ctx: ToolContext,
        messages: list[Message],
    ) -> tuple[ToolCall, ToolResult]:
        """Require the model to read a file before editing it."""
        path = call.arguments.get("path")
        if not path or path in self._file_read_state:
            tool = self._tool_map["str_replace_file"]
            return call, tool.execute(call.arguments, ctx)

        logger.info("rejecting edit of unread file %s", path)
        return call, ToolResult(
            success=False,
            error=(
                f"You must read '{path}' with read_file before editing it. "
                "This ensures your edit is based on the current file contents."
            ),
        )

    def _compact_messages(self, messages: list[Message], max_turns: int = 20) -> list[Message]:
        """Drop oldest assistant/tool-turn pairs to keep context focused.

        Always keep the system prompt (first message) and the user goal
        (second message). If the conversation exceeds ``max_turns`` assistant
        turns, replace the dropped history with a short summary message so the
        model still knows work has happened.
        """
        if len(messages) <= 2 + max_turns * 2:
            return messages

        kept = messages[:2]
        # Each turn is one assistant message + one or more tool results.
        # Find assistant message indices after the first two messages.
        assistant_indices = [
            i for i, m in enumerate(messages[2:], start=2) if m.role == "assistant"
        ]
        if len(assistant_indices) <= max_turns:
            return messages

        cutoff = assistant_indices[-max_turns]
        dropped_turns = len(assistant_indices) - max_turns
        summary = (
            f"[Earlier {dropped_turns} turns of reasoning and tool results were "
            "removed to keep context focused. The current state of the workspace "
            "reflects any edits already made.]"
        )
        kept.append(Message(role="user", content=summary))
        kept.extend(messages[cutoff:])
        return kept

    def _log_event(self, event: dict[str, Any]) -> None:
        """Append a structured event to the trace log."""
        if not self.log_path:
            return
        event["workspace"] = str(self.workspace)
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.exception("failed to write trace event")

    def run(self, goal_description: str, max_steps: int = 50) -> str:
        """Execute the agent loop and return the final answer or error message.

        Returns the agent's final text response (or error description).
        The caller is responsible for extracting the patch from the workspace
        via ``git diff`` after this method returns.
        """
        from agent.llm.parser import build_tools_payload

        ctx = ToolContext(workspace=str(self.workspace), conda_env=self.conda_env)

        messages: list[Message] = [
            Message(role="system", content=self.system_prompt),
            Message(role="user", content=goal_description),
        ]

        tools_schema = build_tools_payload(self.tools)

        self._log_event(
            {
                "type": "run_start",
                "max_steps": max_steps,
                "tools": self.tool_names,
                "system_prompt": self.system_prompt,
                "goal_description": goal_description,
            }
        )

        for step in range(1, max_steps + 1):
            messages = self._compact_messages(messages, max_turns=20)
            logger.info("step %d/%d: calling LLM", step, max_steps)
            try:
                response = self.llm.chat(messages, tools=tools_schema)
            except Exception as exc:
                logger.exception("LLM call failed at step %d", step)
                self._log_event(
                    {
                        "type": "llm_error",
                        "step": step,
                        "error": str(exc),
                    }
                )
                return f"LLM error at step {step}: {exc}"

            self._log_event(
                {
                    "type": "llm_response",
                    "step": step,
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": c.id,
                            "name": c.name,
                            "arguments": c.arguments,
                        }
                        for c in (response.tool_calls or [])
                    ],
                }
            )

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
                self._log_event(
                    {
                        "type": "final_answer",
                        "step": step,
                        "content": response.content,
                    }
                )
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
                        error=(
                            f"unknown tool '{call.name}'. Available: {', '.join(self.tool_names)}"
                        ),
                    )
                else:
                    try:
                        # Force a re-read before editing a file we haven't seen
                        # recently. This mirrors Claude Code's file-state tracking
                        # and prevents edits based on stale memory.
                        if call.name == "str_replace_file":
                            call, result = self._ensure_file_read_before_edit(call, ctx, messages)
                        else:
                            result = tool.execute(call.arguments, ctx)
                            if call.name == "read_file" and result.success:
                                path = call.arguments.get("path")
                                if path:
                                    self._file_read_state[str(path)] = result.output or ""
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

                self._log_event(
                    {
                        "type": "tool_result",
                        "step": step,
                        "tool_call_id": call.id,
                        "tool_name": call.name,
                        "arguments": call.arguments,
                        "success": result.success,
                        "output": result.output,
                        "error": result.error,
                        "metadata": result.metadata,
                    }
                )

                messages.append(
                    Message(
                        role="tool",
                        content=_format_tool_result(result),
                        tool_call_id=call.id,
                    )
                )

        logger.warning("agent reached max steps (%d)", max_steps)
        self._log_event(
            {
                "type": "max_steps_reached",
                "max_steps": max_steps,
            }
        )
        return f"Reached maximum steps ({max_steps}) without final answer."
