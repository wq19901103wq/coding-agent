import logging
import os

import requests
from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult

logger = logging.getLogger("agent.tools.web_search")

OUTPUT_MAX_LENGTH = 5000


class WebSearchInput(BaseModel):
    query: str = Field(..., description="搜索关键词")
    max_results: int = Field(default=5, description="返回结果的最大数量")


class SearchResult(BaseModel):
    site_name: str
    title: str
    url: str
    snippet: str
    content: str = ""
    date: str = ""
    icon: str = ""
    mime: str = ""


class SearchResponse(BaseModel):
    search_results: list[SearchResult]


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "网页搜索（通过 Moonshot Search API）"
    input_schema = WebSearchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        """使用 Moonshot Search API 搜索网页。"""
        query = input.get("query", "").strip()
        max_results = input.get("max_results", 5)

        if not query:
            return ToolResult(
                success=False,
                error="Query cannot be empty",
                output="",
                metadata={"results": []},
            )

        api_key = os.getenv("CODING_AGENT_LLM_API_KEY", "")
        if not api_key:
            return ToolResult(
                success=False,
                error="LLM API key is not configured; required for Moonshot search",
                output="",
                metadata={"results": []},
            )

        base_url = os.getenv("CODING_AGENT_LLM_BASE_URL", "https://api.kimi.com/coding/v1")
        search_url = f"{base_url}/search"

        try:
            response = requests.post(
                search_url,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "text_query": query,
                    "limit": max_results,
                    "enable_page_crawling": False,
                    "timeout_seconds": 30,
                },
                timeout=60,
            )
        except Exception as exc:
            logger.warning("SearchWeb request failed: %s", exc)
            return ToolResult(
                success=False,
                error=f"Search request failed: {exc}",
                output="",
                metadata={"results": []},
            )

        if response.status_code != 200:
            logger.warning(
                "SearchWeb HTTP error: status=%s, query=%s",
                response.status_code,
                query,
            )
            return ToolResult(
                success=False,
                error=f"Failed to search. Status: {response.status_code}",
                output="",
                metadata={"results": []},
            )

        try:
            data = response.json()
            results = SearchResponse(**data).search_results
        except Exception as exc:
            logger.warning(
                "SearchWeb response parse error: %s, query=%s",
                exc,
                query,
            )
            return ToolResult(
                success=False,
                error=f"Failed to parse search results: {exc}",
                output="",
                metadata={"results": []},
            )

        formatted: list[str] = []
        for result in results:
            formatted.append(
                f"Title: {result.title}\n"
                f"Date: {result.date}\n"
                f"URL: {result.url}\n"
                f"Summary: {result.snippet}\n\n"
                f"{result.content}\n\n"
            )

        output = "\n---\n\n".join(formatted)
        metadata = {"results": [r.model_dump() for r in results], "count": len(results)}

        if len(output) > OUTPUT_MAX_LENGTH:
            original_length = len(output)
            output = output[:OUTPUT_MAX_LENGTH]
            metadata["truncated"] = True
            metadata["original_length"] = original_length

        return ToolResult(
            success=True,
            output=output,
            metadata=metadata,
        )
