import pytest
from pydantic import ValidationError

from agent.config import load_config


def _write_user_config(home, content):
    user_dir = home / ".coding-agent"
    user_dir.mkdir()
    (user_dir / "config.toml").write_text(content)


def test_load_default_config(isolated_home):
    """无配置文件时，使用 pydantic 内置默认配置。"""
    config = load_config()
    assert config.llm.provider == "kimi"
    assert config.llm.model == "kimi-for-coding"
    assert config.llm.base_url == "https://api.kimi.com/coding/v1"
    assert config.llm.api_key == ""
    assert config.llm.max_steps_per_turn == 100
    assert config.llm.max_retries_per_step == 3
    assert config.history.enabled is True
    assert config.history.max_messages == 20
    assert config.security.confirm_dangerous is False
    assert config.output.theme == "default"


def test_user_config_merge(isolated_home):
    """用户配置存在时，与内置默认合并。"""
    _write_user_config(isolated_home, '[llm]\nprovider = "openai"\nmodel = "gpt-4o"\n')

    config = load_config()
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4o"
    # 未覆盖字段保持默认
    assert config.llm.base_url == "https://api.kimi.com/coding/v1"
    assert config.llm.max_steps_per_turn == 100
    assert config.history.max_messages == 20


def test_env_var_highest_priority(isolated_home, monkeypatch):
    """环境变量优先级高于用户配置文件。"""
    _write_user_config(isolated_home, '[llm]\nprovider = "openai"\nmodel = "gpt-4o"\n')
    monkeypatch.setenv("CODING_AGENT_LLM_PROVIDER", "kimi")

    config = load_config()
    assert config.llm.provider == "kimi"
    assert config.llm.model == "gpt-4o"


def test_coding_agent_config_env_path(isolated_home, monkeypatch):
    """CODING_AGENT_CONFIG 环境变量指定的文件优先级最高。"""
    custom_config = isolated_home / "custom.toml"
    custom_config.write_text('[llm]\nprovider = "openai"\nmodel = "custom-model"\n')
    monkeypatch.setenv("CODING_AGENT_CONFIG", str(custom_config))

    _write_user_config(isolated_home, '[llm]\nmodel = "user-model"\n')

    config = load_config()
    assert config.llm.provider == "openai"
    assert config.llm.model == "custom-model"


def test_partial_override(isolated_home):
    """用户只配置部分字段，其余保持默认。"""
    _write_user_config(isolated_home, '[llm]\nmodel = "gpt-4o"\n')

    config = load_config()
    assert config.llm.provider == "kimi"
    assert config.llm.model == "gpt-4o"
    assert config.llm.max_steps_per_turn == 100
    assert config.llm.max_retries_per_step == 3
    assert config.history.max_messages == 20


def test_nested_override(isolated_home):
    """嵌套字段只覆盖指定键。"""
    _write_user_config(isolated_home, "[llm]\nmax_steps_per_turn = 200\n")

    config = load_config()
    assert config.llm.max_steps_per_turn == 200
    assert config.llm.provider == "kimi"
    assert config.llm.model == "kimi-for-coding"
    assert config.llm.max_retries_per_step == 3


def test_invalid_provider(isolated_home):
    """无效 provider 触发校验错误。"""
    _write_user_config(isolated_home, '[llm]\nprovider = "x"\n')

    with pytest.raises(ValidationError):
        load_config()


def test_negative_steps(isolated_home):
    """max_steps_per_turn 为负数触发校验错误。"""
    _write_user_config(isolated_home, "[llm]\nmax_steps_per_turn = -1\n")

    with pytest.raises(ValidationError):
        load_config()


def test_negative_retries(isolated_home):
    """max_retries_per_step 为负数触发校验错误。"""
    _write_user_config(isolated_home, "[llm]\nmax_retries_per_step = -1\n")

    with pytest.raises(ValidationError):
        load_config()


def test_negative_max_messages(isolated_home):
    """history.max_messages 为负数触发校验错误。"""
    _write_user_config(isolated_home, "[history]\nmax_messages = -5\n")

    with pytest.raises(ValidationError):
        load_config()


def test_empty_env_var_ignored(isolated_home, monkeypatch):
    """空字符串环境变量视为未设置。"""
    monkeypatch.setenv("CODING_AGENT_LLM_PROVIDER", "")

    config = load_config()
    assert config.llm.provider == "kimi"


def test_api_key_env_priority(isolated_home, monkeypatch):
    """环境变量 API Key 优先级高于配置文件。"""
    _write_user_config(isolated_home, '[llm]\napi_key = "file-key"\n')
    monkeypatch.setenv("CODING_AGENT_LLM_API_KEY", "env-key")

    config = load_config()
    assert config.llm.api_key == "env-key"


def test_api_key_empty_env_uses_file(isolated_home, monkeypatch):
    """空字符串 API Key 环境变量时，使用配置文件中的 key。"""
    _write_user_config(isolated_home, '[llm]\napi_key = "file-key"\n')
    monkeypatch.setenv("CODING_AGENT_LLM_API_KEY", "")

    config = load_config()
    assert config.llm.api_key == "file-key"


def test_project_config_lowest_priority(isolated_home):
    """项目配置 config.toml 作为最低文件优先级。"""
    project_config = isolated_home / "config.toml"
    project_config.write_text('[llm]\nmodel = "project-model"\n')
    _write_user_config(isolated_home, '[llm]\nmodel = "user-model"\n')

    config = load_config()
    assert config.llm.model == "user-model"


def test_history_db_path_default_expanded(isolated_home):
    """默认 history.db_path 中的 ~ 被展开为当前 HOME。"""
    config = load_config()
    assert config.history.db_path == str(isolated_home / ".coding-agent" / "history.db")


def test_history_db_path_config_expanded(isolated_home):
    """配置文件中 history.db_path 的 ~ 被展开。"""
    _write_user_config(isolated_home, '[history]\ndb_path = "~/custom.db"\n')
    config = load_config()
    assert config.history.db_path == str(isolated_home / "custom.db")


def test_history_db_env_expands_tilde(isolated_home, monkeypatch):
    """环境变量 CODING_AGENT_HISTORY_DB 中的 ~ 被展开并覆盖配置文件。"""
    _write_user_config(isolated_home, '[history]\ndb_path = "~/file.db"\n')
    monkeypatch.setenv("CODING_AGENT_HISTORY_DB", "~/env.db")

    config = load_config()
    assert config.history.db_path == str(isolated_home / "env.db")


def test_invalid_toml_raises(isolated_home):
    """无效 TOML 文件应抛出 ValueError。"""
    config_file = isolated_home / "config.toml"
    config_file.write_text("invalid toml [[")

    with pytest.raises(ValueError, match="invalid TOML"):
        load_config()


def test_env_provider_validated(isolated_home, monkeypatch):
    """环境变量中的无效 provider 同样触发 Pydantic 校验。"""
    monkeypatch.setenv("CODING_AGENT_LLM_PROVIDER", "bad-provider")

    with pytest.raises(ValidationError):
        load_config()
