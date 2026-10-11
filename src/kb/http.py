"""Shared async HTTP client with retry/backoff for sitemap and page fetches."""

from __future__ import annotations

import asyncio
import random
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import cache

import httpx
from pydantic import BaseModel

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

Sleep = Callable[[float], Awaitable[None]]


class HttpPolicy(BaseModel):
    concurrency: int = 10
    min_delay: float = 0.5
    max_delay: float = 2.0
    max_retries: int = 3
    timeout: float = 30.0


@dataclass
class Response:
    status: int
    url: str
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class FetchError(Exception):
    """Raised when a request still fails after all retries."""

    def __init__(self, url: str, kind: str, status: int | None = None, detail: str = "") -> None:
        self.url = url
        self.kind = kind  # "timeout" | "connect" | "status" | "http"
        self.status = status
        super().__init__(f"{kind} fetching {url}" + (f" (HTTP {status})" if status else "") + (f": {detail}" if detail else ""))


def get_headers() -> dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
        "Upgrade-Insecure-Requests": "1",
    }


@cache
def _ssl_context() -> ssl.SSLContext:
    # Building the default context loads the CA bundle (~1s on Windows); reuse it
    return httpx.create_ssl_context()


def create_client(policy: HttpPolicy) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        http2=True,
        verify=_ssl_context(),
        follow_redirects=True,
        timeout=httpx.Timeout(policy.timeout),
        limits=httpx.Limits(
            max_connections=policy.concurrency + 5,
            max_keepalive_connections=policy.concurrency,
        ),
    )


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    value = resp.headers.get("Retry-After", "")
    try:
        return float(value)
    except ValueError:
        return float(2 ** (attempt + 1))


async def get(
    client: httpx.AsyncClient,
    url: str,
    policy: HttpPolicy,
    extra_headers: dict[str, str] | None = None,
    sleep: Sleep = asyncio.sleep,
) -> Response:
    """GET with retries on timeouts, connection errors, 429 and 5xx.

    Any other status (2xx, 304, 4xx) is returned for the caller to interpret.
    """
    headers = get_headers()
    if extra_headers:
        headers.update(extra_headers)

    last_error: FetchError | None = None
    for attempt in range(policy.max_retries):
        try:
            resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            last_error = FetchError(url, "timeout", detail=str(e))
        except httpx.ConnectError as e:
            last_error = FetchError(url, "connect", detail=str(e))
        except httpx.HTTPError as e:
            last_error = FetchError(url, "http", detail=str(e))
        else:
            if resp.status_code == 429:
                last_error = FetchError(url, "status", resp.status_code)
                if attempt < policy.max_retries - 1:
                    await sleep(_retry_after(resp, attempt))
                continue
            if resp.status_code >= 500:
                last_error = FetchError(url, "status", resp.status_code)
            else:
                return Response(
                    status=resp.status_code,
                    url=str(resp.url),
                    content=resp.content,
                    headers={k.lower(): v for k, v in resp.headers.items()},
                )

        if attempt < policy.max_retries - 1:
            await sleep(float(2 ** (attempt + 1)))

    assert last_error is not None
    raise last_error
