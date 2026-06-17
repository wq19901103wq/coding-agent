"""End-to-end tests for multi-agent worker subprocess."""

import json
import time
import uuid

from agent.config import Config
from agent.supervisor.models import GoalStatus
from agent.supervisor.supervisor import Supervisor


def test_real_worker_subprocess_executes_goal(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.py").write_text("print('hello')")

    responses_path = tmp_path / "responses.json"
    responses_path.write_text(
        json.dumps(
            [
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "name": "read_file",
                            "arguments": {"path": "hello.py"},
                        }
                    ],
                },
                {"content": "File contains hello"},
            ]
        )
    )

    db_path = tmp_path / "goals.db"
    socket_path = f"/tmp/ca_e2e_worker_{uuid.uuid4().hex[:8]}.sock"
    config = Config()

    supervisor = Supervisor(
        workspace=str(workspace),
        config=config,
        socket_address=socket_path,
        db_path=str(db_path),
    )
    supervisor.start()

    try:
        goal = supervisor.submit_goal(
            title="Read hello.py",
            description="Read the file",
            agent_role="coder",
        )

        # Override spawn_worker to pass --mock-responses to the real subprocess.
        def spawn_with_mock(socket_address: str, goal, cfg: Config):
            import subprocess
            import sys

            cmd = [
                sys.executable,
                "-m",
                "agent.worker.worker_main",
                "--socket",
                socket_address,
                "--workspace",
                supervisor.workspace,
                "--role",
                goal.agent_role,
                "--mock-responses",
                str(responses_path),
            ]
            return subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        supervisor._spawn_worker = spawn_with_mock
        supervisor.run_goal(goal.id)

        for _ in range(300):
            fetched = supervisor.persistence.get(goal.id)
            if fetched.status == GoalStatus.DONE:
                break
            time.sleep(0.05)

        fetched = supervisor.persistence.get(goal.id)
        assert fetched.status == GoalStatus.DONE
        assert "File contains hello" in (fetched.result_summary or "")
    finally:
        supervisor.stop()
