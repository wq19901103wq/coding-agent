from pydantic import BaseModel, Field

from agent.tools.base import BaseTool, ToolContext, ToolResult

try:
    from ddgs import DDGS
except ImportError:  # pragma: no cover - handled by runtime dependency
    DDGS = None  # type: ignore[misc, assignment]


class WebSearchInput(BaseModel):
    query: str = Field(..., description="搜索关键词")
    max_results: int = Field(default=5, description="返回结果的最大数量")


OUTPUT_MAX_LENGTH = 5000


class WebSearchTool(BaseTool):
    name = "web_search"
    description = "网页搜索"
    input_schema = WebSearchInput

    def execute(self, input: dict, ctx: ToolContext) -> ToolResult:
        query = input.get("query", "")
        max_results = input.get("max_results", 5)

        if not query.strip():
            return ToolResult(
                success=False,
                error="Query cannot be empty",
                output="",
                metadata={"results": []},
            )

        if DDGS is None:
            return ToolResult(
                success=False,
                error="ddgs package is not installed",
                output="",
                metadata={"results": []},
            )

        try:
            results = list(DDGS().text(keywords=query, max_results=max_results))  # type: ignore[call-arg]
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
        metadata = {"results": results, "count": len(results)}
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
