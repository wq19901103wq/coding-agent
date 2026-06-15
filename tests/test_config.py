import pytest
from pydantic import ValidationError

from agent.config import load_config


def _write_user_config(tmp_path, content):
    user_dir = tmp_path / ".coding-agent"
    user_dir.mkdir()
    (user_dir / "config.toml").write_text(content)


def test_load_default_config(monkeypatch, tmp_path):
    """无配置文件时，使用 pydantic 内置默认配置。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    config = load_config()
    assert config.llm.provider == "kimi"
    assert config.llm.model == "kimi-for-coding"
    assert config.llm.base_url == "https://api.kimi.com/coding/v1"
    assert config.llm.api_key == ""
    assert config.llm.max_steps_per_turn == 100
    assert config.llm.max_retries_per_step == 3
    assert config.history.enabled is True
    assert config.history.max_messages == 20
    assert config.security.confirm_dangerous is True
    assert config.output.theme == "default"


def test_user_config_merge(monkeypatch, tmp_path):
    """用户配置存在时，与内置默认合并。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\nprovider = "openai"\nmodel = "gpt-4o"\n')

    config = load_config()
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4o"
    # 未覆盖字段保持默认
    assert config.llm.base_url == "https://api.kimi.com/coding/v1"
    assert config.llm.max_steps_per_turn == 100
    assert config.history.max_messages == 20


def test_env_var_highest_priority(monkeypatch, tmp_path):
    """环境变量优先级高于用户配置文件。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\nprovider = "openai"\nmodel = "gpt-4o"\n')
    monkeypatch.setenv("CODING_AGENT_LLM_PROVIDER", "kimi")

    config = load_config()
    assert config.llm.provider == "kimi"
    assert config.llm.model == "gpt-4o"


def test_coding_agent_config_env_path(monkeypatch, tmp_path):
    """CODING_AGENT_CONFIG 环境变量指定的文件优先级最高。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    custom_config = tmp_path / "custom.toml"
    custom_config.write_text('[llm]\nprovider = "openai"\nmodel = "custom-model"\n')
    monkeypatch.setenv("CODING_AGENT_CONFIG", str(custom_config))

    _write_user_config(tmp_path, '[llm]\nmodel = "user-model"\n')

    config = load_config()
    assert config.llm.provider == "openai"
    assert config.llm.model == "custom-model"


def test_partial_override(monkeypatch, tmp_path):
    """用户只配置部分字段，其余保持默认。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\nmodel = "gpt-4o"\n')

    config = load_config()
    assert config.llm.provider == "kimi"
    assert config.llm.model == "gpt-4o"
    assert config.llm.max_steps_per_turn == 100
    assert config.llm.max_retries_per_step == 3
    assert config.history.max_messages == 20


def test_nested_override(monkeypatch, tmp_path):
    """嵌套字段只覆盖指定键。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\nmax_steps_per_turn = 200\n')

    config = load_config()
    assert config.llm.max_steps_per_turn == 200
    assert config.llm.provider == "kimi"
    assert config.llm.model == "kimi-for-coding"
    assert config.llm.max_retries_per_step == 3


def test_invalid_provider(monkeypatch, tmp_path):
    """无效 provider 触发校验错误。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\nprovider = "x"\n')

    with pytest.raises(ValidationError):
        load_config()


def test_negative_steps(monkeypatch, tmp_path):
    """max_steps_per_turn 为负数触发校验错误。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\nmax_steps_per_turn = -1\n')

    with pytest.raises(ValidationError):
        load_config()


def test_negative_retries(monkeypatch, tmp_path):
    """max_retries_per_step 为负数触发校验错误。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\nmax_retries_per_step = -1\n')

    with pytest.raises(ValidationError):
        load_config()


def test_negative_max_messages(monkeypatch, tmp_path):
    """history.max_messages 为负数触发校验错误。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[history]\nmax_messages = -5\n')

    with pytest.raises(ValidationError):
        load_config()


def test_empty_env_var_ignored(monkeypatch, tmp_path):
    """空字符串环境变量视为未设置。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODING_AGENT_LLM_PROVIDER", "")

    config = load_config()
    assert config.llm.provider == "kimi"


def test_api_key_env_priority(monkeypatch, tmp_path):
    """环境变量 API Key 优先级高于配置文件。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\napi_key = "file-key"\n')
    monkeypatch.setenv("CODING_AGENT_LLM_API_KEY", "env-key")

    config = load_config()
    assert config.llm.api_key == "env-key"


def test_api_key_empty_env_uses_file(monkeypatch, tmp_path):
    """空字符串 API Key 环境变量时，使用配置文件中的 key。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    _write_user_config(tmp_path, '[llm]\napi_key = "file-key"\n')
    monkeypatch.setenv("CODING_AGENT_LLM_API_KEY", "")

    config = load_config()
    assert config.llm.api_key == "file-key"


def test_project_config_lowest_priority(monkeypatch, tmp_path):
    """项目配置 config.toml 作为最低文件优先级。"""
    project_config = tmp_path / "config.toml"
    project_config.write_text('[llm]\nmodel = "project-model"\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_user_config(tmp_path, '[llm]\nmodel = "user-model"\n')

    config = load_config()
    assert config.llm.model == "user-model"
