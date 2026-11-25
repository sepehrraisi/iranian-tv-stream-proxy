"""Step 4: ?h= cannot select a rendition above MAX_HEIGHT."""

from __future__ import annotations

import asyncio
import logging

from fastapi.testclient import TestClient

import server


def test_max_height_defaults_to_force_height():
    assert server.FORCE_HEIGHT == 480
    assert server.MAX_HEIGHT == 480
    assert server.capped_height(None) == 480
    assert server.capped_height(360) == 360
    assert server.capped_height(1080) == 480


def _fetched(upstream, needle: str) -> bool:
    return any(needle in url for url in upstream.calls)


def test_query_above_cap_stays_at_force_height(client: TestClient, upstream):
    response = client.get("/stream/tv2/index.m3u8?h=1080")
    assert response.status_code == 200
    assert response.headers["x-stream-quality"] == "854x480"
    assert _fetched(upstream, "/480/index.m3u8")
    assert not _fetched(upstream, "/1080/")


def test_query_below_cap_still_selects_that_height(client: TestClient, upstream):
    response = client.get("/stream/tv2/index.m3u8?h=360")
    assert response.status_code == 200
    assert response.headers["x-stream-quality"] == "640x360"
    assert _fetched(upstream, "/360/index.m3u8")


def test_max_height_zero_disables_the_cap(client: TestClient, monkeypatch, upstream):
    monkeypatch.setattr(server, "MAX_HEIGHT", 0)
    response = client.get("/stream/tv2/index.m3u8?h=1080")
    assert response.status_code == 200
    assert response.headers["x-stream-quality"] == "1920x1080"
    assert _fetched(upstream, "/1080/index.m3u8")


def test_direct_media_playlist_is_logged_once(caplog, monkeypatch):
    monkeypatch.setattr(server, "FORCE_HEIGHT", 480)
    media = "https://cdn.sepehrtv.ir/live/tv2/480/index.m3u8"
    server.resolver._cache["tv2"] = server.ResolvedStream(
        slug="tv2", url=media, source="api"
    )
    server._MEDIA_ONLY_LOGGED.discard("tv2")
    with caplog.at_level(logging.INFO, logger="sepehr-stream"):
        _body, _url, quality = asyncio.run(server.fetch_forced_quality_playlist("tv2"))
    assert quality == "media"
    assert "already a media playlist" in caplog.text
    assert "m3u8" not in "".join(
        rec.message for rec in caplog.records if "already a media playlist" in rec.message
    )
    assert server.traffic.channel("tv2").quality == "media"
