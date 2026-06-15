from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - handled by runtime dependency
    DDGS = None  # type: ignore[misc, assignment]


class WebSearchInput(BaseModel):
    query: str = Field(..., description="搜索关键词")
    max_results: int = Field(default=5, description="返回结果的最大数量")


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "使用 DuckDuckGo 搜索网络内容"
    input_schema = WebSearchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        query = input.get("query", "")
        max_results = input.get("max_results", 5)

        if DDGS is None:
            return ToolResult(
                success=False,
                error="ddgs package is not installed",
                output="",
                metadata={"results": []},
            )

        try:
            results = list(DDGS().text(keywords=query, max_results=max_results))
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"Web search failed: {exc}",
                output="",
                metadata={"results": []},
            )

        formatted: list[str] = []
        for item in results:
            title = item.get("title", "")
            href = item.get("href", "")
            body = item.get("body", "")
            formatted.append(f"Title: {title}\nURL: {href}\nSnippet: {body}")

        output = "\n\n".join(formatted)
        return ToolResult(
            success=True,
            output=output,
            metadata={"results": results, "count": len(results)},
        )
