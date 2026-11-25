"""Step 3: one upstream GET per segment, shared as simulated viewers grow."""

from __future__ import annotations

import asyncio

import httpx

import server


def _reset(upstream) -> None:
    server.traffic.reset()
    server.segment_buffer.rings.clear()
    server.segment_buffer._by_token.clear()
    server.segment_buffer._by_canonical.clear()
    server.segment_buffer._inflight.clear()
    upstream.segment_fetches = 0
    upstream.calls.clear()


def _burst(path: str, viewers: int) -> list[httpx.Response]:
    async def run() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return list(
                await asyncio.gather(*[client.get(path) for _ in range(viewers)])
            )

    return asyncio.run(run())


def test_parallel_viewers_share_one_upstream_fetch(upstream, monkeypatch):
    monkeypatch.setattr(server, "SEGMENT_SINGLE_FLIGHT", True)
    url = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=aaa"
    path = f"/stream/tv2/s/{server.pack_url(url)}"
    seen: list[tuple[int, int, int]] = []
    for viewers in (1, 10, 50):
        _reset(upstream)
        responses = _burst(path, viewers)
        assert all(response.status_code == 200 for response in responses)
        assert all(response.content == upstream.segment_body for response in responses)
        assert upstream.segment_fetches == 1
        assert server.traffic.upstream_bytes == len(upstream.segment_body)
        assert server.traffic.downstream_bytes == viewers * len(upstream.segment_body)
        seen.append((viewers, server.traffic.upstream_bytes, server.traffic.downstream_bytes))
    # Upstream stays one segment while viewers go from 1 to 50.
    assert [row[1] for row in seen] == [100_000, 100_000, 100_000]
    assert seen[-1][2] == 50 * 100_000


def test_same_path_different_token_is_one_fetch(upstream, monkeypatch):
    monkeypatch.setattr(server, "SEGMENT_SINGLE_FLIGHT", True)
    first = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=aaa"
    second = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=bbb"
    _reset(upstream)
    transport_app = server.app

    async def run() -> None:
        transport = httpx.ASGITransport(app=transport_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            a = await client.get(f"/stream/tv2/s/{server.pack_url(first)}")
            b = await client.get(f"/stream/tv2/s/{server.pack_url(second)}")
            assert a.status_code == 200 and b.status_code == 200
            assert a.content == b.content

    asyncio.run(run())
    assert upstream.segment_fetches == 1


def test_singleflight_off_fetches_each_token(upstream, monkeypatch):
    monkeypatch.setattr(server, "SEGMENT_SINGLE_FLIGHT", False)
    first = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=aaa"
    second = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=bbb"
    _reset(upstream)

    async def run() -> None:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get(f"/stream/tv2/s/{server.pack_url(first)}")).status_code == 200
            assert (await client.get(f"/stream/tv2/s/{server.pack_url(second)}")).status_code == 200

    asyncio.run(run())
    assert upstream.segment_fetches == 2
