"""REPL 主循环测试。

所有 LLM 交互均使用 mock LLM，不发起真实网络请求。
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from agent.config import Config, LLMConfig
from agent.history import HistoryManager
from agent.llm.schema import AssistantResponse, Message, ToolCall
from agent.repl import REPL, _format_tool_result, main
from agent.tools.base import ToolResult


class MockLLM:
    """用于 REPL 测试的 mock LLM 客户端。"""

    def __init__(
        self,
        responses: list[AssistantResponse] | None = None,
        side_effect: Any = None,
    ):
        self.responses = responses or []
        self.side_effect = side_effect
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
    ) -> AssistantResponse:
        self.calls.append({"messages": messages, "tools": tools})
        if self.side_effect is not None:
            return self.side_effect(messages, tools)
        response = self.responses[self.call_count]
        self.call_count += 1
        return response


def _make_config(**overrides: Any) -> Config:
    defaults = {
        "llm": LLMConfig(api_key="test-key", max_steps_per_turn=10),
        "history": {"enabled": True, "db_path": "~/.coding-agent/history.db"},
        "security": {
            "confirm_dangerous": True,
            "log_safety_events": False,
            "allow_outside_workspace": False,
        },
    }
    defaults.update(overrides)
    return Config(**defaults)


def _make_repl(
    tmp_path: Path,
    inputs: list[str],
    llm: MockLLM | None = None,
    workspace: Path | str | None = None,
    history: HistoryManager | None = None,
    config: Config | None = None,
) -> tuple[REPL, io.StringIO]:
    workspace = workspace or tmp_path
    config = config or _make_config()
    input_iter = iter(inputs)

    def input_func(prompt: str = "") -> str:
        return next(input_iter)

    output = io.StringIO()
    console = Console(file=output, color_system=None)
    repl = REPL(
        workspace=str(workspace),
        config=config,
        llm_client=llm,
        console=console,
        input_func=input_func,
        history_manager=history,
    )
    return repl, output


# ---------------------------------------------------------------------------
# 基础 REPL 行为
# ---------------------------------------------------------------------------


def test_repl_direct_response(tmp_path):
    llm = MockLLM(responses=[AssistantResponse(content="你好！")])
    repl, output = _make_repl(tmp_path, inputs=["hello", "exit"], llm=llm)

    repl.run()

    assert "你好！" in output.getvalue()
    assert repl.messages[-1].role == "assistant"
    assert repl.messages[-1].content == "你好！"


def test_repl_exit_by_command(tmp_path):
    """支持 exit / quit 退出。"""
    for cmd in ("exit", "quit"):
        llm = MockLLM(responses=[])
        repl, output = _make_repl(tmp_path, inputs=[cmd], llm=llm)
        repl.run()
        assert "再见" in output.getvalue()


def test_repl_saves_history(tmp_path, isolated_home):
    history = HistoryManager(str(tmp_path / "history.db"))
    config = _make_config(history={"enabled": True, "db_path": str(tmp_path / "history.db")})
    llm = MockLLM(responses=[AssistantResponse(content="收到")])

    repl, _ = _make_repl(
        tmp_path,
        inputs=["记住这句话", "exit"],
        llm=llm,
        history=history,
        config=config,
    )
    repl.run()

    messages = history.load_messages(repl.session_id)
    assert any(m.role == "user" and m.content == "记住这句话" for m in messages)
    assert any(m.role == "assistant" and m.content == "收到" for m in messages)


def test_repl_loads_existing_history(tmp_path, isolated_home):
    history = HistoryManager(str(tmp_path / "history.db"))
    session_id = history.get_or_create_session(str(tmp_path))
    history.save_message(session_id, Message(role="user", content="previous"))
    history.save_message(session_id, Message(role="assistant", content="prev reply"))

    config = _make_config(history={"enabled": True, "db_path": str(tmp_path / "history.db")})
    llm = MockLLM(responses=[AssistantResponse(content="ok")])

    repl, _ = _make_repl(tmp_path, inputs=["next", "exit"], llm=llm, history=history, config=config)
    repl.run()

    contents = [m.content for m in repl.messages]
    assert "previous" in contents
    assert "prev reply" in contents
    assert "next" in contents


# ---------------------------------------------------------------------------
# 工具调用循环
# ---------------------------------------------------------------------------


def test_repl_tool_call_loop(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(id="call-1", name="read_file", arguments={"path": "a.txt"})
                ],
            ),
            AssistantResponse(content="文件内容是 hello"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["read", "exit"], llm=llm)
    repl.run()

    assert llm.calls[0]["tools"] is not None
    assert any(t["function"]["name"] == "read_file" for t in llm.calls[0]["tools"])
    assert "文件内容是 hello" in output.getvalue()
    assert repl.messages[-1].role == "assistant"


def test_repl_max_steps_per_turn(tmp_path):
    config = _make_config(llm=LLMConfig(api_key="test-key", max_steps_per_turn=2))
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(id=f"call-{i}", name="list_directory", arguments={"path": "."})
                ],
            )
            for i in range(3)
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["go", "exit"], llm=llm, config=config)
    repl.run()

    assert llm.call_count == 2
    assert "最大" in output.getvalue() or "上限" in output.getvalue()


# ---------------------------------------------------------------------------
# 安全与确认
# ---------------------------------------------------------------------------


def test_repl_dangerous_shell_confirmed(tmp_path):
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo x > out.txt"},
                    )
                ],
            ),
            AssistantResponse(content="已完成"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["run", "y", "exit"], llm=llm)
    repl.run()

    assert (tmp_path / "out.txt").exists()
    assert (tmp_path / "out.txt").read_text(encoding="utf-8").strip() == "x"
    assert "危险操作" in output.getvalue()
    assert "已完成" in output.getvalue()


def test_repl_dangerous_shell_declined(tmp_path):
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo x > out.txt"},
                    )
                ],
            ),
            AssistantResponse(content="已跳过"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["run", "n", "exit"], llm=llm)
    repl.run()

    assert not (tmp_path / "out.txt").exists()
    assert "已跳过" in output.getvalue()


def test_repl_forbidden_shell_rejected_without_prompt(tmp_path):
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "sudo ls -la"},
                    )
                ],
            ),
            AssistantResponse(content="被拒绝"),
        ]
    )

    # 如果 REPL 对 forbidden 命令弹出确认，输入序列里没有 y，会抛出 StopIteration
    repl, output = _make_repl(tmp_path, inputs=["run", "exit"], llm=llm)
    repl.run()

    assert "forbidden" in output.getvalue().lower() or "禁止" in output.getvalue()
    assert "被拒绝" in output.getvalue()


def test_repl_dangerous_shell_always_allow(tmp_path):
    """输入 a/always 后，同类型危险操作不再询问。"""
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo x > out1.txt"},
                    )
                ],
            ),
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="execute_shell",
                        arguments={"command": "echo y > out2.txt"},
                    )
                ],
            ),
            AssistantResponse(content="全部完成"),
        ]
    )

    # 只提供一次确认输入 a，第二次危险操作不应再消耗输入
    repl, output = _make_repl(tmp_path, inputs=["run", "a", "exit"], llm=llm)
    repl.run()

    assert (tmp_path / "out1.txt").exists()
    assert (tmp_path / "out2.txt").exists()
    assert "全部完成" in output.getvalue()


def test_repl_dangerous_shell_invalid_input_loops(tmp_path):
    """无效输入时循环重问，直到收到有效选项。"""
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo x > out.txt"},
                    )
                ],
            ),
            AssistantResponse(content="已完成"),
        ]
    )

    repl, output = _make_repl(
        tmp_path, inputs=["run", "invalid", "yes", "exit"], llm=llm
    )
    repl.run()

    assert (tmp_path / "out.txt").exists()
    assert "无效输入" in output.getvalue()
    assert "已完成" in output.getvalue()


def test_repl_dangerous_shell_logs_safety_event(tmp_path, isolated_home):
    config = _make_config(
        security={
            "confirm_dangerous": True,
            "log_safety_events": True,
            "allow_outside_workspace": False,
        }
    )
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo x > out.txt"},
                    )
                ],
            ),
            AssistantResponse(content="已完成"),
        ]
    )

    repl, _ = _make_repl(tmp_path, inputs=["run", "y", "exit"], llm=llm, config=config)
    repl.run()

    log_path = isolated_home / ".coding-agent" / "safety.log"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "execute_shell"
    assert entry["classification"] == "dangerous"
    assert entry["confirmed"] is True
    assert entry["result"]["success"] is True


def test_repl_declined_dangerous_shell_logs_safety_event(tmp_path, isolated_home):
    config = _make_config(
        security={
            "confirm_dangerous": True,
            "log_safety_events": True,
            "allow_outside_workspace": False,
        }
    )
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo x > out.txt"},
                    )
                ],
            ),
            AssistantResponse(content="已跳过"),
        ]
    )

    repl, _ = _make_repl(tmp_path, inputs=["run", "n", "exit"], llm=llm, config=config)
    repl.run()

    log_path = isolated_home / ".coding-agent" / "safety.log"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "execute_shell"
    assert entry["classification"] == "dangerous"
    assert entry["confirmed"] is False


def test_repl_forbidden_shell_logs_safety_event(tmp_path, isolated_home):
    config = _make_config(
        security={
            "confirm_dangerous": True,
            "log_safety_events": True,
            "allow_outside_workspace": False,
        }
    )
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "sudo ls -la"},
                    )
                ],
            ),
            AssistantResponse(content="被拒绝"),
        ]
    )

    repl, _ = _make_repl(tmp_path, inputs=["run", "exit"], llm=llm, config=config)
    repl.run()

    log_path = isolated_home / ".coding-agent" / "safety.log"
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "execute_shell"
    assert entry["classification"] == "forbidden"
    assert entry["confirmed"] is None
    assert entry["result"]["success"] is False


def test_repl_safety_log_disabled(tmp_path, isolated_home):
    config = _make_config(
        security={
            "confirm_dangerous": True,
            "log_safety_events": False,
            "allow_outside_workspace": False,
        }
    )
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "echo x > out.txt"},
                    )
                ],
            ),
            AssistantResponse(content="已完成"),
        ]
    )

    repl, _ = _make_repl(tmp_path, inputs=["run", "y", "exit"], llm=llm, config=config)
    repl.run()

    log_path = isolated_home / ".coding-agent" / "safety.log"
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# ask_user 处理
# ---------------------------------------------------------------------------


def test_repl_ask_user_returns_answer_to_llm(tmp_path):
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="ask_user",
                        arguments={"question": "你叫什么名字？"},
                    )
                ],
            ),
            AssistantResponse(content="你好，Alice"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["ask", "Alice", "exit"], llm=llm)
    repl.run()

    assert "你叫什么名字？" in output.getvalue()
    assert "你好，Alice" in output.getvalue()
    tool_msgs = [m for m in repl.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "Alice" in tool_msgs[0].content


# ---------------------------------------------------------------------------
# 快捷命令
# ---------------------------------------------------------------------------


def test_repl_slash_help_and_model(tmp_path):
    llm = MockLLM(responses=[])
    repl, output = _make_repl(
        tmp_path, inputs=["/help", "/model", "/clear", "exit"], llm=llm
    )
    repl.run()

    out = output.getvalue()
    assert "/help" in out
    assert "/clear" in out
    assert "/model" in out
    assert "kimi-for-coding" in out


def test_repl_unknown_slash_command(tmp_path):
    llm = MockLLM(responses=[])
    repl, output = _make_repl(tmp_path, inputs=["/unknown", "exit"], llm=llm)
    repl.run()
    assert "未知命令" in output.getvalue()


# ---------------------------------------------------------------------------
# 入口与工具函数
# ---------------------------------------------------------------------------


def test_format_tool_result_success():
    result = ToolResult(success=True, output="ok", metadata={"x": 1})
    text = _format_tool_result(result)
    assert '"success": true' in text
    assert "ok" in text


def test_format_tool_result_failure():
    result = ToolResult(success=False, error="boom")
    text = _format_tool_result(result)
    assert '"success": false' in text
    assert "boom" in text


def test_main_entry_accepts_workspace(tmp_path, monkeypatch):
    runs = []

    def fake_run(self):
        runs.append(Path(self.workspace).resolve())

    monkeypatch.setattr(REPL, "run", fake_run)
    main([str(tmp_path)])

    assert len(runs) == 1
    assert runs[0] == Path(tmp_path).resolve()
