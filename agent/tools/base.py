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

    @property
    def workspace_path(self) -> Path:
        return Path(self.workspace).resolve()


class BaseTool(ABC):
    name: str
    description: str
    input_schema: type[BaseModel]

    @abstractmethod
    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        ...
