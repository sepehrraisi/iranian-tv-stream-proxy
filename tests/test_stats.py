"""Step 1: /stats and the periodic traffic log. Fetch behavior is unchanged."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

import server


def _as_host(host: str):
    async def app(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["client"] = (host, 50000)
        await server.app(scope, receive, send)

    return app


def test_stats_loopback_shape():
    with TestClient(_as_host("127.0.0.1")) as local:
        played = local.get("/stream/tv2/index.m3u8")
        assert played.status_code == 200
        response = local.get("/stats")
    assert response.status_code == 200
    body = response.json()
    for key in (
        "upstream_bytes",
        "downstream_bytes",
        "segment_hits",
        "segment_misses",
        "hit_rate",
        "singleflight_joins",
        "rejected_hosts",
        "active_channels",
        "viewers",
        "channels",
    ):
        assert key in body
    assert body["upstream_bytes"] > 0
    assert body["downstream_bytes"] > 0
    assert body["viewers"] >= 1
    assert body["active_channels"] >= 1
    row = body["channels"]["tv2"]
    assert row["upstream_bytes"] > 0
    assert "upstream" not in row
    assert "media_url" not in row
    assert "http" not in str(row)


def test_stats_hidden_from_the_internet():
    with TestClient(_as_host("203.0.113.8")) as remote:
        denied = remote.get("/stats")
        assert denied.status_code == 403
        assert "detail" in denied.json()


def test_stats_token(monkeypatch):
    monkeypatch.setattr(server, "STATS_TOKEN", "s3cret")
    with TestClient(_as_host("203.0.113.8")) as remote:
        assert remote.get("/stats").status_code == 403
        bearer = remote.get("/stats", headers={"Authorization": "Bearer s3cret"})
        assert bearer.status_code == 200
        query = remote.get("/stats?token=s3cret")
        assert query.status_code == 200


def test_traffic_log_line_has_bytes_and_no_urls(caplog):
    server.traffic.note_upstream("tv2", 1000)
    server.traffic.note_downstream("tv2", 400)
    server.traffic.note_viewer("tv2", "10.1.1.9")
    with caplog.at_level(logging.INFO, logger="sepehr-stream"):
        server.traffic.log_snapshot()
    text = caplog.text
    assert "upstream_bytes=1000" in text
    assert "downstream_bytes=400" in text
    assert "channel=tv2" in text
    assert "http://" not in text
    assert "m3u8" not in text
