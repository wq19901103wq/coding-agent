"""Tests for REPL supervisor integration."""

from unittest.mock import MagicMock

from agent.repl import REPL


def test_repl_goals_add_command(tmp_path):
    config = MagicMock()
    config.security.confirm_dangerous = False
    config.history.enabled = False
    config.llm.max_steps_per_turn = 5
    config.history.db_path = None

    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
    )

    repl._handle_slash_command('/goals add "Fix bug" coder')

    goals = repl.supervisor.persistence.list_active()
    assert len(goals) == 1
    assert goals[0].title == "Fix bug"
    assert goals[0].agent_role == "coder"


def test_repl_agent_list_command(tmp_path, capsys):
    config = MagicMock()
    config.security.confirm_dangerous = False
    config.history.enabled = False
    config.llm.max_steps_per_turn = 5
    config.history.db_path = None

    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
    )

    repl._handle_slash_command("/agent list")
    output = repl.console.file.getvalue()
    assert "coder" in output


def test_repl_agent_switch_command(tmp_path):
    config = MagicMock()
    config.security.confirm_dangerous = False
    config.history.enabled = False
    config.llm.max_steps_per_turn = 5
    config.history.db_path = None

    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
    )

    repl._handle_slash_command("/agent architect")
    assert repl.current_role == "architect"


def test_repl_detects_complex_input(tmp_path):
    config = MagicMock()
    config.security.confirm_dangerous = False
    config.history.enabled = False
    config.llm.max_steps_per_turn = 5
    config.history.db_path = None

    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
    )

    assert repl._should_use_supervisor("帮我规划一个多文件重构方案")
    assert repl._should_use_supervisor("/goals add test")
    assert not repl._should_use_supervisor("hi")
