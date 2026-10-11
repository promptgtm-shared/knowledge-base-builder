import asyncio

import httpx
import pytest
import respx

from kb.http import FetchError, HttpPolicy, create_client, get

POLICY = HttpPolicy(max_retries=3)
URL = "https://example.com/page"


async def _no_sleep(_: float) -> None:
    return None


def _run(coro):
    return asyncio.run(coro)


async def _get(**kwargs):
    async with create_client(POLICY) as client:
        return await get(client, URL, POLICY, sleep=kwargs.pop("sleep", _no_sleep), **kwargs)


@respx.mock
def test_retries_503_then_succeeds() -> None:
    route = respx.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(200, text="ok")])
    resp = _run(_get())
    assert resp.status == 200
    assert resp.text == "ok"
    assert route.call_count == 2


@respx.mock
def test_honors_retry_after_on_429() -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200)])
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    resp = _run(_get(sleep=record))
    assert resp.status == 200
    assert slept == [7.0]


@respx.mock
def test_gives_up_after_max_retries_with_typed_error() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(FetchError) as exc:
        _run(_get())
    assert exc.value.kind == "status"
    assert exc.value.status == 500
    assert route.call_count == POLICY.max_retries


@respx.mock
def test_timeouts_exhaust_into_timeout_error() -> None:
    respx.get(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError) as exc:
        _run(_get())
    assert exc.value.kind == "timeout"


@respx.mock
def test_404_and_304_are_returned_not_raised() -> None:
    respx.get(URL).mock(side_effect=[httpx.Response(404), httpx.Response(304)])
    assert _run(_get()).status == 404
    assert _run(_get()).status == 304


@respx.mock
def test_extra_headers_sent_and_final_url_recorded() -> None:
    respx.get(URL).mock(return_value=httpx.Response(301, headers={"Location": "https://example.com/new"}))
    respx.get("https://example.com/new").mock(return_value=httpx.Response(200, headers={"ETag": '"abc"'}))
    resp = _run(_get(extra_headers={"If-None-Match": '"old"'}))
    assert resp.url == "https://example.com/new"
    assert resp.headers["etag"] == '"abc"'
    assert respx.calls[0].request.headers["If-None-Match"] == '"old"'


@respx.mock
def test_retry_after_is_clamped_and_sanitized() -> None:
    slept: list[float] = []

    async def record(seconds: float) -> None:
        slept.append(seconds)

    for header in ("1e9", "inf", "-5", "nan"):
        respx.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": header}), httpx.Response(200)])
        _run(_get(sleep=record))
    assert slept == [60.0, 2.0, 2.0, 2.0]
