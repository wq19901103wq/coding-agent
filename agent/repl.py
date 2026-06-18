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
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

from agent.config import Config, load_config
from agent.context import ContextManager
from agent.history import HistoryManager
from agent.indexing import Indexer
from agent.llm import LLMClient, LLMError, Message, ToolCall, build_tools_payload
from agent.llm.schema import AssistantResponse, Usage
from agent.logging_config import setup_logging
from agent.mcp_client import MCPClient
from agent.safety import (
    CommandClass,
    PathOutsideWorkspaceError,
    classify_shell_command,
    validate_path,
)
from agent.supervisor import Supervisor
from agent.supervisor.models import GoalStatus
from agent.supervisor.persistence import resolve_db_path
from agent.supervisor.role_loader import RoleLoader
from agent.tools import TOOL_REGISTRY, ToolContext, ToolResult, get_tool
from agent.tools.apply_patch import parse_diff

_FILE_WRITE_TOOLS = {"write_file", "str_replace_file", "apply_patch"}

logger = logging.getLogger("agent.repl")


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


def _build_system_prompt(
    workspace: str,
    tools_schema: list[dict[str, Any]],
    extra_prompt: str | None = None,
) -> str:
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        workspace=workspace,
        tools_schema=json.dumps(tools_schema, ensure_ascii=False, indent=2),
    )
    if extra_prompt:
        prompt += f"\n\n额外要求：\n{extra_prompt}"
    return prompt


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
        self.config = config or load_config(workspace=self.workspace)
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
                content=_build_system_prompt(
                    self.workspace, self.tools_schema, self.config.llm.system_prompt
                ),
            )
        ]
        self._total_usage = Usage()
        self._write_backups: list[dict[str, str]] = []
        self._context_manager = ContextManager(self.messages, self.config.context)
        self._mcp_client: MCPClient | None = None
        self._goal_completion_event: threading.Event | None = None
        goals_db_path = resolve_db_path(self.workspace)

        def _confirm(prompt: str) -> bool:
            answer = self.input_func(prompt).strip().lower()
            return answer in ("y", "yes")

        def _on_goal_completed(_goal) -> None:
            event = self._goal_completion_event
            if event is not None:
                event.set()

        self.supervisor = Supervisor(
            workspace=self.workspace,
            config=self.config,
            db_path=goals_db_path,
            confirm_callback=_confirm,
            goal_completed_callback=_on_goal_completed,
        )
        self.supervisor.start()
        self.current_role = "default"
        self._load_history()
        self._connect_mcp()

    @staticmethod
    def _default_input(prompt: str = "") -> str:
        try:
            import readline

            # 修复部分终端下中文退格/光标错位问题
            readline.parse_and_bind("set meta-flag on")
            readline.parse_and_bind("set input-meta on")
            readline.parse_and_bind("set convert-meta off")
            readline.parse_and_bind("set output-meta on")
        except Exception:
            pass
        return input(prompt)

    def _load_history(self) -> None:
        if not self.config.history.enabled:
            return
        recent = self.history.load_messages(self.session_id, limit=self.config.history.max_messages)

        # 校验历史完整性：
        # 1. 丢弃末尾不完整的 assistant(tool_calls)（崩溃在 tool 执行前）。
        # 2. 丢弃 tool_call_id 为空或不匹配任何 assistant tool_call 的 tool 消息。
        if recent and recent[-1].role == "assistant" and recent[-1].tool_calls:
            dropped = recent.pop()
            self.console.print(
                f"[dim]检测到未完成的对话记录（role={dropped.role}），已自动清理。[/dim]"
            )

        valid_tool_call_ids = {
            tc.id
            for msg in recent
            if msg.role == "assistant" and msg.tool_calls
            for tc in msg.tool_calls
        }
        cleaned: list[Message] = []
        dropped_count = 0
        for msg in recent:
            if msg.role == "tool" and (
                not msg.tool_call_id or msg.tool_call_id not in valid_tool_call_ids
            ):
                dropped_count += 1
                continue
            cleaned.append(msg)

        if dropped_count:
            self.console.print(f"[dim]已清理 {dropped_count} 条无效的 tool 消息记录。[/dim]")

        self.messages.extend(cleaned)
        logger.debug(
            "Loaded %s messages for session %s (dropped %s invalid tool messages)",
            len(cleaned),
            self.session_id,
            dropped_count,
        )

    def _save_message(self, msg: Message) -> None:
        if self.config.history.enabled:
            self.history.save_message(self.session_id, msg)

    def run(self) -> None:
        """启动 REPL 循环。"""
        self.console.print(f"[bold green]coding-agent[/bold green] 工作目录: {self.workspace}")
        self._print_git_status()
        self._print_pending_todos()
        self._print_help()

        try:
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
        finally:
            self._disconnect_mcp()
            self.supervisor.stop()

    def _handle_slash_command(self, command: str) -> None:
        parts = command.split(maxsplit=1)
        name = parts[0]
        arg = parts[1] if len(parts) > 1 else ""

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
        elif name == "/sessions":
            self._handle_sessions_command()
        elif name == "/switch":
            self._handle_switch_command(arg)
        elif name == "/rename":
            self._handle_rename_command(arg)
        elif name == "/delete":
            self._handle_delete_command(arg)
        elif name == "/tokens":
            self._handle_tokens_command()
        elif name == "/history":
            self._handle_history_command(arg)
        elif name == "/undo":
            self._handle_undo_command()
        elif name == "/compact":
            self._handle_compact_command()
        elif name == "/reload":
            self._handle_reload_command()
        elif name == "/git":
            self._handle_git_command()
        elif name == "/mcp":
            self._handle_mcp_command()
        elif name == "/yolo":
            self._handle_yolo_command(arg)
        elif name == "/goals":
            self._handle_goals_command(arg)
        elif name == "/agent":
            self._handle_agent_command(arg)
        else:
            self.console.print(f"[red]未知命令: {command}[/red]")

    def _handle_sessions_command(self) -> None:
        """列出最近会话。"""
        sessions = self.history.list_recent_sessions(limit=10)
        if not sessions:
            self.console.print("[dim]暂无会话记录。[/dim]")
            return

        self.console.print("[bold]最近会话：[/bold]")
        for idx, session in enumerate(sessions, start=1):
            title = session.get("title") or "(未命名)"
            current = " [当前]" if session["id"] == self.session_id else ""
            self.console.print(
                f"  {idx}. {session['id'][:8]}  {title}{current}  "
                f"{session['workspace']}  {session['updated_at']}"
            )

    def _resolve_session_id(self, arg: str) -> str | None:
        """把用户输入的序号或 ID 前缀解析为完整 session id。"""
        sessions = self.history.list_recent_sessions(limit=100)
        if arg.isdigit():
            idx = int(arg) - 1
            if 0 <= idx < len(sessions):
                return str(sessions[idx]["id"])
            return None
        for session in sessions:
            session_id = str(session["id"])
            if session_id == arg or session_id.startswith(arg):
                return session_id
        return None

    def _handle_switch_command(self, arg: str) -> None:
        """切换到指定会话。"""
        if not arg:
            self.console.print("[red]用法: /switch <会话ID或序号>[/red]")
            return

        target_id = self._resolve_session_id(arg)
        if target_id is None:
            self.console.print(f"[red]找不到会话: {arg}[/red]")
            return

        session = self.history.get_session(target_id)
        if session is None:
            self.console.print(f"[red]找不到会话: {arg}[/red]")
            return

        self.session_id = target_id
        self.workspace = session["workspace"]
        self.messages = [
            Message(
                role="system",
                content=_build_system_prompt(
                    self.workspace, self.tools_schema, self.config.llm.system_prompt
                ),
            )
        ]
        self._load_history()
        self.console.print(f"[green]已切换到会话 {target_id[:8]}[/green]")

    def _handle_rename_command(self, arg: str) -> None:
        """重命名当前会话。"""
        if not arg:
            self.console.print("[red]用法: /rename <新标题>[/red]")
            return

        self.history.rename_session(self.session_id, arg)
        self.console.print(f"[green]会话已重命名为: {arg}[/green]")

    def _handle_delete_command(self, arg: str) -> None:
        """删除指定会话。"""
        if not arg:
            self.console.print("[red]用法: /delete <会话ID或序号>[/red]")
            return

        target_id = self._resolve_session_id(arg)
        if target_id is None:
            self.console.print(f"[red]找不到会话: {arg}[/red]")
            return

        if target_id == self.session_id:
            self.history.delete_session(target_id)
            self.session_id = self.history.get_or_create_session(self.workspace)
            self.messages = [self.messages[0]]
            self.console.print("[green]当前会话已删除，已创建新会话。[/green]")
        else:
            self.history.delete_session(target_id)
            self.console.print(f"[green]会话 {target_id[:8]} 已删除。[/green]")

    def _maybe_auto_compact(self) -> bool:
        """如果开启自动压缩且接近阈值，执行压缩。"""
        if not self.config.context.auto_compact:
            return False
        if not self._context_manager.is_near_limit():
            return False
        return self._context_manager.compact(self.llm)

    def _handle_compact_command(self) -> None:
        """手动压缩上下文。"""
        if self._context_manager.compact(self.llm):
            self.console.print("[green]上下文已压缩。[/green]")
        else:
            self.console.print("[yellow]当前消息不足，无需压缩。[/yellow]")

    def _handle_reload_command(self) -> None:
        """重新加载配置。"""
        self.config = load_config(workspace=self.workspace)
        self._context_manager.config = self.config.context
        self.console.print("[green]配置已重新加载。[/green]")

    def _handle_git_command(self) -> None:
        """显示当前 git 状态。"""
        status = self._git_status()
        if status is None:
            self.console.print("[dim]当前工作目录不是 git 仓库。[/dim]")
            return
        self.console.print(f"[bold]分支:[/bold] {status['branch']}")
        if status["uncommitted"]:
            self.console.print(f"[yellow]未提交文件: {len(status['uncommitted'])}[/yellow]")
            for line in status["uncommitted"][:10]:
                self.console.print(f"  {line}")
            if len(status["uncommitted"]) > 10:
                self.console.print(f"  ... 还有 {len(status['uncommitted']) - 10} 个文件")
        else:
            self.console.print("[green]工作区干净[/green]")

    def _git_status(self) -> dict[str, Any] | None:
        """获取当前工作目录的 git 状态，不是 git 仓库则返回 None。"""
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--branch"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

        if result.returncode != 0:
            return None

        lines = result.stdout.splitlines()
        branch = "unknown"
        uncommitted: list[str] = []
        for line in lines:
            if line.startswith("##"):
                # ## main...origin/main
                branch_part = line[3:].split("...", 1)[0]
                branch = branch_part.strip()
            elif line.strip():
                uncommitted.append(line.strip())

        return {"branch": branch, "uncommitted": uncommitted}

    def _connect_mcp(self) -> None:
        """根据配置连接 MCP server 并注册其工具。"""
        if not getattr(self.config.mcp, "enabled", False):
            return
        command = getattr(self.config.mcp, "command", None)
        if not command or not isinstance(command, str):
            return
        args = getattr(self.config.mcp, "args", None) or []
        if not isinstance(args, list):
            args = []
        try:
            from agent.tools import register_tool
            from agent.tools.mcp_adapter import MCPToolAdapter

            self._mcp_client = MCPClient(
                command=command,
                args=args,
            )
            self._mcp_client.connect()
            for tool in self._mcp_client.tools:
                register_tool(MCPToolAdapter(tool, self._mcp_client))
                logger.info("Registered MCP tool: %s", tool.name)
            self.console.print(
                f"[dim]已连接 MCP server，注册 {len(self._mcp_client.tools)} 个工具。[/dim]"
            )
        except Exception as exc:
            logger.exception("Failed to connect MCP server")
            self.console.print(f"[red]连接 MCP server 失败: {exc}[/red]")

    def _disconnect_mcp(self) -> None:
        if self._mcp_client:
            try:
                self._mcp_client.disconnect()
            except Exception:
                logger.exception("Error disconnecting MCP client")
            self._mcp_client = None

    def _handle_mcp_command(self) -> None:
        """显示当前 MCP 连接状态。"""
        if not self._mcp_client:
            self.console.print("[dim]未连接 MCP server。[/dim]")
            return
        self.console.print(f"[bold]MCP 已连接[/bold]，共 {len(self._mcp_client.tools)} 个工具：")
        for tool in self._mcp_client.tools:
            self.console.print(f"  - {tool.name}")

    def _handle_yolo_command(self, arg: str) -> None:
        """切换危险操作确认开关（yolo 模式）。"""
        arg = arg.strip().lower()
        if arg == "on":
            self.config.security.confirm_dangerous = False
            self.console.print("[yellow]已切换到 YOLO 模式：危险操作不再确认[/yellow]")
        elif arg == "off":
            self.config.security.confirm_dangerous = True
            self.console.print("[green]已切换到安全模式：危险操作需要确认[/green]")
        elif arg in ("", "status"):
            if self.config.security.confirm_dangerous:
                self.console.print("[green]当前为安全模式：危险操作需要确认[/green]")
            else:
                self.console.print("[yellow]当前为 YOLO 模式：危险操作不再确认[/yellow]")
        else:
            self.console.print("[red]用法：/yolo on|off|status[/red]")

    def _handle_goals_command(self, arg: str) -> None:
        """处理 /goals 命令。"""
        arg = arg.strip()
        if not arg or arg == "list":
            self._print_goals()
            return

        known_subs = {"all", "add", "show", "cancel", "resume", "clear-done"}
        first = arg.split(maxsplit=1)[0]
        if first in known_subs:
            rest = arg[len(first) :].strip()
            if first == "all":
                self._print_goals(all_goals=True)
            elif first == "add":
                self._handle_add_goal(rest)
            elif first == "show":
                self._handle_show_goal(rest)
            elif first == "cancel":
                self._handle_cancel_goal(rest)
            elif first == "resume":
                self._handle_resume_goal(rest)
            elif first == "clear-done":
                self._handle_clear_done_goals()
            return

        # /goals <需求描述> [role]
        self._handle_add_goal(arg)

    def _handle_add_goal(self, arg: str) -> None:
        import shlex

        try:
            parts = shlex.split(arg.strip())
        except ValueError:
            self.console.print("[red]参数解析失败，请检查引号[/red]")
            return
        if not parts:
            self.console.print("[red]用法: /goals add <title> [role][/red]")
            return
        title = parts[0]
        role = parts[1] if len(parts) > 1 else "default"
        try:
            RoleLoader().get(role)
        except KeyError:
            self.console.print(f"[red]未知角色: {role}[/red]")
            return
        goal = self.supervisor.submit_goal(title=title, description="", agent_role=role)
        self.console.print(f"[green]已创建目标: {goal.id} ({goal.title})[/green]")
        self._process_supervisor_goal(goal.id)

    def _handle_show_goal(self, goal_id: str) -> None:
        goal = self.supervisor.persistence.get(goal_id.strip())
        if goal is None:
            self.console.print(f"[red]找不到目标: {goal_id}[/red]")
            return
        self.console.print(f"[bold]{goal.id}[/bold]: {goal.title}")
        self.console.print(f"  角色: {goal.agent_role}")
        self.console.print(f"  状态: {goal.status.value}")
        self.console.print(f"  描述: {goal.description or '(无)'}")
        if goal.result_summary:
            self.console.print(f"  结果: {goal.result_summary}")

    def _handle_cancel_goal(self, goal_id: str) -> None:
        goal_id = goal_id.strip()
        goal = self.supervisor.persistence.get(goal_id)
        if goal is None:
            self.console.print(f"[red]找不到目标: {goal_id}[/red]")
            return
        self.supervisor.cancel_goal(goal_id)
        self.console.print(f"[yellow]已取消目标: {goal_id}[/yellow]")

    def _handle_resume_goal(self, goal_id: str) -> None:
        goal_id = goal_id.strip()
        goal = self.supervisor.persistence.get(goal_id)
        if goal is None:
            self.console.print(f"[red]找不到目标: {goal_id}[/red]")
            return
        self.supervisor.persistence.resume(goal_id)
        self.supervisor.run_goal(goal_id)
        self.console.print(f"[green]已恢复目标: {goal_id}[/green]")

    def _handle_clear_done_goals(self) -> None:
        done = self.supervisor.persistence.list_goals(status=GoalStatus.DONE)
        for goal in done:
            self.supervisor.persistence.delete(goal.id)
        self.console.print(f"[green]已清理 {len(done)} 个已完成目标[/green]")

    def _print_goals(self, all_goals: bool = False) -> None:
        if all_goals:
            goals = self.supervisor.persistence.list_all()
        else:
            goals = self.supervisor.persistence.list_active()
        if not goals:
            self.console.print("[dim]暂无目标。[/dim]")
            return
        self.console.print("[bold]目标列表:[/bold]")
        for goal in goals:
            self.console.print(
                f"  [bold]{goal.id}[/bold] {goal.title} "
                f"([cyan]{goal.agent_role}[/cyan]) - {goal.status.value}"
            )

    def _handle_agent_command(self, arg: str) -> None:
        """处理 /agent 命令。"""
        arg = arg.strip()
        if arg in ("", "list"):
            roles = RoleLoader().list_roles()
            self.console.print("[bold]可用角色:[/bold]")
            for name in roles:
                role = RoleLoader().get(name)
                self.console.print(f"  [cyan]{name}[/cyan]: {role.description}")
            return
        try:
            RoleLoader().get(arg)
            self.current_role = arg
            self.console.print(f"[green]已切换到角色: {arg}[/green]")
        except KeyError:
            self.console.print(f"[red]未知角色: {arg}[/red]")

    def _should_use_supervisor(self, user_input: str) -> bool:
        """判断是否应该使用 supervisor 处理复杂任务。"""
        if user_input.startswith("/goals") or user_input.startswith("/agent"):
            return True
        if len(user_input) > 500:
            return True
        keywords = ["规划", "重构", "多文件", "设计", "review", "审查", "分解"]
        return any(kw in user_input for kw in keywords)

    def _process_supervisor_input(self, user_input: str) -> None:
        """通过 supervisor 处理用户输入。"""
        goal = self.supervisor.submit_goal(
            title=user_input[:50],
            description=user_input,
            agent_role=self.current_role,
        )
        self.console.print(f"[bold blue]已创建目标 {goal.id}，正在执行...[/bold blue]")
        self._process_supervisor_goal(goal.id)

    def _process_supervisor_goal(self, goal_id: str) -> None:
        """执行单个 supervisor goal 并等待结果。"""
        self._goal_completion_event = threading.Event()
        self.supervisor.run_goal(goal_id)

        timeout = self.config.llm.timeout or 300.0
        try:
            for _ in range(int(timeout)):
                if self._goal_completion_event.wait(timeout=1.0):
                    break
                self.console.print("[dim].[/dim]", end="")
        except KeyboardInterrupt:
            self.console.print("\n[yellow]已取消等待，目标仍在后台执行。[/yellow]")
            return
        finally:
            self._goal_completion_event = None

        fetched = self.supervisor.persistence.get(goal_id)
        if fetched is None:
            self.console.print("[red]目标状态丢失[/red]")
            return
        if fetched.status == GoalStatus.DONE:
            self.console.print(f"[green]目标完成:[/green] {fetched.result_summary or ''}")
        elif fetched.status == GoalStatus.FAILED:
            self.console.print(f"[red]目标失败:[/red] {'; '.join(fetched.error_log)}")
        else:
            self.console.print("[yellow]目标仍在执行中，可使用 /goals 查看状态。[/yellow]")

    def _print_git_status(self) -> None:
        """启动时打印简洁的 git 状态。"""
        status = self._git_status()
        if status is None:
            return
        if status["uncommitted"]:
            self.console.print(
                f"[dim]git: {status['branch']} | 未提交文件 {len(status['uncommitted'])}[/dim]"
            )
        else:
            self.console.print(f"[dim]git: {status['branch']} | 工作区干净[/dim]")

    def _handle_undo_command(self) -> None:
        """撤销最近一次写操作。"""
        if not self._write_backups:
            self.console.print("[yellow]没有可撤销的操作。[/yellow]")
            return

        last = self._write_backups.pop()
        target = Path(self.workspace) / last["path"]
        backup_path = Path(last["backup_path"])
        if not backup_path.exists():
            self.console.print("[red]备份文件不存在，无法撤销。[/red]")
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.console.print(f"[green]已撤销对 {last['path']} 的修改。[/green]")

    def _handle_tokens_command(self) -> None:
        """显示当前会话 token 使用情况。"""
        self.console.print("[bold]Token 使用情况（本会话累计）：[/bold]")
        self.console.print(f"  prompt:     {self._total_usage.prompt_tokens}")
        self.console.print(f"  completion: {self._total_usage.completion_tokens}")
        self.console.print(f"  total:      {self._total_usage.total_tokens}")

    def _handle_history_command(self, arg: str) -> None:
        """显示最近 N 条消息摘要。"""
        try:
            limit = int(arg)
        except ValueError:
            limit = 10

        # 跳过 system prompt
        recent = [m for m in self.messages if m.role != "system"][-limit:]
        if not recent:
            self.console.print("[dim]暂无历史消息。[/dim]")
            return

        self.console.print(f"[bold]最近 {len(recent)} 条消息：[/bold]")
        for msg in recent:
            if msg.role == "assistant" and msg.tool_calls:
                names = ", ".join(tc.name for tc in msg.tool_calls)
                preview = f"[调用工具: {names}]"
            elif msg.role == "tool":
                preview = f"[工具结果 id={msg.tool_call_id}]"
            else:
                preview = (msg.content or "")[:100]
                if len(msg.content or "") > 100:
                    preview += "..."
            self.console.print(f"  \\[{msg.role}] {preview}")

    def _auto_set_session_title(self, text: str) -> None:
        """新会话自动用第一条用户消息前 30 字作为标题。"""
        try:
            session = self.history.get_session(self.session_id)
        except Exception:
            return
        if session is None:
            return
        if session.get("title"):
            return
        title = text[:30] + ("..." if len(text) > 30 else "")
        if title.strip():
            self.history.rename_session(self.session_id, title.strip())

    def _process_user_input(self, text: str) -> bool:
        if self._should_use_supervisor(text):
            self._process_supervisor_input(text)
            return True

        user_msg = Message(role="user", content=text)
        self._save_message(user_msg)
        self.messages.append(user_msg)
        self._auto_set_session_title(text)

        try:
            response = self._run_turn()
        except LLMError as exc:
            self.console.print(f"[red]❌ LLM 请求失败: {exc}[/red]")
            logger.error("LLM request failed: %s", exc)
            # 记录发送给 LLM 的消息摘要，便于排查 tool_call_id 类问题
            for idx, msg in enumerate(self.messages):
                logger.debug(
                    "Message[%s] role=%s tool_calls=%s tool_call_id=%s",
                    idx,
                    msg.role,
                    [tc.id for tc in (msg.tool_calls or [])],
                    msg.tool_call_id,
                )
            return False
        except KeyboardInterrupt:
            self.console.print("[yellow]⚠️  操作已取消[/yellow]")
            logger.info("User cancelled the operation")
            return False
        except Exception as exc:
            self.console.print(f"[red]❌ 处理请求时发生错误: {exc}[/red]")
            logger.exception("Unexpected error while processing user input")
            return False

        # 流式输出已在 _run_turn_stream 中实时打印，避免重复渲染
        if not self.config.llm.stream:
            self._print_assistant(response.content)

        if self._total_usage.total_tokens:
            self.console.print(
                f"[dim]tokens: {self._total_usage.total_tokens}[/dim]",
                justify="right",
            )
        return True

    def _run_turn(self) -> AssistantResponse:
        """执行一次完整的 LLM 交互 turn。"""
        max_steps = self.config.llm.max_steps_per_turn
        for step in range(max_steps):
            if self.config.llm.stream:
                response = self._run_turn_stream()
            else:
                response = self._run_turn_non_stream()

            # 避免 assistant 消息 content 为空且没有 tool_calls，导致 OpenAI 400 错误
            assistant_content = response.content or ""
            if not assistant_content and not response.tool_calls:
                assistant_content = "（无内容）"
            assistant_msg = Message(
                role="assistant",
                content=assistant_content,
                tool_calls=response.tool_calls,
            )
            self._save_message(assistant_msg)
            self.messages.append(assistant_msg)
            self._total_usage.prompt_tokens += response.usage.prompt_tokens
            self._total_usage.completion_tokens += response.usage.completion_tokens
            self._total_usage.total_tokens += response.usage.total_tokens

            if not response.tool_calls:
                if self._maybe_auto_compact():
                    self.console.print("[dim]上下文已自动压缩。[/dim]")
                return response

            for call in response.tool_calls:
                logger.debug("Executing tool call: id=%s name=%s", call.id, call.name)
                result = self._execute_tool_call(call)
                if (
                    not result.success
                    and call.name != "ask_user"
                    and "User declined" not in (result.error or "")
                    and "forbidden" not in (result.error or "").lower()
                ):
                    self.console.print(f"[yellow]{call.name} 失败，正在重试...[/yellow]")
                    result = self._execute_tool_call(call)
                tool_msg = Message(
                    role="tool",
                    content=_format_tool_result(result),
                    tool_call_id=call.id,
                )
                logger.debug(
                    "Tool result message: tool_call_id=%s success=%s",
                    call.id,
                    result.success,
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

    def _backup_write_operation(self, call: ToolCall) -> None:
        """在执行写操作前备份原文件，用于 /undo。"""
        if call.name in ("write_file", "str_replace_file"):
            path = call.arguments.get("path")
            if path:
                self._backup_file(path)
        elif call.name == "apply_patch":
            diff = call.arguments.get("diff", "")
            try:
                patches = parse_diff(diff)
                for patch in patches:
                    path = patch.new_path or patch.old_path
                    if path and path != "/dev/null":
                        self._backup_file(path)
            except Exception:
                pass

    def _backup_file(self, relative_path: str) -> Path | None:
        """备份单个文件到 ~/.coding-agent/backups/<session_id>/<timestamp>/。

        返回备份路径；如果原文件不存在则返回 None（如新建文件）。
        """
        try:
            validate_path(relative_path, Path(self.workspace))
        except PathOutsideWorkspaceError:
            logger.warning("backup skipped for path outside workspace: %s", relative_path)
            return None

        target = Path(self.workspace) / relative_path
        if not target.exists():
            return None

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        backup_dir = Path.home() / ".coding-agent" / "backups" / self.session_id / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        self._write_backups.append({"path": relative_path, "backup_path": str(backup_path)})
        return backup_path

    def _preview_apply_patch(self, call: ToolCall) -> None:
        """在确认前展示 apply_patch 的变更摘要。"""
        diff = call.arguments.get("diff", "")
        if not diff:
            return
        try:
            patches = parse_diff(diff)
        except Exception:
            self.console.print("[yellow]⚠️  无法解析 patch 预览[/yellow]")
            return

        if not patches:
            return

        self.console.print("[bold cyan]📋 即将应用以下变更：[/bold cyan]")
        for patch in patches:
            old_path = patch.old_path or "/dev/null"
            new_path = patch.new_path or "/dev/null"
            if old_path == new_path:
                action = f"修改 {new_path}"
            elif old_path == "/dev/null":
                action = f"新增 {new_path}"
            elif new_path == "/dev/null":
                action = f"删除 {old_path}"
            else:
                action = f"重命名 {old_path} -> {new_path}"

            added = sum(1 for h in patch.hunks for line in h.lines if line.startswith("+"))
            removed = sum(1 for h in patch.hunks for line in h.lines if line.startswith("-"))
            self.console.print(f"  • {action} [green]+{added}[/green] [red]-{removed}[/red]")

    def _execute_tool_call(self, call: ToolCall) -> ToolResult:
        """执行单个 tool call，处理安全确认与 ask_user 交互。"""
        self.console.print(
            f"🔧 调用工具: [bold]{call.name}[/bold]({self._format_tool_arguments(call.arguments)})"
        )

        if call.name == "apply_patch":
            self._preview_apply_patch(call)

        if call.name in _FILE_WRITE_TOOLS:
            self._backup_write_operation(call)

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
                # 用户已确认，使用可信入口执行危险命令
                result = tool.execute_forced(call.arguments, ctx)
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

        # shell 命令涉及命令注入风险，不支持永久放行
        is_shell = call.name == "execute_shell"
        prompt_options = "[y/n]" if is_shell else "[y/n/a]"
        prompt_hint = "y: 是, n: 否" if is_shell else "y: 是, n: 否, a: 总是允许"

        while True:
            prompt_text = f"是否执行？{prompt_options} ({prompt_hint}): "
            answer = self.input_func(prompt_text).strip().lower()
            if answer in ("y", "yes", "是"):
                return True
            if answer in ("n", "no"):
                return False
            if answer in ("a", "always"):
                if is_shell:
                    self.console.print("[red]shell 命令不支持永久放行，请输入 y 或 n[/red]")
                    continue
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
        self.console.print(
            "[bold]快捷命令[/bold]: /help, /clear, /model, /index, "
            "/sessions, /switch, /rename, /delete, /tokens, /history, /undo, "
            "/compact, /reload, /git, /mcp, /yolo, /goals, /agent | 退出: exit/quit"
        )

    def run_once(self, command: str) -> int:
        """非交互执行单条指令，返回退出码（0 成功，1 失败）。"""
        self._print_pending_todos()
        self._print_help()

        if command.lower() in ("exit", "quit"):
            self.console.print("再见！")
            return 0

        success = self._process_user_input(command)
        return 0 if success else 1


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="coding-agent 命令行 AI 编程助手")
    parser.add_argument(
        "workspace",
        nargs="?",
        default=".",
        help="工作目录（默认为当前目录）",
    )
    parser.add_argument(
        "--run",
        dest="command",
        default=None,
        help="非交互执行单条指令后退出",
    )
    args = parser.parse_args(argv)
    workspace = Path(args.workspace).resolve()
    load_dotenv(workspace / ".env", override=False)

    repl = REPL(workspace=str(workspace))
    if args.command:
        return repl.run_once(args.command)
    repl.run()
    return 0
