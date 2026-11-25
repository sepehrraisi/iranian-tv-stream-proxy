"""Step 5: memory cap, shared concurrency limit, optional bandwidth cap."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import server


def _seg(name: str, size: int) -> server.CachedSegment:
    url = f"https://cdn.sepehrtv.ir/live/tv2/480/{name}.ts"
    return server.CachedSegment(
        token=server.pack_url(url),
        url=url,
        duration=10.0,
        data=b"a" * size,
        content_type="video/MP2T",
        media_sequence=1,
    )


def test_defaults_fit_a_small_vps():
    assert server.CACHE_SEGMENTS == 8
    assert server.CACHE_MAX_BYTES == 128 * 1024 * 1024
    assert server.UPSTREAM_LIMIT_BPS == 0


def test_memory_cap_drops_orphans_before_the_ring(monkeypatch):
    monkeypatch.setattr(server, "CACHE_MAX_BYTES", 150)
    buf = server.segment_buffer
    ring_seg = _seg("ring", 100)
    ring = server.ChannelRing(slug="tv2", segments=[ring_seg])
    buf.rings["tv2"] = ring
    buf._remember("tv2", ring_seg.token, ring_seg)
    older = _seg("old", 100)
    newer = _seg("new", 100)
    # stored_at order is insertion order; backdate the orphan we expect to lose.
    older.stored_at = ring_seg.stored_at - 10
    newer.stored_at = ring_seg.stored_at + 10
    buf._remember("tv2", older.token, older)
    buf._remember("tv2", newer.token, newer)
    assert ring.segments == [ring_seg]
    assert older.token not in buf._by_token
    assert newer.token not in buf._by_token
    assert sum(len(seg.data) for seg in buf._unique_segments()) <= 150


def test_zero_cap_keeps_every_segment(monkeypatch):
    monkeypatch.setattr(server, "CACHE_MAX_BYTES", 0)
    buf = server.segment_buffer
    for name in ("a", "b", "c"):
        seg = _seg(name, 100)
        buf._remember("tv2", seg.token, seg)
    assert len(buf._by_token) == 3


def test_bandwidth_cap_sleeps(monkeypatch):
    monkeypatch.setattr(server, "UPSTREAM_LIMIT_BPS", 1000)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
    asyncio.run(server.throttle_upstream(2500))
    assert slept == [2.5]
    slept.clear()
    monkeypatch.setattr(server, "UPSTREAM_LIMIT_BPS", 0)
    asyncio.run(server.throttle_upstream(2500))
    assert slept == []


def test_stream_fallback_uses_the_fetch_semaphore(client: TestClient, monkeypatch):
    entered = 0

    class Counting:
        async def __aenter__(self):
            nonlocal entered
            entered += 1
            return self

        async def __aexit__(self, *_args):
            return False

    async def fail_cache(*_args, **_kwargs):
        raise RuntimeError("cache disabled for this test")

    monkeypatch.setattr(server.segment_buffer, "download_and_cache", fail_cache)
    monkeypatch.setattr(server.segment_buffer, "_sem", Counting())
    url = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=aaa"
    response = client.get(f"/stream/tv2/s/{server.pack_url(url)}")
    assert response.status_code == 200
    assert entered == 1
    assert len(response.content) == 100_000
