"""One viewer, 36s behind the live edge, with rotating playlist tokens.

Production (TV3, one viewer) showed hit rate ~0.24 and upstream about twice
downstream: the prefetcher pulled the newest CACHE_SEGMENTS while the player
starts at LIVE_START_OFFSET_SECONDS. Query tokens (s=, t=) rotate on every
playlist refresh; the path does not.
"""

from __future__ import annotations

import asyncio

import httpx

import server

SEGMENT_BYTES = 100_000
SEGMENT_COUNT = 16
SEGMENT_DURATION = 6.0
FIRST_SEQ = 1000
# Unit file keeps 15. That is wider than the 36s the player actually reads.
PRODUCTION_CACHE_SEGMENTS = 15


def _master() -> str:
    return (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1920x1080\n"
        "1080/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=768x432\n"
        "432/index.m3u8\n"
    )


def _media(token: str) -> str:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
        f"#EXT-X-MEDIA-SEQUENCE:{FIRST_SEQ}",
    ]
    for offset in range(SEGMENT_COUNT):
        seq = FIRST_SEQ + offset
        lines.append(f"#EXTINF:{SEGMENT_DURATION:.3f},")
        lines.append(f"seg{seq}.ts?s={token}&t={token}")
    return "\n".join(lines) + "\n"


class RotatingLive:
    """Master with 1080 and 432. Every media playlist rotates s= and t=."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.segment_urls: list[str] = []
        self.tokens: list[str] = []
        self.segment_body = b"s" * SEGMENT_BYTES
        self._n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        path = request.url.path
        if path.endswith("master.m3u8"):
            return httpx.Response(200, text=_master())
        if "/1080/" in path and path.endswith(".m3u8"):
            self._n += 1
            token = f"edge{self._n}"
            self.tokens.append(token)
            return httpx.Response(200, text=_media(token))
        if path.endswith(".m3u8"):
            self._n += 1
            token = f"rot{self._n}"
            self.tokens.append(token)
            return httpx.Response(200, text=_media(token))
        if path.endswith(".ts"):
            self.segment_urls.append(url)
            return httpx.Response(
                200,
                content=self.segment_body,
                headers={"content-type": "video/MP2T"},
            )
        return httpx.Response(404, text="not found")


def _install(upstream: RotatingLive) -> None:
    server.resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    server.resolver._cache.clear()
    server.resolver._cache["tv3"] = server.ResolvedStream(
        slug="tv3",
        url="https://cdn.sepehrtv.ir/live/tv3/master.m3u8?t=1",
        source="test",
    )
    server.traffic.reset()
    server.segment_buffer.rings.clear()
    server.segment_buffer._by_token.clear()
    server.segment_buffer._by_canonical.clear()
    server.segment_buffer._inflight.clear()


def _play_window(playlist: str, offset: float) -> list[str]:
    """Proxy paths for segments overlapping [end - offset, end]."""
    pending = SEGMENT_DURATION
    entries: list[tuple[float, str]] = []
    for raw in playlist.splitlines():
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            pending = float(line.split(":", 1)[1].split(",")[0])
        elif line and not line.startswith("#"):
            entries.append((pending, line))
    total = sum(dur for dur, _ in entries)
    start_at = max(0.0, total - offset)
    cursor = 0.0
    begin = 0
    for index, (dur, _url) in enumerate(entries):
        if cursor + dur > start_at + 1e-6:
            begin = index
            break
        cursor += dur
    return [url for _dur, url in entries[begin:]]


def _seq(url: str) -> str | None:
    marker = "seg"
    if marker not in url or ".ts" not in url:
        return None
    name = url.split(marker, 1)[1].split(".ts", 1)[0]
    return name


async def _settle() -> None:
    """Let the index handler's background prefetch finish."""
    idle = 0
    while idle < 8:
        inflight = [task for task in server.segment_buffer._inflight.values() if not task.done()]
        if inflight:
            idle = 0
            await asyncio.gather(*inflight, return_exceptions=True)
            continue
        await asyncio.sleep(0)
        idle += 1


async def _viewer_session(upstream: RotatingLive) -> None:
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        opened = await client.get("/stream/tv3/index.m3u8")
        assert opened.status_code == 200
        assert "TIME-OFFSET=-36.000" in opened.text
        # Taken before the background task is drained. The whole seek window
        # has to already be in RAM, or the player blocks on those fetches.
        early_seqs = {_seq(url) for url in upstream.segment_urls}
        await _settle()
        # Buffer loop, same as production while the viewer is active.
        await server.segment_buffer.refresh_all()
        await _settle()
        refreshed = await client.get("/stream/tv3/index.m3u8")
        assert refreshed.status_code == 200
        await _settle()

        # Playlist tokens moved. The player still asks for the -36s window.
        assert len(upstream.tokens) >= 2
        assert upstream.tokens[0] != upstream.tokens[-1]
        paths = _play_window(refreshed.text, server.LIVE_START_OFFSET_SECONDS)
        assert paths, "player window was empty"
        responses = []
        for path in paths:
            responses.append(await client.get(path))
        await _settle()

    assert all(response.status_code == 200 for response in responses)
    assert all(response.headers.get("X-Stream-Buffer") == "hit" for response in responses)
    assert all(len(response.content) == SEGMENT_BYTES for response in responses)

    first_upstream = server.unpack_url(paths[0].rsplit("/", 1)[-1])
    first_seq = _seq(first_upstream)
    fetched = {_seq(url) for url in upstream.segment_urls}
    played = set()
    for path in paths:
        upstream_url = server.unpack_url(path.rsplit("/", 1)[-1])
        played.add(_seq(upstream_url))
    behind = sorted(seq for seq in fetched if seq is not None and seq not in played)
    snap = server.traffic.snapshot()
    ratio = (
        snap["upstream_bytes"] / snap["downstream_bytes"]
        if snap["downstream_bytes"]
        else 0
    )
    detail = (
        f"hit_rate={snap['hit_rate']:.3f} hits={snap['segment_hits']} "
        f"misses={snap['segment_misses']} joins={snap['singleflight_joins']} "
        f"upstream={snap['upstream_bytes']} downstream={snap['downstream_bytes']} "
        f"ratio={ratio:.3f} segment_fetches={len(upstream.segment_urls)} "
        f"played={sorted(played)} behind={behind} early={sorted(s for s in early_seqs if s)} "
        f"qualities={[url for url in upstream.segment_urls if '/1080/' in url]}"
    )
    assert first_seq in early_seqs, detail
    assert played <= early_seqs, detail
    assert not behind, detail
    assert not any("/1080/" in url for url in upstream.segment_urls), detail
    assert snap["hit_rate"] > 0.9, detail
    assert 0.85 <= ratio <= 1.15, detail


def test_rotating_tokens_player_thirty_six_seconds_behind(upstream, monkeypatch):
    assert server.LIVE_START_OFFSET_SECONDS == 36
    monkeypatch.setattr(server, "CACHE_SEGMENTS", PRODUCTION_CACHE_SEGMENTS)
    monkeypatch.setattr(server, "SEGMENT_SINGLE_FLIGHT", True)
    monkeypatch.setattr(server, "SEGMENT_CACHE", True)
    live = RotatingLive()
    _install(live)
    asyncio.run(_viewer_session(live))
