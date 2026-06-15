import os
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib


class LLMConfig(BaseModel):
    provider: str = "kimi"
    model: str = "kimi-for-coding"
    base_url: str = "https://api.kimi.com/coding/v1"
    api_key: str = ""
    max_steps_per_turn: int = 100
    max_retries_per_step: int = 3


class SecurityConfig(BaseModel):
    confirm_dangerous: bool = True
    log_safety_events: bool = True
    allow_outside_workspace: bool = False


class HistoryConfig(BaseModel):
    enabled: bool = True
    db_path: str = "~/.coding-agent/history.db"
    max_messages: int = 20


class OutputConfig(BaseModel):
    theme: str = "default"
    verbose: bool = False


class Config(BaseModel):
    llm: LLMConfig = LLMConfig()
    security: SecurityConfig = SecurityConfig()
    history: HistoryConfig = HistoryConfig()
    output: OutputConfig = OutputConfig()

    @field_validator("llm")
    @classmethod
    def validate_provider(cls, v):
        if v.provider not in ("kimi", "openai"):
            raise ValueError("provider must be 'kimi' or 'openai'")
        if v.max_steps_per_turn < 1:
            raise ValueError("max_steps_per_turn must be >= 1")
        return v


def _load_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def _apply_env(config: Config) -> Config:
    if os.getenv("CODING_AGENT_LLM_PROVIDER"):
        config.llm.provider = os.getenv("CODING_AGENT_LLM_PROVIDER")
    if os.getenv("CODING_AGENT_LLM_MODEL"):
        config.llm.model = os.getenv("CODING_AGENT_LLM_MODEL")
    if os.getenv("CODING_AGENT_LLM_API_KEY"):
        config.llm.api_key = os.getenv("CODING_AGENT_LLM_API_KEY")
    if os.getenv("CODING_AGENT_LLM_BASE_URL"):
        config.llm.base_url = os.getenv("CODING_AGENT_LLM_BASE_URL")
    if os.getenv("CODING_AGENT_HISTORY_DB"):
        config.history.db_path = os.getenv("CODING_AGENT_HISTORY_DB")
    return config


def load_config(config_path: str | None = None) -> Config:
    data = {}
    if config_path:
        data = _load_toml(Path(config_path))
    else:
        user_config = Path.home() / ".coding-agent" / "config.toml"
        project_config = Path("config.toml")
        default_config = Path(__file__).parent.parent / "config.toml"
        for path in [default_config, project_config, user_config]:
            data = {**data, **_load_toml(path)}

    config = Config(**data)
    config = _apply_env(config)
    return config
