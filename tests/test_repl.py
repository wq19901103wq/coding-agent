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
from tests.conftest import MockLLM


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
    return Config(**defaults)  # type: ignore[arg-type]


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
        llm_client=llm,  # type: ignore[arg-type]
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


def test_repl_confirm_dangerous_disabled(tmp_path):
    """security.confirm_dangerous=False 时直接执行危险操作，不询问确认。"""
    config = _make_config(
        security={
            "confirm_dangerous": False,
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

    repl, output = _make_repl(tmp_path, inputs=["run", "exit"], llm=llm, config=config)
    repl.run()

    assert (tmp_path / "out.txt").exists()
    assert "危险操作" not in output.getvalue()
    assert "已完成" in output.getvalue()


# ---------------------------------------------------------------------------
# ask_user 处理
# ---------------------------------------------------------------------------


def test_repl_write_file_confirmed(tmp_path):
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={"path": "a.py", "content": "x=1"},
                    )
                ],
            ),
            AssistantResponse(content="已写入"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["write", "y", "exit"], llm=llm)
    repl.run()

    assert (tmp_path / "a.py").exists()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x=1"
    assert "危险操作" in output.getvalue()
    assert "已写入" in output.getvalue()


def test_repl_write_file_declined(tmp_path):
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={"path": "a.py", "content": "x=1"},
                    )
                ],
            ),
            AssistantResponse(content="已跳过"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["write", "n", "exit"], llm=llm)
    repl.run()

    assert not (tmp_path / "a.py").exists()
    assert "已跳过" in output.getvalue()


def test_repl_write_file_always_allow(tmp_path):
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={"path": "a.py", "content": "x=1"},
                    )
                ],
            ),
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="write_file",
                        arguments={"path": "b.py", "content": "y=2"},
                    )
                ],
            ),
            AssistantResponse(content="全部完成"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["write", "a", "exit"], llm=llm)
    repl.run()

    assert (tmp_path / "a.py").exists()
    assert (tmp_path / "b.py").exists()
    assert "全部完成" in output.getvalue()


def test_repl_str_replace_file_confirmed(tmp_path):
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="str_replace_file",
                        arguments={"path": "a.py", "old_str": "x=1", "new_str": "x=2"},
                    )
                ],
            ),
            AssistantResponse(content="已替换"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["replace", "y", "exit"], llm=llm)
    repl.run()

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x=2\n"
    assert "危险操作" in output.getvalue()
    assert "已替换" in output.getvalue()


def test_repl_str_replace_file_declined(tmp_path):
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    llm = MockLLM(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="str_replace_file",
                        arguments={"path": "a.py", "old_str": "x=1", "new_str": "x=2"},
                    )
                ],
            ),
            AssistantResponse(content="已跳过"),
        ]
    )

    repl, output = _make_repl(tmp_path, inputs=["replace", "n", "exit"], llm=llm)
    repl.run()

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x=1\n"
    assert "已跳过" in output.getvalue()


def test_repl_startup_prints_pending_todos(tmp_path, isolated_home):
    history = HistoryManager(str(tmp_path / "history.db"))
    session_id = history.get_or_create_session(str(tmp_path))
    history.create_todo(session_id, "待办一")
    history.create_todo(session_id, "待办二")
    history.complete_todo(history.list_todos(session_id)[0]["id"])

    config = _make_config(history={"enabled": True, "db_path": str(tmp_path / "history.db")})
    llm = MockLLM(responses=[])
    repl, output = _make_repl(
        tmp_path, inputs=["exit"], llm=llm, history=history, config=config
    )
    repl.run()

    out = output.getvalue()
    assert "待办" in out or "todo" in out.lower()
    assert "待办二" in out
    assert "待办一" not in out


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
# 端到端场景
# ---------------------------------------------------------------------------


