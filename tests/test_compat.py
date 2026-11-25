"""Frozen client-facing behavior. Upstream is the mock in conftest.py."""

from __future__ import annotations

from urllib.parse import urljoin

from fastapi.testclient import TestClient

import server
from conftest import master_url, media_playlist

EXPECTED_PLAYLIST_CACHE = "public, max-age=1"
EXPECTED_SEGMENT_CACHE = "public, max-age=86400, immutable"

SLUGS = [
    "tv1", "tv2", "tv3", "tv4", "tv5", "irinn", "varzesh", "namayesh",
    "nasim", "mostanad", "pouya", "ofogh", "salamat", "amouzesh", "quran",
    "tamasha",
]


def expected_media(slug: str, label: str) -> str:
    base = f"https://cdn.sepehrtv.ir/live/{slug}/{label}/index.m3u8"
    key = server.pack_url(urljoin(base, "enc.key"))
    first = server.pack_url(urljoin(base, f"seg100-{label}.ts?token=aaa"))
    second = server.pack_url(urljoin(base, f"seg101-{label}.ts?token=aaa"))
    offset = f"{server.LIVE_START_OFFSET_SECONDS:.3f}"
    return (
        "#EXTM3U\n"
        f"#EXT-X-START:TIME-OFFSET=-{offset},PRECISE=YES\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:100\n"
        f'#EXT-X-KEY:METHOD=AES-128,URI="/stream/{slug}/s/{key}"\n'
        "#EXTINF:10.0,\n"
        f"/stream/{slug}/s/{first}\n"
        "#EXTINF:9.5,\n"
        f"/stream/{slug}/s/{second}\n"
    )


def test_env_flags_keep_old_names_and_defaults(monkeypatch):
    # This suite forces PREWARM_ON_START=0 before import so startup cannot
    # phone the CDN. The parser still defaults that flag to on.
    monkeypatch.delenv("PREWARM_ON_START", raising=False)
    monkeypatch.delenv("SEGMENT_CACHE", raising=False)
    monkeypatch.delenv("USE_BROWSER_FALLBACK", raising=False)
    assert server.env_flag("PREWARM_ON_START", "1") is True
    assert server.env_flag("SEGMENT_CACHE", "1") is True
    assert server.env_flag("USE_BROWSER_FALLBACK", "1") is True
    monkeypatch.setenv("PREWARM_ON_START", "0")
    assert server.env_flag("PREWARM_ON_START", "1") is False
    monkeypatch.setenv("PREWARM_ON_START", "1")
    assert server.env_flag("PREWARM_ON_START", "1") is True
    assert server.FORCE_HEIGHT == 480
    assert server.LIVE_START_OFFSET_SECONDS == 36.0
    assert server.TOKEN_TTL_SECONDS == 900
    assert server.WARM_INTERVAL_SECONDS == 300
    assert server.BUFFER_POLL_SECONDS == 1.0
    assert server.BUFFER_FETCH_CONCURRENCY == 8
    for name in (
        "HOST",
        "PORT",
        "PUBLIC_BASE_URL",
        "USER_AGENT",
        "TOKEN_TTL_SECONDS",
        "RESOLVE_TIMEOUT_SECONDS",
        "UPSTREAM_TIMEOUT_SECONDS",
        "USE_BROWSER_FALLBACK",
        "CHROMIUM_PATH",
        "FORCE_HEIGHT",
        "LIVE_START_OFFSET_SECONDS",
        "PREWARM_ON_START",
        "WARM_INTERVAL_SECONDS",
        "PREWARM_SLUGS",
        "SEGMENT_CACHE",
        "CACHE_SEGMENTS",
        "BUFFER_POLL_SECONDS",
        "BUFFER_FETCH_CONCURRENCY",
    ):
        assert hasattr(server, name), name


def test_index_playlist_shape(client: TestClient):
    response = client.get("/stream/tv2/index.m3u8")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert response.headers["access-control-allow-origin"] == "*"
    assert response.headers["cache-control"] == EXPECTED_PLAYLIST_CACHE
    assert response.headers["x-stream-quality"] == "854x480"
    body = response.text
    assert body == expected_media("tv2", "480")
    assert "#EXT-X-STREAM-INF" not in body
    assert "seg100-1080" not in body
    assert "TIME-OFFSET=-2" not in body


def test_height_query_selects_lower_variant(client: TestClient):
    response = client.get("/stream/tv2/index.m3u8?h=360")
    assert response.status_code == 200
    assert response.headers["x-stream-quality"] == "640x360"
    assert response.text == expected_media("tv2", "360")


