import requests
from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult

DEFAULT_MAX_LENGTH = 5000
DEFAULT_TIMEOUT = 10


class FetchUrlInput(BaseModel):
    url: str = Field(..., description="要抓取的网页 URL")
    max_length: int = Field(default=DEFAULT_MAX_LENGTH, description="返回内容的最大长度")
    timeout: int = Field(default=DEFAULT_TIMEOUT, description="请求超时时间（秒）")


class FetchUrlTool(BaseTool):
    name = "fetch_url"
    description = "抓取网页内容"
    input_schema = FetchUrlInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        url = input.get("url", "")
        max_length = input.get("max_length", DEFAULT_MAX_LENGTH)
        timeout = input.get("timeout", DEFAULT_TIMEOUT)

        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Failed to fetch URL: {exc}",
            )

        text = response.text
        metadata: dict | None = None
        if len(text) > max_length:
            original_length = len(text)
            text = text[:max_length]
            metadata = {
                "truncated": True,
                "original_length": original_length,
            }

        return ToolResult(success=True, output=text, metadata=metadata)
