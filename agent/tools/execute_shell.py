import subprocess

from pydantic import BaseModel, Field

from agent.safety import CommandClass, classify_shell_command
from agent.tools.base import BaseTool, ToolContext, ToolResult

MAX_OUTPUT_LENGTH = 5000


class ExecuteShellInput(BaseModel):
    command: str = Field(..., description="要执行的 shell 命令")
    timeout: int = Field(default=30, description="超时时间（秒）")


class ExecuteShellTool(BaseTool):
    name = "execute_shell"
    description = "执行 shell 命令"
    input_schema = ExecuteShellInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        command = input.get("command", "")
        timeout = input.get("timeout", 30)

        force = input.get("_force", False)
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

        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=ctx.workspace_path,
                capture_output=True,
                text=True,
                timeout=timeout,
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
            return ToolResult(
                success=False, error=f"Failed to execute command: {exc}"
            )

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
