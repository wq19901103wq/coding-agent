from unittest.mock import MagicMock

from agent.repl import REPL


def test_apply_patch_triggers_confirmation(tmp_path):
    config = MagicMock()
    config.security.confirm_dangerous = True
    config.history.enabled = False
    config.llm.max_steps_per_turn = 1
    config.history.db_path = None
    config.model_dump.return_value = {}

    inputs = iter(["n"])

    def fake_input(prompt: str = "") -> str:
        return str(next(inputs))

    repl = REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
        input_func=fake_input,
    )

    from agent.llm.schema import ToolCall

    call = ToolCall(
        id="1",
        name="apply_patch",
        arguments={"diff": "--- a\n+++ a\n@@ -1 +1 @@\n-x\n+y\n"},
    )
    result = repl._execute_tool_call(call)

    assert not result.success
    assert "User declined" in result.error
