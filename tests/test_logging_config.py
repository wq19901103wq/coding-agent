import logging
import tempfile
from pathlib import Path

from agent.logging_config import setup_logging


def test_setup_logging_creates_log_file(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("HOME", tmp_dir)
        monkeypatch.setenv("CODING_AGENT_LOG_LEVEL", "DEBUG")

        setup_logging()
        log = logging.getLogger("test")
        log.info("hello")

        log_path = Path(tmp_dir) / ".coding-agent" / "coding-agent.log"
        assert log_path.exists()
        content = log_path.read_text(encoding="utf-8")
        assert "hello" in content


def test_setup_logging_respects_env_level(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        monkeypatch.setenv("HOME", tmp_dir)
        monkeypatch.setenv("CODING_AGENT_LOG_LEVEL", "WARNING")

        setup_logging()
        log = logging.getLogger("test2")
        assert log.level == logging.WARNING or logging.getLogger().level == logging.WARNING
