"""Minimal agent that runs bash commands inside a Docker container.

Inspired by mini-swe-agent: a single ``execute_shell`` tool, executed directly
in the official SWE-bench Docker image. This sidesteps the IPC + multi-tool +
conda-env fragility of the full coding-agent pipeline while keeping our own
LLMClient (retry, config) and Message schema.

Designed for SWE-bench evaluation where correctness matters more than feature
richness.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agent.llm.client import LLMClient
from agent.llm.schema import Message, ToolCall

if TYPE_CHECKING:
    import docker.models.containers  # type: ignore[import-untyped]

logger = logging.getLogger("agent.docker_bash")

# ---------------------------------------------------------------------------
# Prompt templates (adapted from mini-swe-agent, tuned for SWE-bench)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a helpful assistant that can interact with a computer via bash commands.
You are an expert software engineer working on fixing a bug in a repository.
"""

INSTANCE_PROMPT = """\
Please solve the following issue:

{problem_statement}

You can execute bash commands and edit files to implement the necessary changes.

## Recommended Workflow (do this step by step)

1. Analyze the codebase by finding and reading relevant files.
2. Create a script to reproduce the issue and confirm the bug.
3. Edit the source code to resolve the issue. Make the *smallest* possible change.
   Do NOT modify test files unless the issue explicitly requires it.
4. Verify your fix works by running your reproduction script again.
5. Run the existing tests that are relevant to ensure no regressions.
6. When done, submit by running: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
   (do not combine it with any other command).

## Rules

- Every response MUST include at least one bash tool call.
- Commands run in a subshell; `cd` and env vars do not persist across calls.
  Prefix with `cd /testbed && ...` when needed.
- The working directory is `/testbed`.
- You can view files with: `nl -ba <file> | sed -n '<start>,<end>p'`
- You can edit files with: `sed -i 's/old/new/' <file>` or `cat <<'EOF' > <file> ...`
- **NEVER run** `git checkout <branch>`, `git stash`, `git reset --hard`,
  or `git checkout <file>` to undo your changes — these will destroy your
  work. If a `sed` edit didn't work, just run another `sed` to fix it.
  You may use `git checkout <file>` ONLY to discard a broken edit before
  retrying, but never switch branches.
- **Do NOT modify test files.** Your changes to test files will be discarded
  during evaluation and may hide real problems. Only edit source code.
- When done, submit by running: `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT`
"""

# Extra prompt injected when the agent submits but regression tests fail.
REGRESSION_FEEDBACK = """\
Your submission was NOT accepted. A regression check found that {n_failed} \
previously-passing test(s) now FAIL after your changes:

{failed_tests}

You must fix these regressions. Either:
- Adjust your source code change so these tests pass again, OR
- If a test is genuinely obsolete due to your fix, explain why.

After fixing, submit again with: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT
You have {attempts_left} attempt(s) left before the current patch is submitted as-is.
"""

BASH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "execute_shell",
        "description": "Execute a bash command in the repository environment.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute.",
                }
            },
            "required": ["command"],
        },
    },
}

SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

# Observation: show head + tail when output is long (like mini-swe-agent).
_MAX_OBSERVATION = 10000
_HEAD_TAIL = 5000


