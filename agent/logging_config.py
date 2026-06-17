"""项目日志配置。"""

import logging
import os
from pathlib import Path

DEFAULT_LOG_LEVEL = "INFO"


def setup_logging(level: str | None = None) -> None:
    """配置根日志记录器。

    日志写入 ~/.coding-agent/coding-agent.log，可选通过环境变量
    CODING_AGENT_LOG_LEVEL 控制级别。

    DEBUG 级别同时输出到 stderr，其他级别只写入文件。
    """
    raw_level = level or os.getenv("CODING_AGENT_LOG_LEVEL", DEFAULT_LOG_LEVEL) or DEFAULT_LOG_LEVEL
    effective_level = raw_level.upper()
    numeric_level = getattr(logging, effective_level, logging.INFO)

    log_dir = Path.home() / ".coding-agent"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "coding-agent.log"

    handlers: list[logging.Handler] = [logging.FileHandler(log_path, encoding="utf-8")]
    if effective_level == "DEBUG":
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )
