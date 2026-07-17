import ipaddress
import os
from urllib.parse import urlsplit

import requests
from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult

DEFAULT_MAX_LENGTH = 5000
DEFAULT_TIMEOUT = 10


def _validate_public_url(url: str) -> str | None:
    """Reject URLs that obviously target local or private services."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "URL is malformed"
    if parsed.scheme not in {"http", "https"}:
        return "Only http and https URLs are allowed"
    if not hostname:
        return "URL must include a hostname"
    if parsed.username is not None or parsed.password is not None:
        return "URLs containing credentials are not allowed"
    if port is not None and not (1 <= port <= 65535):
        return "URL port is invalid"

    normalized = hostname.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith((".localhost", ".local", ".internal")):
        return "Local and private network hosts are not allowed"
    if normalized.replace(".", "").isdigit() or normalized.startswith("0x"):
        return "Ambiguous numeric hostnames are not allowed"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return None
    if not address.is_global:
        return "Local and private network addresses are not allowed"
    return None


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
        validation_error = _validate_public_url(url)
        if validation_error:
            return ToolResult(success=False, error=validation_error)

        api_key = os.getenv("CODING_AGENT_LLM_API_KEY", "")
        if not api_key:
            return ToolResult(
                success=False,
                error="LLM API key is not configured; required for Moonshot fetch",
            )

        base_url = os.getenv("CODING_AGENT_LLM_BASE_URL", "https://api.kimi.com/coding/v1")
        # The /fetch endpoint is a Moonshot/Kimi-specific extension. Other
        # OpenAI-compatible providers (Volces, DeepSeek, etc.) do not implement
        # it, so calling it would just 404. Fail fast with a clear message.
        if "api.kimi.com" not in base_url:
            return ToolResult(
                success=False,
                error=(
                    "fetch_url requires the Kimi/Moonshot API (api.kimi.com). "
                    f"Current base_url '{base_url}' does not support this endpoint. "
                    "Switch to Kimi provider or use a different method to fetch the URL."
                ),
            )
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
