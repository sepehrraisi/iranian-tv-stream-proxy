"""Step 7: unpacked URLs may only target known Sepehr hosts."""

from __future__ import annotations

import logging

import httpx
from fastapi.testclient import TestClient

import server


def test_allowlist_matches_suffixes_only():
    assert server.upstream_host_allowed("https://cdn.sepehrtv.ir/live/a.ts")
    assert server.upstream_host_allowed("https://sepehrapi.sepehrtv.ir/v3/channels/")
    assert server.upstream_host_allowed("https://sepehrtv.ir/frame/t/tv2")
    assert not server.upstream_host_allowed("https://evil.example/a.ts")
    assert not server.upstream_host_allowed("https://sepehrtv.ir.evil.example/a.ts")
    assert not server.upstream_host_allowed("file:///etc/passwd")
    assert not server.upstream_host_allowed("http://127.0.0.1/latest/meta-data")


def test_segment_and_playlist_reject_foreign_hosts(client: TestClient, upstream):
    foreign = server.pack_url("https://evil.example/video.ts")
    segment = client.get(f"/stream/tv2/s/{foreign}")
    assert segment.status_code == 403
    assert segment.json()["detail"] == "Upstream host is not allowed"
    playlist = client.get(f"/stream/tv2/p/{server.pack_url('https://evil.example/a.m3u8')}")
    assert playlist.status_code == 403
    assert not any("evil.example" in url for url in upstream.calls)
    assert server.traffic.rejected_hosts >= 1


def test_allowlisted_segment_still_plays(client: TestClient):
    url = "https://cdn.sepehrtv.ir/live/tv2/480/seg100-480.ts?token=aaa"
    response = client.get(f"/stream/tv2/s/{server.pack_url(url)}")
    assert response.status_code == 200
    assert len(response.content) == 100_000


def test_redirect_to_a_foreign_host_is_not_followed(client: TestClient, upstream):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jump"):
            return httpx.Response(302, headers={"location": "https://evil.example/secret.ts"})
        return upstream.handler(request)

    server.resolver.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    jumped = server.pack_url("https://cdn.sepehrtv.ir/live/tv2/jump")
    response = client.get(f"/stream/tv2/s/{jumped}")
    assert response.status_code == 403
    assert not any("evil.example" in url for url in upstream.calls)


def test_star_allowlist_logs_once_a_minute(monkeypatch, caplog):
    monkeypatch.setattr(server, "UPSTREAM_HOST_ALLOWLIST", "*")
    monkeypatch.setattr(server, "_LAST_ALLOW_ANY_LOG", 0.0)
    with caplog.at_level(logging.ERROR, logger="sepehr-stream"):
        assert server.upstream_host_allowed("https://evil.example/a")
        assert server.upstream_host_allowed("https://evil.example/b")
    errors = [rec for rec in caplog.records if "allowing every upstream host" in rec.message]
    assert len(errors) == 1