def test_nested_playlist_and_segment_roundtrip(client: TestClient):
    media = f"https://cdn.sepehrtv.ir/live/tv2/480/index.m3u8"
    token = server.pack_url(media)
    response = client.get(f"/stream/tv2/p/{token}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert response.headers["cache-control"] == EXPECTED_PLAYLIST_CACHE
    assert response.text == expected_media("tv2", "480")

    segment_url = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=aaa"
    seg_token = server.pack_url(segment_url)
    fetched = client.get(f"/stream/tv2/s/{seg_token}")
    assert fetched.status_code == 200
    assert fetched.headers["access-control-allow-origin"] == "*"
    assert fetched.headers["content-type"].startswith("video/MP2T")
    assert fetched.headers["cache-control"] == EXPECTED_SEGMENT_CACHE
    assert fetched.headers["content-length"] == str(len(fetched.content))
    assert len(fetched.content) == 100_000

    cached = client.get(f"/stream/tv2/s/{seg_token}")
    assert cached.status_code == 200
    assert cached.content == fetched.content
    assert cached.headers["x-stream-buffer"] == "hit"


def test_unknown_slug_and_bad_token(client: TestClient):
    for path in (
        "/stream/nope/index.m3u8",
        "/stream/nope/p/aaaa",
        "/stream/nope/s/aaaa",
        "/preview/nope",
        "/iframe/nope",
        "/resolve/nope",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert "nope" in response.json()["detail"]
    deleted = client.delete("/stream/nope")
    assert deleted.status_code == 404
    bad = client.get("/stream/tv2/s/not-valid")
    assert bad.status_code == 400
    assert "detail" in bad.json()


def test_iframe_and_preview(client: TestClient):
    iframe = client.get("/iframe/tv2")
    assert iframe.status_code == 200
    assert "text/html" in iframe.headers["content-type"]
    assert "شبکه دو" in iframe.text
    assert "/stream/tv2/index.m3u8" in iframe.text
    assert "frame-ancestors *" in iframe.headers["content-security-policy"]
    preview = client.get("/preview/tv2")
    assert preview.status_code == 200
    assert "شبکه دو" in preview.text
    assert "/stream/tv2/index.m3u8" in preview.text


def test_playlist_channels_root_static_sessions(client: TestClient):
    listing = client.get("/playlist.m3u")
    assert listing.status_code == 200
    assert listing.text.startswith("#EXTM3U\n")
    for slug in SLUGS:
        assert f"/stream/{slug}/index.m3u8" in listing.text
    assert listing.text.count("#EXTINF") == 16

    channels = client.get("/channels")
    assert channels.status_code == 200
    body = channels.json()
    got = [ch["slug"] for ch in body["channels"]]
    assert got == SLUGS
    sample = body["channels"][1]
    for key in ("embed_url", "hls_url", "preview_url", "iframe_url", "name", "num"):
        assert key in sample
    assert sample["hls_url"].endswith("/stream/tv2/index.m3u8")
    assert sample["iframe_url"].endswith("/iframe/tv2")

    root = client.get("/")
    assert root.status_code == 200
    assert "/preview/tv2" in root.text
    assert "/iframe/tv2" in root.text

    ping = server.STATIC_DIR / "ping.txt"
    ping.write_text("ok", encoding="utf-8")
    try:
        static = client.get("/static/ping.txt")
        assert static.status_code == 200
        assert static.text == "ok"
    finally:
        ping.unlink(missing_ok=True)

    sessions = client.get("/sessions")
    assert sessions.status_code == 200
    payload = sessions.json()
    for key in (
        "warm_interval_seconds",
        "prewarm_on_start",
        "wanted",
        "ready_count",
        "resolved",
        "segment_buffer",
    ):
        assert key in payload
    assert payload["wanted"] == SLUGS
    buffer = payload["segment_buffer"]
    for key in ("enabled", "keep_segments", "poll_seconds", "ready_count", "total_bytes", "channels"):
        assert key in buffer


def test_resolve_warm_and_delete(client: TestClient):
    resolved = client.get("/resolve/tv2")
    assert resolved.status_code == 200
    body = resolved.json()
    for key in (
        "slug",
        "source",
        "age_seconds",
        "ready",
        "quality",
        "upstream",
        "media_url",
        "hls_url",
        "last_ok_seconds_ago",
    ):
        assert key in body
    assert body["slug"] == "tv2"
    assert body["upstream"] == master_url("tv2")
    assert body["hls_url"].endswith("/stream/tv2/index.m3u8")
    assert "480" in (body["media_url"] or "")

    warmed = client.post("/warm")
    assert warmed.status_code == 200
    warm_body = warmed.json()
    assert set(warm_body["warmed"]) == set(SLUGS)
    assert warm_body["count"] == 16
    assert "segment_buffer" in warm_body

    dropped = client.delete("/stream/tv2")
    assert dropped.status_code == 200
    assert dropped.json() == {"invalidated": "tv2"}


def test_media_fixture_matches_rewriter():
    """The golden string is the rewriter's output for the fixture, not a second parser."""
    base = "https://cdn.sepehrtv.ir/live/tv2/480/index.m3u8"
    rewritten = server.rewrite_playlist(media_playlist("480"), "tv2", base)
    assert rewritten == expected_media("tv2", "480")
