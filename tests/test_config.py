from agent.config import load_config


def test_load_default_config():
    config = load_config()
    assert config.llm.provider == "kimi"
    assert config.llm.model == "kimi-for-coding"
    assert config.history.max_messages == 20
