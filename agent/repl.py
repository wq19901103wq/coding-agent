"""coding-agent REPL 主循环。

提供交互式命令行界面，负责：
- 会话管理与历史加载
- 用户快捷命令处理
- LLM 工具调用循环
- 危险命令确认
- ask_user 交互
- 消息持久化
"""

import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

from agent.config import Config, load_config
from agent.history import HistoryManager
from agent.indexing import Indexer
from agent.llm import LLMClient, Message, ToolCall, build_tools_payload
from agent.llm.schema import AssistantResponse
from agent.safety import CommandClass, classify_shell_command
from agent.tools import TOOL_REGISTRY, ToolContext, ToolResult, get_tool

_FILE_WRITE_TOOLS = {"write_file", "str_replace_file", "apply_patch"}


SYSTEM_PROMPT_TEMPLATE = """你是一个命令行 AI 编程助手。工作目录：{workspace}

可用工具：
{tools_schema}

规则：
1. 优先使用工具完成任务。
2. 危险操作会询问用户确认，forbidden 命令会被直接拒绝。
3. 所有路径必须是相对于工作目录的相对路径。
4. 如果信息不足，使用 ask_user 工具询问用户。
5. 每次回复尽可能简洁、明确。
"""


def _build_system_prompt(workspace: str, tools_schema: list[dict[str, Any]]) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        workspace=workspace,
        tools_schema=json.dumps(tools_schema, ensure_ascii=False, indent=2),
    )


def _format_tool_result(result: ToolResult) -> str:
    """将工具执行结果序列化为 tool 消息内容。"""
    return json.dumps(result.model_dump(), ensure_ascii=False, default=str)


