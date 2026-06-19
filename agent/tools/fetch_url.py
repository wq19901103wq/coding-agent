import os

import requests
from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult

DEFAULT_MAX_LENGTH = 5000
DEFAULT_TIMEOUT = 10


class FetchUrlInput(BaseModel):
    url: str = Field(..., description="抓取网页 URL")
    max_length: int = Field(default=DEFAULT_MAX_LENGTH, description="返回内容的最大长度")
    timeout: int = Field(default=DEFAULT_TIMEOUT, description="请求超时时间（秒）")


class FetchUrlTool(BaseTool):
    name = "fetch_url"
    description = "抓取网页内容（通过 Moonshot Fetch API）"
    input_schema = FetchUrlInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        url = input.get("url", "")
        max_length = input.get("max_length", DEFAULT_MAX_LENGTH)
        timeout = input.get("timeout", 30)

        if not url:
            return ToolResult(
                success=False,
                error="URL cannot be empty",
            )

        api_key = os.getenv("CODING_AGENT_LLM_API_KEY", "")
        if not api_key:
            return ToolResult(
                success=False,
                error="LLM API key is not configured; required for Moonshot fetch",
            )

        base_url = os.getenv("CODING_AGENT_LLM_BASE_URL", "https://api.kimi.com/coding/v1")
        fetch_url = f"{base_url}/fetch"

        try:
            response = requests.post(
                fetch_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={"url": url},
                timeout=timeout,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Failed to fetch URL: {exc}",
            )

        if response.status_code != 200:
            return ToolResult(
                success=False,
                error=f"Failed to fetch URL. Status: {response.status_code}",
            )

        try:
            data = response.json()
            text = f"Title: {data.get('title', '')}\n\n{data.get('markdown', '')}"
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Failed to parse response: {exc}",
            )

        metadata: dict | None = None
        if len(text) > max_length:
            original_length = len(text)
            text = text[:max_length]
            metadata = {
                "truncated": True,
                "original_length": original_length,
            }

        return ToolResult(success=True, output=text, metadata=metadata)
