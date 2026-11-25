"""Step 2: the segment loop fetches a channel only while it has a viewer."""

from __future__ import annotations

import asyncio
import time

import server


def test_idle_channel_is_not_fetched(upstream, monkeypatch):
    monkeypatch.setattr(server, "PREWARM_SEGMENTS", False)
    monkeypatch.setattr(server, "VIEWER_IDLE_SECONDS", 75)

    asyncio.run(server.segment_buffer.refresh_all())
    assert upstream.segment_fetches == 0

    server.traffic.note_viewer("tv2", "10.0.0.5")
    asyncio.run(server.segment_buffer.refresh_all())
    segment_urls = [url for url in upstream.calls if url.split("?", 1)[0].endswith(".ts")]
    assert segment_urls
    assert all("/tv2/" in url for url in segment_urls)
    fetched = upstream.segment_fetches

    server.traffic.channel("tv2").last_view_at = time.time() - 1000
    asyncio.run(server.segment_buffer.refresh_all())
    assert upstream.segment_fetches == fetched


def test_prewarm_segments_restores_every_channel(upstream, monkeypatch):
    monkeypatch.setattr(server, "PREWARM_SEGMENTS", True)
    asyncio.run(server.segment_buffer.refresh_all())
    slugs = set()
    for url in upstream.calls:
        if not url.split("?", 1)[0].endswith(".ts"):
            continue
        for slug in ("tv1", "tv2", "irinn", "tamasha"):
            if f"/{slug}/" in url:
                slugs.add(slug)
    assert slugs == {"tv1", "tv2", "irinn", "tamasha"}
    assert upstream.segment_fetches >= 16
