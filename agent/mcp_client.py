"""MCP（Model Context Protocol）客户端封装。

提供同步接口连接外部 MCP server，并将其工具注册到 agent 工具集中。
"""

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
else:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        pass

_MCP_AVAILABLE = "StdioServerParameters" in globals()


class _MCPNotInstalledError(RuntimeError):
    """MCP 包未安装时抛出。"""


def _ensure_mcp_installed() -> None:
    if not _MCP_AVAILABLE:
        raise _MCPNotInstalledError("MCP 功能需要安装 'mcp' 包。请运行：pip install mcp")


class MCPClient:
    """基于 stdio 的 MCP 客户端同步包装。"""

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None):
        _ensure_mcp_installed()
        self.params: Any = StdioServerParameters(command=command, args=args, env=env)
        self._session: Any = None
        self._streams: Any = None
        self.tools: list[Any] = []

    async def _connect(self) -> None:
        self._streams = await stdio_client(self.params).__aenter__()
        read, write = self._streams
        self._session = await ClientSession(read, write).__aenter__()
        await self._session.initialize()
        result = await self._session.list_tools()
        self.tools = result.tools

    def connect(self) -> None:
        """建立与 MCP server 的连接并加载工具列表。"""
        asyncio.run(self._connect())

    async def _disconnect(self) -> None:
        if self._session:
            await self._session.__aexit__(None, None, None)
        if self._streams:
            await self._streams.__aexit__(None, None, None)
        self._session = None
        self._streams = None

    def disconnect(self) -> None:
        """断开与 MCP server 的连接。"""
        asyncio.run(self._disconnect())

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("MCP client is not connected")
        return await self._session.call_tool(name, arguments)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """同步调用 MCP server 上的工具。"""
        return asyncio.run(self._call_tool(name, arguments))
