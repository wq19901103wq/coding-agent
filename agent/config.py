import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


class LLMConfig(BaseModel):
    provider: str = "kimi"
    model: str = "kimi-for-coding"
    base_url: str = "https://api.kimi.com/coding/v1"
    api_key: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    stream: bool = True
    max_steps_per_turn: int = 100
    max_retries_per_step: int = 3
    system_prompt: str | None = None

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        if v not in ("kimi", "openai"):
            raise ValueError("provider must be 'kimi' or 'openai'")
        return v

    @field_validator("max_steps_per_turn")
    @classmethod
    def _validate_max_steps_per_turn(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_steps_per_turn must be >= 1")
        return v

    @field_validator("max_retries_per_step")
    @classmethod
    def _validate_max_retries_per_step(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_retries_per_step must be >= 0")
        return v


class SecurityConfig(BaseModel):
    confirm_dangerous: bool = True
    log_safety_events: bool = True
    allow_outside_workspace: bool = False


class HistoryConfig(BaseModel):
    enabled: bool = True
    db_path: str = "~/.coding-agent/history.db"
    max_messages: int = 20

    @field_validator("max_messages")
    @classmethod
    def _validate_max_messages(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_messages must be >= 0")
        return v


class OutputConfig(BaseModel):
    theme: str = "default"
    verbose: bool = False


class ContextConfig(BaseModel):
    max_tokens: int = 8000
    auto_compact: bool = False
    preserve_recent: int = 4

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(cls, v: int) -> int:
        if v < 100:
            raise ValueError("max_tokens must be >= 100")
        return v

    @field_validator("preserve_recent")
    @classmethod
    def _validate_preserve_recent(cls, v: int) -> int:
        if v < 1:
            raise ValueError("preserve_recent must be >= 1")
        return v


class MCPConfig(BaseModel):
    enabled: bool = False
    command: str = ""
    args: list[str] = Field(default_factory=list)


class Config(BaseModel):
    llm: LLMConfig = LLMConfig()
    security: SecurityConfig = SecurityConfig()
    history: HistoryConfig = HistoryConfig()
    context: ContextConfig = ContextConfig()
    mcp: MCPConfig = MCPConfig()
    output: OutputConfig = OutputConfig()


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid TOML in config file: {path}") from exc


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并两个字典，override 中的字段覆盖 base 中的同名字段。"""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _env_override_data() -> dict[str, Any]:
    """读取环境变量并返回嵌套覆盖字典（空字符串视为未设置）。"""
    import json

    overrides: dict[str, Any] = {}
    provider = os.getenv("CODING_AGENT_LLM_PROVIDER")
    if provider:
        overrides.setdefault("llm", {})["provider"] = provider
    model = os.getenv("CODING_AGENT_LLM_MODEL")
    if model:
        overrides.setdefault("llm", {})["model"] = model
    api_key = os.getenv("CODING_AGENT_LLM_API_KEY")
    if api_key:
        overrides.setdefault("llm", {})["api_key"] = api_key
    base_url = os.getenv("CODING_AGENT_LLM_BASE_URL")
    if base_url:
        overrides.setdefault("llm", {})["base_url"] = base_url
    headers = os.getenv("CODING_AGENT_LLM_HEADERS")
    if headers:
        try:
            overrides.setdefault("llm", {})["headers"] = json.loads(headers)
        except json.JSONDecodeError:
            pass
    stream = os.getenv("CODING_AGENT_LLM_STREAM")
    if stream is not None:
        overrides.setdefault("llm", {})["stream"] = stream.lower() in ("1", "true", "yes")
    db_path = os.getenv("CODING_AGENT_HISTORY_DB")
    if db_path:
        overrides.setdefault("history", {})["db_path"] = db_path

    context_max_tokens = os.getenv("CODING_AGENT_CONTEXT_MAX_TOKENS")
    if context_max_tokens:
        try:
            overrides.setdefault("context", {})["max_tokens"] = int(context_max_tokens)
        except ValueError:
            pass
    context_auto_compact = os.getenv("CODING_AGENT_CONTEXT_AUTO_COMPACT")
    if context_auto_compact:
        overrides.setdefault("context", {})["auto_compact"] = context_auto_compact.lower() in (
            "1",
            "true",
            "yes",
        )
    context_preserve_recent = os.getenv("CODING_AGENT_CONTEXT_PRESERVE_RECENT")
    if context_preserve_recent:
        try:
            overrides.setdefault("context", {})["preserve_recent"] = int(context_preserve_recent)
        except ValueError:
            pass

    return overrides


def load_config(config_path: str | None = None, workspace: str | None = None) -> Config:
    """加载配置。

    注意：``.env`` 文件在 ``agent.repl:main`` 中加载，优先级高于本函数。

    优先级（从高到低）：
    1. 环境变量（CODING_AGENT_LLM_*、CODING_AGENT_HISTORY_DB）
    2. 函数参数 ``config_path`` 或 ``CODING_AGENT_CONFIG`` 环境变量指定的文件
    3. ``~/.coding-agent/config.toml``
    4. ``workspace`` 目录下的 ``config.toml``（如果提供了 workspace）
    5. 当前工作目录下的 ``config.toml``
    6. 内置默认配置（pydantic 模型默认值）
    """
    data: dict[str, Any] = {}
    paths: list[Path] = []

    if config_path:
        paths.append(Path(config_path))
    else:
        env_config = os.getenv("CODING_AGENT_CONFIG")
        # 按文件优先级从低到高排列，后加载的覆盖先加载的
        paths.append(Path("config.toml").resolve())
        if workspace:
            paths.append(Path(workspace).resolve() / "config.toml")
        paths.append(Path.home() / ".coding-agent" / "config.toml")
        if env_config:
            paths.append(Path(env_config))

    for path in paths:
        loaded = _load_toml(path)
        data = _deep_merge(data, loaded)

    env_data = _env_override_data()
    data = _deep_merge(data, env_data)

    config = Config(**data)
    config.history.db_path = os.path.expanduser(config.history.db_path)
    return config
