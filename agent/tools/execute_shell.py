import os
import re
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import CommandClass, classify_shell_command
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000
SUMMARY_LENGTH = 1500

# Tokens that occasionally leak from the LLM's tool-call formatting into the
# generated shell command. They must be removed before execution.
_LEAKED_TOKENS = ("</invoke>", "</invoke", "<invoke>")


def _sanitize_command(command: str) -> str:
    """Strip leaked XML-like tokens from the end of generated commands."""
    cleaned = command.strip()
    for token in _LEAKED_TOKENS:
        while cleaned.endswith(token):
            cleaned = cleaned[: -len(token)].rstrip()
        cleaned = cleaned.replace(f"\n{token}", "\n")
        cleaned = cleaned.replace(f" {token}", " ")
    return cleaned.strip()


def _summarize_output(text: str, max_len: int = SUMMARY_LENGTH) -> str:
    """Return a concise summary of a long command output.

    For pytest-like output, keep the header and the failed-test/traceback tail.
    For generic output, keep the beginning and end.
    """
    if len(text) <= max_len:
        return text

    lines = text.splitlines()

    # Try to find pytest failure summary lines.
    failure_lines = [
        i
        for i, line in enumerate(lines)
        if re.search(r"FAILED|ERROR|failed|error", line, re.IGNORECASE)
    ]
    if failure_lines:
        # Keep header (first 20 lines) + tail around failures.
        head = lines[:20]
        tail_start = max(0, min(failure_lines) - 10)
        tail = lines[tail_start:]
        candidate = "\n".join(head + ["\n... (truncated) ...\n"] + tail)
        if len(candidate) <= max_len:
            return candidate
        # Still too long: keep only tail.
        tail = lines[max(0, len(lines) - 80) :]
        candidate = "\n".join(["... (truncated) ..."] + tail)
        if len(candidate) <= max_len:
            return candidate

    # Generic: keep beginning + end.
    half = max_len // 2
    return text[:half] + f"\n\n... ({len(text) - max_len} chars truncated) ...\n\n" + text[-half:]


class ExecuteShellInput(BaseModel):
    command: str = Field(..., description="要执行的 shell 命令")
    timeout: int = Field(default=30, description="超时时间（秒）")


class ExecuteShellTool(BaseTool):
    name = "execute_shell"
    description = (
        "执行 shell 命令。用于运行测试、编译、git diff、ls 等操作。"
        "不要用 execute_shell 来读文件（用 read_file）"
        "或编辑文件（用 str_replace_file）——sed/awk 容易出错。"
    )
    input_schema = ExecuteShellInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """Public entry point: dangerous commands always require confirmation.

        The ``_force`` key is ignored here so that an LLM cannot bypass the
        safety classification by injecting it into the arguments.
        """
        return self._execute(input, ctx, force=False)

    def execute_forced(self, input: dict, ctx: ToolContext) -> ToolResult:
        """Trusted entry point for callers that have already obtained consent."""
        return self._execute(input, ctx, force=True)

    def _execute(self, input: dict, ctx: ToolContext, *, force: bool) -> ToolResult:
        command = _sanitize_command(input.get("command", ""))
        timeout = input.get("timeout", 30)

        classification = classify_shell_command(command)
        if classification == CommandClass.FORBIDDEN:
            return ToolResult(
                success=False,
                error=f"Command classified as forbidden and will not be executed: '{command}'.",
            )
        # In SWE-bench mode, skip dangerous-command confirmation so test/repro
        # commands (python -c, cd, &&, etc.) can run without interactive consent.
        if (
            not force
            and classification == CommandClass.DANGEROUS
            and os.environ.get("CODING_AGENT_SWEBENCH_FORCE") == "1"
        ):
            force = True
        if classification == CommandClass.DANGEROUS and not force:
            return ToolResult(
                success=False,
                error=(
                    f"Command classified as dangerous and requires user confirmation: "
                    f"'{command}'. It was not executed."
                ),
            )

        env = os.environ.copy()
        if ctx.conda_env is not None:
            base_prefix = Path(os.environ.get("CONDA_PREFIX", Path.home() / "anaconda3"))
            env_bin = base_prefix / "envs" / ctx.conda_env / "bin"
            env["PATH"] = f"{env_bin}{os.pathsep}{env['PATH']}"

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=ctx.workspace_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else (exc.stderr or "")
            )
            output = _summarize_output(stdout)
            err = _summarize_output(stderr)
            full = (output + "\n[stderr]\n" + err).strip()
            return ToolResult(
                success=False,
                error=f"Command timed out after {timeout} seconds: '{command}'.\n{full}",
                metadata={
                    "returncode": -1,
                    "stdout": output,
                    "stderr": err,
                },
            )
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to execute command: {exc}")

        output = completed.stdout or ""
        stderr = completed.stderr or ""
        full_output = output
        if stderr:
            full_output = full_output + f"\n[stderr]\n{stderr}"

        summarized = _summarize_output(full_output)

        metadata = {"returncode": completed.returncode}
        if len(full_output) > MAX_OUTPUT_LENGTH:
            metadata["truncated"] = True
            metadata["original_length"] = len(full_output)

        if completed.returncode != 0:
            return ToolResult(
                success=False,
                error=summarized or f"Command exited with code {completed.returncode}",
                metadata=metadata,
            )

        return ToolResult(success=True, output=summarized, metadata=metadata)