def test_repl_end_to_end_write_and_run_file(tmp_path, mock_llm):
    """完整流程：LLM 写文件并运行文件，结果回传给 LLM 后给出总结。"""
    script_path = "hello.py"
    script_content = 'print("hello from agent")'

    llm = mock_llm(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments={"path": script_path, "content": script_content},
                    )
                ],
            ),
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="execute_shell",
                        arguments={"command": "python3 hello.py"},
                    )
                ],
            ),
            AssistantResponse(content="已完成：文件已写入并成功运行"),
        ]
    )

    repl, output = _make_repl(
        tmp_path,
        inputs=["写一个 hello.py 并运行它", "y", "y", "exit"],
        llm=llm,
    )
    repl.run()

    # 验证文件已被正确写入
    target = tmp_path / script_path
    assert target.exists()
    assert target.read_text(encoding="utf-8") == script_content

    # 验证最终总结输出到控制台
    assert "已完成：文件已写入并成功运行" in output.getvalue()

    # 验证工具执行结果已回传给 LLM
    tool_msgs = [m for m in repl.messages if m.role == "tool"]
    assert len(tool_msgs) == 2

    write_result = json.loads(tool_msgs[0].content)
    assert write_result["success"] is True
    assert script_path in write_result["output"]

    run_result = json.loads(tool_msgs[1].content)
    assert run_result["success"] is True
    assert "hello from agent" in run_result["output"]


# ---------------------------------------------------------------------------
# 快捷命令
# ---------------------------------------------------------------------------


def test_repl_slash_help_and_model(tmp_path):
    llm = MockLLM(responses=[])
    repl, output = _make_repl(
        tmp_path, inputs=["/help", "/model", "exit"], llm=llm
    )
    repl.run()

    out = output.getvalue()
    assert "/help" in out
    assert "/clear" in out
    assert "/model" in out
    assert "kimi-for-coding" in out


def test_repl_slash_clear_clears_history(tmp_path, isolated_home):
    history = HistoryManager(str(tmp_path / "history.db"))
    session_id = history.get_or_create_session(str(tmp_path))
    history.save_message(session_id, Message(role="user", content="previous"))
    history.save_message(session_id, Message(role="assistant", content="prev reply"))

    config = _make_config(history={"enabled": True, "db_path": str(tmp_path / "history.db")})
    llm = MockLLM(responses=[])
    repl, output = _make_repl(
        tmp_path, inputs=["/clear", "exit"], llm=llm, history=history, config=config
    )
    repl.run()

    assert history.load_messages(session_id) == []
    # 保留 system prompt
    assert len(repl.messages) == 1
    assert repl.messages[0].role == "system"


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


# ---------------------------------------------------------------------------
# 复杂端到端场景
# ---------------------------------------------------------------------------


def test_repl_end_to_end_read_modify_run(tmp_path, mock_llm):
    """读-改-跑闭环：读取文件、局部替换、运行。"""
    (tmp_path / "calc.py").write_text("print(1 + 1)", encoding="utf-8")

    llm = mock_llm(
        responses=[
            # 第 1 步：读取文件
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "calc.py"},
                    )
                ],
            ),
            # 第 2 步：局部替换
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="str_replace_file",
                        arguments={
                            "path": "calc.py",
                            "old_str": "print(1 + 1)",
                            "new_str": "print(1 + 2)",
                        },
                    )
                ],
            ),
            # 第 3 步：运行
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-3",
                        name="execute_shell",
                        arguments={"command": "python3 calc.py"},
                    )
                ],
            ),
            AssistantResponse(content="已读取、修改并运行，输出为 3"),
        ]
    )

    repl, output = _make_repl(
        tmp_path,
        inputs=["把 calc.py 改成输出 1+2，然后运行", "y", "y", "exit"],
        llm=llm,
    )
    repl.run()

    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == "print(1 + 2)"
    assert "已读取、修改并运行" in output.getvalue()

    tool_msgs = [m for m in repl.messages if m.role == "tool"]
    assert len(tool_msgs) == 3
    run_result = json.loads(tool_msgs[2].content)
    assert run_result["success"] is True
    assert "3" in run_result["output"]


def test_repl_end_to_end_ask_user_then_write(tmp_path, mock_llm):
    """ask_user 交互：询问文件名后创建并运行。"""
    llm = mock_llm(
        responses=[
            # 第 1 步：询问文件名
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="ask_user",
                        arguments={"question": "请提供文件名"},
                    )
                ],
            ),
            # 第 2 步：创建文件
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="write_file",
                        arguments={
                            "path": "user_script.py",
                            "content": "print('from user')",
                        },
                    )
                ],
            ),
            # 第 3 步：运行
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-3",
                        name="execute_shell",
                        arguments={"command": "python3 user_script.py"},
                    )
                ],
            ),
            AssistantResponse(content="已按你要求创建并运行"),
        ]
    )

    repl, output = _make_repl(
        tmp_path,
        inputs=["帮我创建一个 Python 脚本并运行", "user_script.py", "y", "y", "exit"],
        llm=llm,
    )
    repl.run()

    assert (tmp_path / "user_script.py").read_text(encoding="utf-8") == "print('from user')"
    assert "已按你要求创建并运行" in output.getvalue()

    tool_msgs = [m for m in repl.messages if m.role == "tool"]
    ask_result = json.loads(tool_msgs[0].content)
    assert "user_script.py" in ask_result["output"]


