from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    success: bool
    output: str | None = None
    error: str | None = None
    metadata: dict | None = None


class ToolContext(BaseModel):
    workspace: str
    config: dict = Field(default_factory=dict)
    db_path: str | None = Field(default=None, description="SQLite 数据库路径")
    conda_env: str | None = Field(default=None, description="Target conda env name")

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).resolve()


class BaseTool(ABC):
    name: str
    description: str
    input_schema: type[BaseModel]

    @abstractmethod
    def execute(self, input: dict, ctx: ToolContext) -> ToolResult: ...

    def execute_forced(self, input: dict, ctx: ToolContext) -> ToolResult:
        """Trusted entry point for callers that have already validated consent.

        Tools that need a separate forced path (e.g. execute_shell) should
        override this method. The default implementation delegates to execute.
        """
        return self.execute(input, ctx)