class REPL:
    """REPL 主循环。"""

    def __init__(
        self,
        workspace: str,
        config: Config | None = None,
        llm_client: LLMClient | None = None,
        console: Console | None = None,
        input_func: Callable[[str], str] | None = None,
        history_manager: HistoryManager | None = None,
    ):
        self.workspace = str(Path(workspace).resolve())
        self.config = config or load_config()
        self.console = console or Console()
        self.input_func = input_func or self._default_input
        self.history = history_manager or HistoryManager(self.config.history.db_path)
        self.session_id = self.history.get_or_create_session(self.workspace)
        self.llm = llm_client or LLMClient(self.config.llm)
        self.tools_schema = build_tools_payload(list(TOOL_REGISTRY.values()))
        self._always_allowed_tools: set[str] = set()

        index_db_path = self.config.history.db_path or os.path.expanduser(
            "~/.coding-agent/code_index.db"
        )
        self.indexer = Indexer(self.workspace, index_db_path)
        if self.indexer.is_stale():
            self.indexer.build()
        self.messages: list[Message] = [
            Message(
                role="system",
                content=_build_system_prompt(self.workspace, self.tools_schema),
            )
        ]
        self._load_history()

    @staticmethod
    def _default_input(prompt: str = "") -> str:
        return input(prompt)

    def _load_history(self) -> None:
        if not self.config.history.enabled:
            return
        recent = self.history.load_messages(self.session_id, limit=self.config.history.max_messages)
        self.messages.extend(recent)

    def _save_message(self, msg: Message) -> None:
        if self.config.history.enabled:
            self.history.save_message(self.session_id, msg)

    def run(self) -> None:
        """启动 REPL 循环。"""
        self.console.print(f"[bold green]coding-agent[/bold green] 工作目录: {self.workspace}")
        self._print_pending_todos()
        self._print_help()

        while True:
            try:
                user_input = self.input_func("coding-agent>").strip()
            except (EOFError, KeyboardInterrupt):
                self.console.print("\n再见！")
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit"):
                self.console.print("再见！")
                break
            if user_input.startswith("/"):
                self._handle_slash_command(user_input)
                continue

            self._process_user_input(user_input)

    def _handle_slash_command(self, command: str) -> None:
        parts = command.split(maxsplit=1)
        name = parts[0]

        if name == "/help":
            self._print_help()
        elif name == "/clear":
            self.console.clear()
            self.history.clear_session(self.session_id)
            self.messages = [self.messages[0]]
            self.console.print("屏幕已清除，当前会话历史已清空。")
        elif name == "/model":
            self.console.print(f"当前模型: {self.config.llm.provider}/{self.config.llm.model}")
        elif name == "/index":
            self.console.print("[bold blue]正在重建代码索引...[/bold blue]")
            self.indexer.build()
            self.console.print("[bold green]代码索引已重建。[/bold green]")
        else:
            self.console.print(f"[red]未知命令: {command}[/red]")

    def _process_user_input(self, text: str) -> None:
        user_msg = Message(role="user", content=text)
        self._save_message(user_msg)
        self.messages.append(user_msg)

        response = self._run_turn()

        # 流式输出已在 _run_turn_stream 中实时打印，避免重复渲染
        if not self.config.llm.stream:
            self._print_assistant(response.content)

    def _run_turn(self) -> AssistantResponse:
        """执行一次完整的 LLM 交互 turn。"""
        max_steps = self.config.llm.max_steps_per_turn
        for step in range(max_steps):
            if self.config.llm.stream:
                response = self._run_turn_stream()
            else:
                response = self._run_turn_non_stream()

            assistant_msg = Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            )
            self._save_message(assistant_msg)
            self.messages.append(assistant_msg)

            if not response.tool_calls:
                return response

            for call in response.tool_calls:
                result = self._execute_tool_call(call)
                tool_msg = Message(
                    role="tool",
                    content=_format_tool_result(result),
                    tool_call_id=call.id,
                )
                self._save_message(tool_msg)
                self.messages.append(tool_msg)

        # 达到最大 step 限制
        limit_msg = "⚠️ 已达到本轮最大工具调用次数上限，停止执行。"
        self.console.print(limit_msg)
        limit_response = AssistantResponse(content=limit_msg)
        limit_msg_obj = Message(role="assistant", content=limit_msg)
        self._save_message(limit_msg_obj)
        self.messages.append(limit_msg_obj)
        return limit_response

    def _run_turn_non_stream(self) -> AssistantResponse:
        """非流式执行一个 turn。"""
        return self.llm.chat(self.messages, tools=self.tools_schema)

    def _run_turn_stream(self) -> AssistantResponse:
        """流式执行一个 turn，实时打印 token。"""
        self.console.print("[dim]🤔 思考中...[/dim]")
        content_parts: list[str] = []
        final_response: AssistantResponse | None = None
        first_token = True

        with contextlib.closing(
            self.llm.chat_stream(self.messages, tools=self.tools_schema)
        ) as stream:
            for item in stream:
                if isinstance(item, str):
                    if first_token:
                        self.console.print()  # 从思考状态换行到正式输出
                        first_token = False
                    content_parts.append(item)
                    self.console.print(item, end="")
                elif isinstance(item, AssistantResponse):
                    final_response = item
                    break

        self.console.print()  # 结束当前输出换行

        if final_response is None:
            return AssistantResponse(content="".join(content_parts))

        # 流式过程中已打印的内容优先作为展示内容；
        # 若模型没有输出文本而直接返回 tool_calls，则使用 final_response.content
        content = "".join(content_parts) if content_parts else final_response.content
        return AssistantResponse(
            content=content,
            tool_calls=final_response.tool_calls,
        )

    def _format_tool_arguments(self, arguments: dict) -> str:
        """格式化工具参数用于显示，过长或敏感内容截断/脱敏。"""
        if not arguments:
            return ""
        preview: dict[str, Any] = {}
        for key, value in arguments.items():
            text = str(value)
            if key in ("api_key", "token", "password", "secret"):
                preview[key] = "***"
            elif len(text) > 200:
                preview[key] = text[:200] + "..."
            else:
                preview[key] = value
        return json.dumps(preview, ensure_ascii=False, default=str)

    def _execute_tool_call(self, call: ToolCall) -> ToolResult:
        """执行单个 tool call，处理安全确认与 ask_user 交互。"""
        self.console.print(
            f"🔧 调用工具: [bold]{call.name}[/bold]({self._format_tool_arguments(call.arguments)})"
        )

        if call.name == "ask_user":
            result = self._handle_ask_user(call)
            self.console.print(f"{'✅' if result.success else '❌'} {call.name}")
            return result

        try:
            tool = get_tool(call.name)
        except KeyError:
            result = ToolResult(success=False, error=f"Tool '{call.name}' not found")
            self.console.print(f"❌ {call.name}: {result.error}")
            return result

        ctx = ToolContext(
            workspace=self.workspace,
            config=self.config.model_dump(),
            db_path=self.config.history.db_path,
        )

        if call.name in _FILE_WRITE_TOOLS:
            confirmed = self._confirm_dangerous(call)
            if not confirmed:
                result = ToolResult(
                    success=False,
                    error=(f"User declined {call.name}: '{call.arguments.get('path', '')}'"),
                )
                self.console.print(f"❌ {call.name}: {result.error}")
                return result
            try:
                result = tool.execute(call.arguments, ctx)
                self.console.print(f"{'✅' if result.success else '❌'} {call.name}")
                return result
            except Exception as exc:
                result = ToolResult(success=False, error=f"Tool execution error: {exc}")
                self.console.print(f"❌ {call.name}: {result.error}")
                return result

        if call.name == "execute_shell":
            command = call.arguments.get("command", "")
            classification = classify_shell_command(command)
            if classification == CommandClass.FORBIDDEN:
                result = ToolResult(
                    success=False,
                    error=f"Command classified as forbidden: '{command}'",
                )
                self.console.print(f"❌ {call.name}: {result.error}")
                self._log_safety_event(call, classification, confirmed=None, result=result)
                return result
            if classification == CommandClass.DANGEROUS:
                confirmed = self._confirm_dangerous(call)
                if not confirmed:
                    result = ToolResult(
                        success=False,
                        error=f"User declined dangerous command: '{command}'",
                    )
                    self.console.print(f"❌ {call.name}: {result.error}")
                    self._log_safety_event(call, classification, confirmed=confirmed, result=result)
                    return result
                # 用户已确认，使用内部标记绕过工具内部的危险确认
                result = tool.execute({**call.arguments, "_force": True}, ctx)
                self.console.print(f"{'✅' if result.success else '❌'} {call.name}")
                self._log_safety_event(call, classification, confirmed=confirmed, result=result)
                return result

        try:
            result = tool.execute(call.arguments, ctx)
            self.console.print(f"{'✅' if result.success else '❌'} {call.name}")
            return result
        except Exception as exc:
            result = ToolResult(success=False, error=f"Tool execution error: {exc}")
            self.console.print(f"❌ {call.name}: {result.error}")
            return result

    def _confirm_dangerous(self, call: ToolCall) -> bool:
        if not self.config.security.confirm_dangerous:
            return True
        if call.name in self._always_allowed_tools:
            return True

        self.console.print("\n[bold yellow]⚠️  危险操作需要确认[/bold yellow]")
        self.console.print(f"工具: {call.name}")
        self.console.print(f"参数: {json.dumps(call.arguments, ensure_ascii=False, default=str)}")
        while True:
            answer = (
                self.input_func("是否执行？[y/n/a] (y: 是, n: 否, a: 总是允许): ").strip().lower()
            )
            if answer in ("y", "yes", "是"):
                return True
            if answer in ("n", "no"):
                return False
            if answer in ("a", "always"):
                self._always_allowed_tools.add(call.name)
                return True
            self.console.print("[red]无效输入，请输入 y、n 或 a[/red]")

    def _log_safety_event(
        self,
        call: ToolCall,
        classification: CommandClass,
        confirmed: bool | None,
        result: ToolResult,
    ) -> None:
        """将安全事件追加写入 ~/.coding-agent/safety.log。"""
        if not self.config.security.log_safety_events:
            return

        log_dir = Path.home() / ".coding-agent"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "safety.log"

        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "tool": call.name,
            "arguments": call.arguments,
            "classification": classification.value,
            "confirmed": confirmed,
            "result": {
                "success": result.success,
                "output": result.output,
                "error": result.error,
            },
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")

    def _handle_ask_user(self, call: ToolCall) -> ToolResult:
        question = call.arguments.get("question", "")
        options = call.arguments.get("options")

        self.console.print(f"请问：{question}")
        if options:
            self.console.print("请选择一个选项并回复对应编号或内容：")
            for idx, option in enumerate(options, start=1):
                self.console.print(f"{idx}. {option}")
        else:
            self.console.print("请直接回复你的答案。")

        answer = self.input_func("你的回答> ").strip()
        return ToolResult(success=True, output=answer)

    def _print_assistant(self, content: str | None) -> None:
        if not content:
            return
        self.console.print(Markdown(content))

    def _print_pending_todos(self) -> None:
        """启动时打印当前会话未完成的待办事项。"""
        if not self.config.history.enabled:
            return
        try:
            todos = self.history.list_todos(self.session_id)
            pending = [t for t in todos if t["status"] in ("pending", "in_progress")]
            if pending:
                self.console.print("[bold yellow]📝 待办事项：[/bold yellow]")
                for todo in pending:
                    self.console.print(f"  - [{todo['status']}] {todo['title']}")
                self.console.print()
        except Exception:
            pass

    def _print_help(self) -> None:
        self.console.print("[bold]快捷命令[/bold]: /help, /clear, /model, /index | 退出: exit/quit")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="coding-agent 命令行 AI 编程助手")
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="工作目录（默认为当前目录）",
    )
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    load_dotenv(workspace / ".env", override=False)

    repl = REPL(workspace=str(workspace))
    repl.run()
    return 0