def test_repl_end_to_end_todo_management(tmp_path, mock_llm, isolated_home):
    """todo 管理：创建多个 todo，完成一个，列出所有。"""
    config = _make_config(history={"enabled": True, "db_path": str(isolated_home / "history.db")})
    llm = mock_llm(
        responses=[
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="set_todo",
                        arguments={"action": "create", "id": "todo-1", "title": "实现 read_file"},
                    )
                ],
            ),
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="set_todo",
                        arguments={"action": "create", "id": "todo-2", "title": "实现 write_file"},
                    )
                ],
            ),
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-3",
                        name="set_todo",
                        arguments={
                            "action": "update",
                            "id": "todo-1",
                            "status": "done",
                        },
                    )
                ],
            ),
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-4",
                        name="set_todo",
                        arguments={"action": "list"},
                    )
                ],
            ),
            AssistantResponse(content="todo 管理完成"),
        ]
    )

    repl, output = _make_repl(
        tmp_path,
        inputs=["创建两个 todo，把第一个标记完成，然后列出所有 todo", "exit"],
        llm=llm,
        config=config,
    )
    repl.run()

    assert "todo 管理完成" in output.getvalue()

    todos = repl.history.list_todos(repl.session_id)
    assert len(todos) == 2
    assert todos[0]["title"] == "实现 read_file"
    assert todos[0]["status"] == "done"
    assert todos[1]["title"] == "实现 write_file"
    assert todos[1]["status"] == "pending"


def test_repl_end_to_end_forbidden_then_recovery(tmp_path, mock_llm):
    """forbidden 命令被拒绝后，后续 harmless 命令仍可正常执行。"""
    llm = mock_llm(
        responses=[
            # 第 1 步：forbidden 命令
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_shell",
                        arguments={"command": "sudo rm -rf /tmp/should_not_run"},
                    )
                ],
            ),
            # 第 2 步：harmless 命令恢复
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="execute_shell",
                        arguments={"command": "echo recovered"},
                    )
                ],
            ),
            AssistantResponse(content="forbidden 已拒绝，后续命令执行成功"),
        ]
    )

    repl, output = _make_repl(
        tmp_path,
        inputs=["先运行 sudo rm，再运行 echo", "exit"],
        llm=llm,
    )
    repl.run()

    assert "forbidden" in output.getvalue().lower() or "禁止" in output.getvalue()
    assert "forbidden 已拒绝，后续命令执行成功" in output.getvalue()

    tool_msgs = [m for m in repl.messages if m.role == "tool"]
    assert len(tool_msgs) == 2
    forbidden_result = json.loads(tool_msgs[0].content)
    assert forbidden_result["success"] is False
    harmless_result = json.loads(tool_msgs[1].content)
    assert harmless_result["success"] is True
    assert "recovered" in harmless_result["output"]


def test_repl_end_to_end_search_and_read(tmp_path, mock_llm):
    """搜索代码后读取匹配文件并修改。"""
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    pass\n", encoding="utf-8")

    llm = mock_llm(
        responses=[
            # 第 1 步：搜索所有函数定义
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="code_search",
                        arguments={"pattern": "^def "},
                    )
                ],
            ),
            # 第 2 步：读取 a.py
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="read_file",
                        arguments={"path": "a.py"},
                    )
                ],
            ),
            # 第 3 步：修改 a.py
            AssistantResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-3",
                        name="str_replace_file",
                        arguments={
                            "path": "a.py",
                            "old_str": "def foo():\n    pass",
                            "new_str": "def foo2():\n    pass",
                        },
                    )
                ],
            ),
            AssistantResponse(content="已搜索并修改 a.py"),
        ]
    )

    repl, output = _make_repl(
        tmp_path,
        inputs=["搜索所有函数定义，把 a.py 里的 foo 改成 foo2", "y", "exit"],
        llm=llm,
    )
    repl.run()

    assert "已搜索并修改 a.py" in output.getvalue()
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "def foo2():\n    pass\n"

    tool_msgs = [m for m in repl.messages if m.role == "tool"]
    assert len(tool_msgs) == 3
    search_result = json.loads(tool_msgs[0].content)
    assert "a.py" in search_result["output"]
    assert "b.py" in search_result["output"]
