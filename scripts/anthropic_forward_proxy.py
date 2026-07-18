"""Minimal Anthropic-protocol forwarder for Claude Code benchmarks.

DeepSeek exposes an Anthropic-compatible Messages API. Claude Code may still
apply its own authentication behavior for custom endpoints, so this local
forwarder replaces only the credential headers and streams the upstream
response unchanged. Secrets are read from the environment and never logged.
"""

from __future__ import annotations

import asyncio
import os

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

app = FastAPI()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["POST"])
async def forward(path: str, request: Request) -> Response:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        return Response("DEEPSEEK_API_KEY is not configured", status_code=503)

    base_url = os.environ.get("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    upstream_url = f"{base_url}/anthropic/{path.lstrip('/')}"
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower()
        not in {
            "authorization",
            "x-api-key",
            "host",
            "content-length",
            "connection",
            "accept-encoding",
        }
    }
    headers["x-api-key"] = api_key
    headers["accept-encoding"] = "identity"

    body = await request.body()
    client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=30, write=30, pool=30))
    upstream = None
    last_error: httpx.HTTPError | None = None
    for attempt in range(3):
        try:
            upstream_request = client.build_request(
                request.method,
                upstream_url,
                headers=headers,
                content=body,
            )
            candidate = await client.send(upstream_request, stream=True)
            if candidate.status_code in {502, 503, 504} and attempt < 2:
                await candidate.aclose()
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            upstream = candidate
            break
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))

    if upstream is None:
        await client.aclose()
        error_name = type(last_error).__name__ if last_error else "HTTPError"
        return Response(f"upstream request failed: {error_name}", status_code=502)

    async def body_iterator():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    response_headers = {}
    for name in ("content-type", "request-id", "x-request-id"):
        if value := upstream.headers.get(name):
            response_headers[name] = value
    return StreamingResponse(
        body_iterator(),
        status_code=upstream.status_code,
        headers=response_headers,
    )


def main() -> None:
    uvicorn.run(app, host="127.0.0.1", port=15721, access_log=True)


if __name__ == "__main__":
    main()
