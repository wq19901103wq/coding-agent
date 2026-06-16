from unittest.mock import MagicMock

from agent.indexing import Indexer
from agent.repl import REPL


def test_repl_builds_index(tmp_path):
    (tmp_path / "math.py").write_text("def square(x):\n    return x * x\n", encoding="utf-8")

    db_path = tmp_path / "index.db"
    config = MagicMock()
    config.security.confirm_dangerous = False
    config.history.enabled = False
    config.llm.max_steps_per_turn = 1
    config.history.db_path = str(db_path)
    config.model_dump.return_value = {}

    REPL(
        workspace=str(tmp_path),
        config=config,
        llm_client=MagicMock(),
        input_func=lambda _: "exit",
    )

    # 索引应包含 square 符号
    indexer = Indexer(str(tmp_path), str(db_path))
    symbols = indexer.search_symbols("square")
    assert len(symbols) == 1
    assert symbols[0].name == "square"
