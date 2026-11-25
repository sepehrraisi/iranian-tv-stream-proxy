"""Test setup: no live CDN, no prewarm, no browser.

Env is set before `server` is imported so startup constants match this suite.
Production defaults are checked via `server.env_flag`, which is what the
process uses when it reads the environment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ["PREWARM_ON_START"] = "0"
os.environ["USE_BROWSER_FALLBACK"] = "0"
os.environ["SEPEHR_OAUTH_CONSUMER_KEY"] = "test-consumer-key"
os.environ["SEPEHR_OAUTH_SIGNATURE"] = "test-signature"
os.environ["SEPEHR_OAUTH_TOKEN"] = "test-token"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx
import pytest

import server

MASTER_PLAYLIST = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080
1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=854x480
480/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=400000,RESOLUTION=640x360
360/index.m3u8
"""


def media_playlist(label: str) -> str:
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:10\n"
        "#EXT-X-MEDIA-SEQUENCE:100\n"
        "#EXT-X-START:TIME-OFFSET=-2.000,PRECISE=YES\n"
        '#EXT-X-KEY:METHOD=AES-128,URI="enc.key"\n'
        "#EXTINF:10.0,\n"
        f"seg100-{label}.ts?token=aaa\n"
        "#EXTINF:9.5,\n"
        f"seg101-{label}.ts?token=aaa\n"
    )


def master_url(slug: str) -> str:
    return f"https://cdn.sepehrtv.ir/live/{slug}/master.m3u8?t=1"


class UpstreamScript:
    """Mock sepehrtv API + CDN. Records every URL the server fetches."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.segment_body = b"x" * 100_000
        self.segment_fetches = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        path = request.url.path
        if path.rstrip("/").endswith("/channels/1"):
            items = []
            for ch in server.CHANNELS:
                slug = str(ch["slug"])
                items.append({"uid": slug, "streams": [{"src": master_url(slug)}]})
            return httpx.Response(200, json={"list": items})
        if "/v3/channels" in path:
            slug = request.url.params.get("key") or "tv2"
            return httpx.Response(
                200,
                json={"list": [{"uid": slug, "streams": [{"src": master_url(slug)}]}]},
            )
        if path.endswith("master.m3u8"):
            return httpx.Response(200, text=MASTER_PLAYLIST)
        if "/1080/" in path and path.endswith(".m3u8"):
            return httpx.Response(200, text=media_playlist("1080"))
        if "/360/" in path and path.endswith(".m3u8"):
            return httpx.Response(200, text=media_playlist("360"))
        if path.endswith(".m3u8"):
            return httpx.Response(200, text=media_playlist("480"))
        if path.endswith(".ts"):
            self.segment_fetches += 1
            return httpx.Response(
                200,
                content=self.segment_body,
                headers={"content-type": "video/MP2T"},
            )
        if path.endswith("enc.key"):
            return httpx.Response(200, content=b"0123456789abcdef")
        return httpx.Response(404, text="not found")


@pytest.fixture(autouse=True)
def upstream(monkeypatch: pytest.MonkeyPatch) -> UpstreamScript:
    script = UpstreamScript()
    transport = httpx.MockTransport(script.handler)
    server.resolver.client = httpx.AsyncClient(transport=transport)
    server.traffic.reset()
    server.segment_buffer.rings.clear()
    server.segment_buffer._by_token.clear()
    server.segment_buffer._by_canonical.clear()
    server.segment_buffer._inflight.clear()
    server.resolver._cache.clear()
    monkeypatch.setattr(server.segment_buffer, "start", lambda: None)
    monkeypatch.setattr(server.resolver, "start_warmer", lambda: None)
    yield script


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    with TestClient(server.app) as test_client:
        yield test_client
