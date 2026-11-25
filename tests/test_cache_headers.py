"""Step 6: short playlist cache, long immutable segment cache."""

from fastapi.testclient import TestClient

import server


def test_legacy_cache_control_mode(client: TestClient, monkeypatch):
    monkeypatch.setattr(server, "CACHE_CONTROL_MODE", "legacy")
    playlist = client.get("/stream/tv2/index.m3u8")
    assert playlist.headers["cache-control"] == "no-cache, no-store"
    url = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=aaa"
    segment = client.get(f"/stream/tv2/s/{server.pack_url(url)}")
    assert segment.headers["cache-control"] == "public, max-age=30"
    assert segment.headers["access-control-allow-origin"] == "*"
