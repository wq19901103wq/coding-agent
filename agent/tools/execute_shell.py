import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from agent.safety import CommandClass, classify_shell_command
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000

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


class ExecuteShellInput(BaseModel):
    command: str = Field(..., description="要执行的 shell 命令")
    timeout: int = Field(default=30, description="超时时间（秒）")


class ExecuteShellTool(BaseTool):
    name = "execute_shell"
    description = "执行 shell 命令"
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
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            return ToolResult(
                success=False,
                error=f"Command timed out after {timeout} seconds: '{command}'.",
                metadata={
                    "returncode": -1,
                    "stdout": stdout[:MAX_OUTPUT_LENGTH],
                    "stderr": stderr[:MAX_OUTPUT_LENGTH],
                },
            )
        except OSError as exc:
            return ToolResult(success=False, error=f"Failed to execute command: {exc}")

        output = completed.stdout or ""
        if completed.stderr:
            output = output + f"\n[stderr]\n{completed.stderr}"

        if len(output) > MAX_OUTPUT_LENGTH:
            original_length = len(output)
            output = output[:MAX_OUTPUT_LENGTH]
            metadata = {
                "truncated": True,
                "original_length": original_length,
                "returncode": completed.returncode,
            }
        else:
            metadata = {"returncode": completed.returncode}

        if completed.returncode != 0:
            return ToolResult(
                success=False,
                error=output or f"Command exited with code {completed.returncode}",
                metadata=metadata,
            )

        return ToolResult(success=True, output=output, metadata=metadata)