def _format_observation(returncode: int, output: str) -> str:
    if len(output) < _MAX_OBSERVATION:
        return json.dumps({"returncode": returncode, "output": output}, ensure_ascii=False)
    head = output[:_HEAD_TAIL]
    tail = output[-_HEAD_TAIL:]
    elided = len(output) - _HEAD_TAIL * 2
    return json.dumps(
        {
            "returncode": returncode,
            "output_head": head,
            "output_tail": tail,
            "elided_chars": elided,
            "warning": "Output too long; middle section omitted.",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Docker shell executor
# ---------------------------------------------------------------------------


@dataclass
class DockerShell:
    """Run bash commands inside a Docker container."""

    container: "docker.models.containers.Container"
    workdir: str = "/testbed"
    timeout: int = 120

    def execute(self, command: str, *, allow_destructive: bool = False) -> tuple[int, str]:
        """Run *command* and return (returncode, combined_output).

        Destructive git commands that would discard the agent's work are
        blocked and return an error message instead of executing. The agent
        keeps doing ``git checkout <file>`` / ``git stash`` despite prompt
        warnings, which silently destroys its own edits and produces empty
        patches. Pass ``allow_destructive=True`` for setup/teardown commands.
        """
        if not allow_destructive:
            blocked_reason = self._check_destructive(command)
            if blocked_reason:
                return 1, blocked_reason
        result = self.container.exec_run(
            ["bash", "-c", command],
            workdir=self.workdir,
            demux=False,
        )
        raw = result.output
        if isinstance(raw, tuple):
            raw = b"".join(p or b"" for p in raw)
        output = (raw or b"").decode("utf-8", errors="replace")
        return result.exit_code, output

    # Commands that discard work-in-progress and lead to empty patches.
    _DESTRUCTIVE_PATTERNS = [
        ("git checkout ", "git checkout <branch> or <file>"),
        ("git stash", "git stash"),
        ("git reset --hard", "git reset --hard"),
        ("git clean -fd", "git clean -fd"),
    ]

    def _check_destructive(self, command: str) -> str:
        """Return an error message if *command* would destroy work, else ''."""
        cmd = command.strip()
        for pattern, desc in self._DESTRUCTIVE_PATTERNS:
            if pattern in cmd:
                return (
                    f"BLOCKED: '{desc}' would discard your changes and lead to "
                    "an empty patch. Edit the file again with sed instead of "
                    "reverting it."
                )
        return ""

    def get_diff(self) -> str:
        """Return all changes: tracked diffs + untracked file contents."""
        rc, status = self.execute("git status --short")
        logger.info("git status before diff:\n%s", status)
        rc, diff = self.execute("git diff")
        parts = [diff] if diff.strip() else []
        # Include untracked files the agent created (git diff misses these).
        rc, untracked = self.execute("git ls-files --others --exclude-standard")
        for f in untracked.strip().splitlines():
            f = f.strip()
            if f:
                rc, content = self.execute(f"cat {f}")
                parts.append(f"\n--- new file: {f} ---\n{content}")
        return "\n".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


@dataclass
class DockerBashAgent:
    """A minimal agent: LLM → bash-in-docker → observation → repeat."""

    llm: LLMClient
    shell: DockerShell
    problem_statement: str
    step_limit: int = 50
    wall_time_limit: int = 1200
    # Test names that must keep passing (from SWE-bench spec, officially allowed).
    # When non-empty, the agent's submission is verified against these before
    # being accepted; regressions are fed back so the agent can fix them.
    pass_to_pass: list[str] = field(default_factory=list)
    max_regression_fixes: int = 2  # how many times to bounce back on regression
    messages: list[Message] = field(default_factory=list)
    n_calls: int = 0
    submitted: bool = False
    _regression_attempts: int = 0

    def run(self) -> str:
        """Run the agent loop. Returns the collected git diff (patch)."""
        start = time.monotonic()
        self.messages = [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user",
                content=INSTANCE_PROMPT.format(problem_statement=self.problem_statement),
            ),
        ]

        while not self.submitted:
            if 0 < self.step_limit <= self.n_calls:
                logger.info("step limit %d reached", self.step_limit)
                break
            if 0 < self.wall_time_limit <= int(time.monotonic() - start):
                logger.info("wall time limit %ds reached", self.wall_time_limit)
                break
            self._step()

        patch = self.shell.get_diff()
        logger.info("agent finished: %d LLM calls, patch=%d bytes", self.n_calls, len(patch))
        return patch

    def _step(self) -> None:
        """One iteration: query LLM, execute tool calls, add observations."""
        self.n_calls += 1
        try:
            response = self.llm.chat(
                self.messages,
                tools=[BASH_TOOL_SCHEMA],
                temperature=0.0,
            )
        except Exception as exc:
            logger.error("LLM call failed: %s", exc)
            self.messages.append(Message(role="user", content=f"[LLM error: {exc}. Please retry.]"))
            return

        # Record assistant message
        assistant_msg = Message(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls or None,
        )
        self.messages.append(assistant_msg)

        if not response.tool_calls:
            # LLM didn't call any tool — nudge it.
            self.messages.append(
                Message(
                    role="user",
                    content=(
                        "You must issue at least one bash command. "
                        "If you are done, run: echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
                    ),
                )
            )
            return

        for call in response.tool_calls:
            self._execute_tool_call(call)

    def _execute_tool_call(self, call: ToolCall) -> None:
        command = call.arguments.get("command", "")
        logger.info("step %d: $ %s", self.n_calls, command[:200])

        if SUBMIT_MARKER in command:
            # Submission: verify no regressions before accepting.
            failed = self._check_regressions()
            if failed and self._regression_attempts < self.max_regression_fixes:
                self._regression_attempts += 1
                attempts_left = self.max_regression_fixes - self._regression_attempts
                feedback = REGRESSION_FEEDBACK.format(
                    n_failed=len(failed),
                    failed_tests="\n".join(f"  - {t}" for t in failed[:20]),
                    attempts_left=attempts_left,
                )
                logger.info(
                    "submission rejected: %d regressions (attempt %d/%d)",
                    len(failed),
                    self._regression_attempts,
                    self.max_regression_fixes,
                )
                self.messages.append(Message(role="tool", content=feedback, tool_call_id=call.id))
                return
            if failed:
                logger.info("submitting with %d regressions (no attempts left)", len(failed))
            self.submitted = True
            observation = "Task submitted. Thank you!"
        else:
            try:
                returncode, output = self.shell.execute(command)
                observation = _format_observation(returncode, output)
            except Exception as exc:  # noqa: BLE001
                logger.warning("command failed: %s", exc)
                observation = json.dumps({"returncode": -1, "error": str(exc)}, ensure_ascii=False)

        self.messages.append(
            Message(
                role="tool",
                content=observation,
                tool_call_id=call.id,
            )
        )

    def _check_regressions(self) -> list[str]:
        """Run pass_to_pass tests and return the names of those that fail.

        This mirrors what a human engineer does before committing: run the
        existing test suite and check nothing broke. Only the test *names*
        from the SWE-bench spec are used (officially allowed); no test patch
        content is read.
        """
        if not self.pass_to_pass:
            return []
        # Run the tests. Use -x to stop early on collection errors, and
        # --tb=no to keep output short (we only need pass/fail per test).
        test_args = " ".join(self.pass_to_pass[:50])  # cap to avoid huge commands
        rc, output = self.shell.execute(
            f"python -m pytest -x --tb=no -q {test_args} 2>&1 | tail -100",
            allow_destructive=False,
        )
        # Parse which tests failed from pytest output.
        failed: list[str] = []
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("FAILED"):
                # Format: "FAILED path::test_name - reason"
                name = line.split("::", 1)[-1].split(" - ")[0].split()[0] if "::" in line else ""
                if name:
                    failed.append(name)
        logger.info(
            "regression check: %d/%d pass_to_pass tests failed", len(failed), len(self.pass_to_pass)
        )
        return failed
