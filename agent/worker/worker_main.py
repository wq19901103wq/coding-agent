"""Entry point for worker subprocess."""

from __future__ import annotations

import argparse
import sys

from agent.config import Config, load_config
from agent.llm import LLMClient
from agent.logging_config import setup_logging
from agent.supervisor.role_loader import RoleLoader
from agent.worker.worker import Worker


def _load_config_from_args(args: argparse.Namespace) -> Config:
    """Load config from supervisor-provided snapshot, falling back to disk."""
    line = sys.stdin.readline()
    stripped = line.strip()
    if stripped:
        try:
            return Config.model_validate_json(stripped)
        except Exception:
            # Fall through to disk config if the snapshot is unreadable.
            pass
    return load_config(config_path=args.config, workspace=args.workspace)


def main() -> int:
    parser = argparse.ArgumentParser(description="coding-agent worker process")
    parser.add_argument("--socket", required=True, help="Supervisor IPC socket address")
    parser.add_argument("--workspace", required=True, help="Workspace directory")
    parser.add_argument("--role", default="coder", help="Agent role name")
    parser.add_argument("--config", default=None, help="Path to config file")
    parser.add_argument(
        "--mock-responses",
        default=None,
        help="Path to JSON file with canned LLM responses (testing only)",
    )
    args = parser.parse_args()

    setup_logging()
    config = _load_config_from_args(args)

    role = RoleLoader().get(args.role)
    if role.model:
        config.llm.model = role.model

    if args.mock_responses:
        from agent.worker.mock_llm import MockLLMClient

        llm_client: LLMClient = MockLLMClient(args.mock_responses)
    else:
        llm_client = LLMClient(config.llm)

    worker = Worker(
        socket_address=args.socket,
        workspace=args.workspace,
        llm_client=llm_client,
        role=role,
    )
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
