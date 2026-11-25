"""
Iranian TV HLS Stream Proxy
===========================
Resolves the real (tokenized) sepehrtv HLS playlist on the server, then proxies
the manifests and segments under a local URL. Clients only ever talk to this
server, so any player works: VLC, hls.js, the /htv panel, TVs, etc.

Resolution order per channel:
  1. Sepehr public API (fast, no browser)
  2. Headless Chromium that opens the official embed and intercepts the .m3u8
     the player itself requests (works when the API shape changes)

Run (Ubuntu):
  uvicorn server:app --host 0.0.0.0 --port 8765

Browser preview:
  http://SERVER:8765/preview/tv2

VLC:
  http://SERVER:8765/stream/tv2/index.m3u8

The Sepehr stream-resolution approach was inspired by:
https://github.com/mobeigi/sepehr-stream-generator
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import re
import string
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

# ─── Config ───────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
HOST = os.environ.get("STREAM_HOST", "0.0.0.0")
PORT = int(os.environ.get("STREAM_PORT", "8765"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")

SEPEHR_SITE = "https://sepehrtv.ir"
SEPEHR_FRAME_URL = "https://sepehrtv.ir/frame/t/{slug}"
SEPEHR_API_URL = (
    "https://sepehrapi.sepehrtv.ir/v3/channels/"
    "?key={slug}&include_media_resources=true&include_details=true"
)
SEPEHR_API_ALL_URL = (
    "https://sepehrapi.sepehrtv.ir/v3/channels/1"
    "?page_size=1000&page=1&include_media_resources=true&include_details=true"
)
SEPEHR_OAUTH_CONSUMER_KEY = os.environ.get("SEPEHR_OAUTH_CONSUMER_KEY", "").strip()
SEPEHR_OAUTH_SIGNATURE = os.environ.get("SEPEHR_OAUTH_SIGNATURE", "").strip()
SEPEHR_OAUTH_TOKEN = os.environ.get("SEPEHR_OAUTH_TOKEN", "").strip()

USER_AGENT = os.environ.get(
    "STREAM_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
)
# sepehr CDN rejects requests without these.
UPSTREAM_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{SEPEHR_SITE}/",
    "Origin": SEPEHR_SITE,
}

# A tokenized URL stays valid for a while; refresh proactively and on failure.
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "900"))
RESOLVE_TIMEOUT_SECONDS = int(os.environ.get("RESOLVE_TIMEOUT_SECONDS", "60"))
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("UPSTREAM_TIMEOUT_SECONDS", "20"))
USE_BROWSER_FALLBACK = os.environ.get("USE_BROWSER_FALLBACK", "1") == "1"
CHROMIUM_PATH = os.environ.get("CHROMIUM_PATH", "").strip()
# Pin quality: 720 = prefer 1280x720 (or closest ≤720). 0 = leave master playlist as-is.
FORCE_HEIGHT = int(os.environ.get("FORCE_HEIGHT", "480"))
# Caps ?h= so a client cannot request a taller rendition than FORCE_HEIGHT.
# Unset means "same as FORCE_HEIGHT". 0 disables the cap.
_MAX_HEIGHT_RAW = os.environ.get("MAX_HEIGHT", "").strip()
MAX_HEIGHT = FORCE_HEIGHT if _MAX_HEIGHT_RAW == "" else int(_MAX_HEIGHT_RAW)
# Start this many seconds behind the live edge (Sepehr segments are ~10s each).
# 3–4 segments of headroom stops the player chasing unpublished CDN segments.
LIVE_START_OFFSET_SECONDS = float(os.environ.get("LIVE_START_OFFSET_SECONDS", "36"))
# Keep all channel tokens + playlists warm so opens are instant.
PREWARM_ON_START = os.environ.get("PREWARM_ON_START", "1") == "1"
WARM_INTERVAL_SECONDS = int(os.environ.get("WARM_INTERVAL_SECONDS", "300"))
# Comma-separated slugs to keep warm; empty = all CHANNELS.
PREWARM_SLUGS = [
    s.strip()
    for s in os.environ.get("PREWARM_SLUGS", "").split(",")
    if s.strip()
]
# Cap on how many segments the playback window may hold. The window starts
# LIVE_START_OFFSET_SECONDS behind the live edge and runs forward to the edge.
# SEGMENT_CACHE stores and shares those bytes. It does not by itself poll
# channels nobody is watching; PREWARM_SEGMENTS=1 restores that old behavior.
SEGMENT_CACHE = os.environ.get("SEGMENT_CACHE", "1") == "1"
PREWARM_SEGMENTS = os.environ.get("PREWARM_SEGMENTS", "0") == "1"
# 1 = one upstream GET per segment path, shared by every viewer. 0 = the old
# check-then-download, which can fetch the same segment once per overlap.
SEGMENT_SINGLE_FLIGHT = os.environ.get("SEGMENT_SINGLE_FLIGHT", "1") == "1"
CACHE_SEGMENTS = max(2, int(os.environ.get("CACHE_SEGMENTS", "8")))
# 0 = no byte cap. Default is 128 MiB so the 1 GB box keeps headroom.
CACHE_MAX_BYTES = int(os.environ.get("CACHE_MAX_BYTES", str(128 * 1024 * 1024)))
# 0 = no upstream bandwidth cap.
UPSTREAM_LIMIT_BPS = int(os.environ.get("UPSTREAM_LIMIT_BPS", "0"))
# Suffixes. "*" allows every host and logs an error at most once a minute.
UPSTREAM_HOST_ALLOWLIST = os.environ.get(
    "UPSTREAM_HOST_ALLOWLIST", "sepehrtv.ir,sepehrapi.sepehrtv.ir"
)
BUFFER_POLL_SECONDS = float(os.environ.get("BUFFER_POLL_SECONDS", "1"))
BUFFER_FETCH_CONCURRENCY = max(2, int(os.environ.get("BUFFER_FETCH_CONCURRENCY", "8")))

EMBED_PLAYER_HTML = BASE_DIR / "embed_player.html"

CHANNELS: list[dict[str, Any]] = [
    {"num": 1, "slug": "tv1", "name": "شبکه یک"},
    {"num": 2, "slug": "tv2", "name": "شبکه دو"},
    {"num": 3, "slug": "tv3", "name": "شبکه سه"},
    {"num": 4, "slug": "tv4", "name": "شبکه چهار"},
    {"num": 5, "slug": "tv5", "name": "شبکه پنج"},
    {"num": 6, "slug": "irinn", "name": "شبکه خبر"},
    {"num": 7, "slug": "varzesh", "name": "شبکه ورزش"},
    {"num": 8, "slug": "namayesh", "name": "شبکه نمایش"},
    {"num": 9, "slug": "nasim", "name": "شبکه نسیم"},
    {"num": 10, "slug": "mostanad", "name": "شبکه مستند"},
    {"num": 11, "slug": "pouya", "name": "شبکه پویا"},
    {"num": 12, "slug": "ofogh", "name": "شبکه افق"},
    {"num": 13, "slug": "salamat", "name": "شبکه سلامت"},
    {"num": 14, "slug": "amouzesh", "name": "شبکه آموزش"},
    {"num": 15, "slug": "quran", "name": "شبکه قرآن"},
    {"num": 16, "slug": "tamasha", "name": "شبکه تماشا"},
]
VALID_SLUGS = {ch["slug"] for ch in CHANNELS}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sepehr-stream")

# Empty = /stats is only served to loopback. Otherwise require this token.
STATS_TOKEN = os.environ.get("STATS_TOKEN", "").strip()
# How long a viewer hit counts as "active" on /stats. The buffer uses the same
# window once lazy fetch is enabled (step 2).
VIEWER_IDLE_SECONDS = float(os.environ.get("VIEWER_IDLE_SECONDS", "75"))


def env_flag(name: str, default: str) -> bool:
    """True only when the env var is the string \"1\" (same rule as the old flags)."""
    return os.environ.get(name, default) == "1"


@dataclass
class ChannelMeter:
    upstream_bytes: int = 0
    downstream_bytes: int = 0
    hits: int = 0
    misses: int = 0
    segment_fetches: int = 0
    viewers: dict[str, float] = field(default_factory=dict)
    last_view_at: float = 0.0
    quality: str | None = None
    bytes_per_segment: int = 0


class TrafficMeter:
    """Process-wide byte counters. No CDN URLs are stored here."""

    def __init__(self) -> None:
        self.upstream_bytes = 0
        self.downstream_bytes = 0
        self.hits = 0
        self.misses = 0
        self.singleflight_joins = 0
        self.rejected_hosts = 0
        self.channels: dict[str, ChannelMeter] = {}

    def reset(self) -> None:
        self.__init__()

    def channel(self, slug: str) -> ChannelMeter:
        return self.channels.setdefault(slug, ChannelMeter())

    def note_upstream(self, slug: str, nbytes: int) -> None:
        if nbytes <= 0:
            return
        self.upstream_bytes += nbytes
        if slug:
            self.channel(slug).upstream_bytes += nbytes

    def note_downstream(self, slug: str, nbytes: int) -> None:
        if nbytes <= 0:
            return
        self.downstream_bytes += nbytes
        if slug:
            self.channel(slug).downstream_bytes += nbytes

    def note_segment_fetch(self, slug: str, nbytes: int) -> None:
        """Count an upstream segment GET. A prefetch fill is not a client miss."""
        ch = self.channel(slug) if slug else None
        if ch is not None:
            ch.segment_fetches += 1
            ch.bytes_per_segment = nbytes

    def note_client_miss(self, slug: str) -> None:
        """A viewer segment request that was not already in RAM."""
        self.misses += 1
        if slug:
            self.channel(slug).misses += 1

    def note_hit(self, slug: str, nbytes: int) -> None:
        self.hits += 1
        if slug:
            self.channel(slug).hits += 1
        self.note_downstream(slug, nbytes)

    def note_viewer(self, slug: str, client: str) -> None:
        if slug not in VALID_SLUGS:
            return
        now = time.time()
        ch = self.channel(slug)
        ch.last_view_at = now
        if client:
            idle = VIEWER_IDLE_SECONDS
            ch.viewers[client] = now
            ch.viewers = {k: t for k, t in ch.viewers.items() if now - t < idle}

    def active_slugs(self) -> list[str]:
        now = time.time()
        idle = VIEWER_IDLE_SECONDS
        return [
            slug
            for slug, ch in self.channels.items()
            if slug in VALID_SLUGS and ch.last_view_at and now - ch.last_view_at < idle
        ]

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        idle = VIEWER_IDLE_SECONDS
        rows: dict[str, Any] = {}
        active = 0
        viewers = 0
        for slug, ch in self.channels.items():
            recent = sum(1 for t in ch.viewers.values() if now - t < idle)
            is_active = bool(ch.last_view_at and now - ch.last_view_at < idle)
            if is_active:
                active += 1
            viewers += recent
            rows[slug] = {
                "upstream_bytes": ch.upstream_bytes,
                "downstream_bytes": ch.downstream_bytes,
                "hits": ch.hits,
                "misses": ch.misses,
                "segment_fetches": ch.segment_fetches,
                "viewers": recent,
                "last_view_seconds_ago": (
                    round(now - ch.last_view_at, 1) if ch.last_view_at else None
                ),
                "quality": ch.quality,
                "bytes_per_segment": ch.bytes_per_segment,
            }
        attempts = self.hits + self.misses
        return {
            "upstream_bytes": self.upstream_bytes,
            "downstream_bytes": self.downstream_bytes,
            "segment_hits": self.hits,
            "segment_misses": self.misses,
            "hit_rate": (self.hits / attempts) if attempts else 0.0,
            "singleflight_joins": self.singleflight_joins,
            "rejected_hosts": self.rejected_hosts,
            "active_channels": active,
            "viewers": viewers,
            "channels": rows,
        }

    def log_snapshot(self) -> None:
        snap = self.snapshot()
        logger.info(
            "traffic upstream_bytes=%s downstream_bytes=%s active_channels=%s "
            "viewers=%s hit_rate=%.3f rejected_hosts=%s",
            snap["upstream_bytes"],
            snap["downstream_bytes"],
            snap["active_channels"],
            snap["viewers"],
            snap["hit_rate"],
            snap["rejected_hosts"],
        )
        for slug, row in snap["channels"].items():
            logger.info(
                "traffic channel=%s upstream_bytes=%s downstream_bytes=%s "
                "viewers=%s hits=%s misses=%s",
                slug,
                row["upstream_bytes"],
                row["downstream_bytes"],
                row["viewers"],
                row["hits"],
                row["misses"],
            )


traffic = TrafficMeter()


def _client_host(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return ""


def _stats_authorized(request: Request) -> bool:
    if STATS_TOKEN:
        header = request.headers.get("authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied:
            supplied = request.query_params.get("token", "")
        return supplied == STATS_TOKEN
    host = _client_host(request)
    return host in {"127.0.0.1", "::1"}


async def _traffic_log_loop() -> None:
    try:
        while True:
            await asyncio.sleep(60)
            traffic.log_snapshot()
    except asyncio.CancelledError:
        pass


# ─── Embed URL helpers (same ?logo=0 quirk handled by /htv) ──────────────────


def _toggle_logo_param(url: str) -> str:
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    has_logo_0 = any(k.lower() == "logo" and v == "0" for k, v in pairs)
    if has_logo_0:
        pairs = [(k, v) for k, v in pairs if not (k.lower() == "logo" and v == "0")]
    else:
        pairs.append(("logo", "0"))
    return urlunparse(parsed._replace(query=urlencode(pairs)))


async def _embed_url_ok(client: httpx.AsyncClient, url: str) -> bool | None:
    try:
        resp = await client.get(url, headers=UPSTREAM_HEADERS, follow_redirects=True)
    except Exception:
        return None
    if resp.status_code == 404:
        return False
    if "یافت نشد" in resp.text or "not found" in resp.text.lower():
        return False
    return resp.status_code < 400


async def resolve_embed_url(client: httpx.AsyncClient, url: str) -> str:
    if await _embed_url_ok(client, url) is True:
        return url
    alt = _toggle_logo_param(url)
    if alt != url and await _embed_url_ok(client, alt) is True:
        return alt
    return url


# ─── URL packing for the proxy (opaque, keeps query strings intact) ──────────


def pack_url(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


_LAST_ALLOW_ANY_LOG = 0.0


def upstream_host_allowed(url: str) -> bool:
    """True for http(s) hosts on UPSTREAM_HOST_ALLOWLIST. `*` is an emergency valve."""
    global _LAST_ALLOW_ANY_LOG
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    entries = [
        item.strip().lower().rstrip(".")
        for item in UPSTREAM_HOST_ALLOWLIST.split(",")
        if item.strip()
    ]
    if "*" in entries:
        now = time.time()
        if now - _LAST_ALLOW_ANY_LOG >= 60:
            _LAST_ALLOW_ANY_LOG = now
            logger.error("UPSTREAM_HOST_ALLOWLIST=* is allowing every upstream host")
        return True
    return any(host == suffix or host.endswith("." + suffix) for suffix in entries if suffix != "*")


def ensure_upstream_allowed(url: str, *, slug: str = "") -> None:
    if upstream_host_allowed(url):
        return
    traffic.rejected_hosts += 1
    logger.warning("rejected upstream host slug=%s", slug or "-")
    raise HTTPException(status_code=403, detail="Upstream host is not allowed")


async def throttle_upstream(nbytes: int) -> None:
    """Optional pause so a bug cannot pull faster than UPSTREAM_LIMIT_BPS."""
    limit = UPSTREAM_LIMIT_BPS
    if limit <= 0 or nbytes <= 0:
        return
    await asyncio.sleep(nbytes / limit)


def canonical_segment_key(slug: str, url: str) -> str:
    """Identity of a media file, ignoring the auth query viewers do not share."""
    parsed = urlparse(url)
    return f"{slug}|{parsed.scheme}|{parsed.netloc.lower()}|{parsed.path}"


def unpack_url(token: str) -> str:
    padding = "=" * (-len(token) % 4)
    try:
        return base64.urlsafe_b64decode(token + padding).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Malformed upstream token") from exc


# ─── Stream resolution ────────────────────────────────────────────────────────


def _collect_m3u8_urls(node: Any, found: list[str]) -> None:
    """Walk arbitrary JSON and collect every .m3u8 URL it contains."""
    if isinstance(node, str):
        if ".m3u8" in node and node.startswith("http"):
            found.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_m3u8_urls(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_m3u8_urls(value, found)


def _rank_playlist(url: str) -> int:
    """Prefer master playlists so the client can pick its own bitrate."""
    lowered = url.lower()
    if "master" in lowered or "playlist" in lowered:
        return 0
    return 1


def _oauth_authorization() -> str | None:
    """Build the provider API header without embedding credentials in source."""
    if not all(
        (SEPEHR_OAUTH_CONSUMER_KEY, SEPEHR_OAUTH_SIGNATURE, SEPEHR_OAUTH_TOKEN)
    ):
        return None
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
    timestamp = int(time.time())
    return (
        f'OAuth oauth_consumer_key="{SEPEHR_OAUTH_CONSUMER_KEY}", '
        f'oauth_nonce="{nonce}", '
        f'oauth_signature="{SEPEHR_OAUTH_SIGNATURE}", '
        'oauth_signature_method="HMAC-SHA1", '
        f'oauth_timestamp="{timestamp}", '
        f'oauth_token="{SEPEHR_OAUTH_TOKEN}", '
        'oauth_version="1.0"'
    )


def _pick_stream_src(payload: Any) -> str | None:
    """Prefer channel['streams'][0]['src'], else any .m3u8 found in the JSON."""
    # Canonical shape: { "list": [ { "streams": [ { "src": "…m3u8" } ] } ] }
    try:
        items = payload.get("list") if isinstance(payload, dict) else None
        if isinstance(items, list):
            for channel in items:
                streams = channel.get("streams") or []
                for stream in streams:
                    src = stream.get("src") if isinstance(stream, dict) else None
                    if isinstance(src, str) and src.startswith("http"):
                        return src
    except Exception:
        pass

    found: list[str] = []
    _collect_m3u8_urls(payload, found)
    if not found:
        return None
    found.sort(key=_rank_playlist)
    return found[0]


async def resolve_via_api(client: httpx.AsyncClient, slug: str) -> str | None:
    authorization = _oauth_authorization()
    if authorization is None:
        logger.debug("Provider API credentials are unset; skipping API resolver")
        return None
    url = SEPEHR_API_URL.format(slug=slug)
    headers = {
        **UPSTREAM_HEADERS,
        "Authorization": authorization,
    }
    try:
        resp = await fetch_upstream(url, slug=slug, headers=headers)
        if resp.status_code != 200:
            logger.info("API resolve for %s returned HTTP %s", slug, resp.status_code)
            return None
        data = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("API resolve for %s failed: %s", slug, exc)
        return None

    src = _pick_stream_src(data)
    if not src:
        logger.info("API resolve for %s returned no stream src", slug)
        return None
    logger.info("Resolved %s via API: %s", slug, src[:160])
    return src


async def resolve_all_via_api(client: httpx.AsyncClient) -> dict[str, str]:
    """One API call → tokenized .m3u8 for every channel (much faster warm-up)."""
    authorization = _oauth_authorization()
    if authorization is None:
        logger.debug("Provider API credentials are unset; skipping batch API resolver")
        return {}
    headers = {
        **UPSTREAM_HEADERS,
        "Authorization": authorization,
    }
    try:
        resp = await fetch_upstream(SEPEHR_API_ALL_URL, slug="", headers=headers)
        if resp.status_code != 200:
            logger.info("Batch API resolve returned HTTP %s", resp.status_code)
            return {}
        data = resp.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("Batch API resolve failed: %s", exc)
        return {}

    out: dict[str, str] = {}
    items = data.get("list") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}

    for channel in items:
        if not isinstance(channel, dict):
            continue
        uid = channel.get("uid") or channel.get("key")
        if not isinstance(uid, str) or uid not in VALID_SLUGS:
            continue
        streams = channel.get("streams") or []
        src = None
        for stream in streams:
            if isinstance(stream, dict):
                candidate = stream.get("src")
                if isinstance(candidate, str) and candidate.startswith("http"):
                    src = candidate
                    break
        if src:
            out[uid] = src

    logger.info("Batch API resolved %s/%s channels", len(out), len(VALID_SLUGS))
    return out


async def resolve_via_browser(client: httpx.AsyncClient, slug: str) -> str | None:
    """Open the official embed headlessly and capture the .m3u8 the player loads."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright not installed — browser fallback unavailable")
        return None

    embed_url = await resolve_embed_url(client, SEPEHR_FRAME_URL.format(slug=slug))
    logger.info("Browser resolve for %s: opening %s", slug, embed_url)
    captured: list[str] = []
    first_hit = asyncio.Event()

    launch_kwargs: dict[str, Any] = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
        ],
    }
    if CHROMIUM_PATH:
        launch_kwargs["executable_path"] = CHROMIUM_PATH

    playwright = await async_playwright().start()
    browser = None
    try:
        browser = await playwright.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="fa-IR",
            viewport={"width": 1280, "height": 720},
        )
        page = await context.new_page()

        def on_request(request: Any) -> None:
            url = request.url
            if ".m3u8" in url and url.startswith("http"):
                captured.append(url)
                first_hit.set()

        page.on("request", on_request)
        await page.goto(embed_url, wait_until="domcontentloaded", timeout=45000)

        # Nudge autoplay in case the player waits for a gesture.
        try:
            await page.evaluate(
                """async () => {
                    const v = document.querySelector('video');
                    if (v) { v.muted = true; try { await v.play(); } catch (_) {} }
                }"""
            )
        except Exception:
            pass

        try:
            await asyncio.wait_for(first_hit.wait(), timeout=RESOLVE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.warning("Browser resolve for %s saw no .m3u8 request", slug)
            return None

        # Give the player a moment in case a master playlist follows the first hit.
        await asyncio.sleep(2)
    except Exception as exc:
        logger.warning("Browser resolve for %s failed: %s", slug, exc)
        return None
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        try:
            await playwright.stop()
        except Exception:
            pass

    if not captured:
        return None
    captured.sort(key=_rank_playlist)
    logger.info("Resolved %s via browser: %s", slug, captured[0][:140])
    return captured[0]


@dataclass
class ResolvedStream:
    slug: str
    url: str
    source: str
    resolved_at: float = field(default_factory=time.time)
    # Forced-quality media playlist URL (after picking 720p from master).
    media_url: str | None = None
    quality: str | None = None
    last_ok_at: float = field(default_factory=time.time)

    @property
    def age(self) -> float:
        return time.time() - self.resolved_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "source": self.source,
            "age_seconds": round(self.age, 1),
            "ready": self.media_url is not None or bool(self.url),
            "quality": self.quality,
            "upstream": self.url,
            "media_url": self.media_url,
            "hls_url": f"{PUBLIC_BASE_URL}/stream/{self.slug}/index.m3u8",
            "last_ok_seconds_ago": round(time.time() - self.last_ok_at, 1),
        }


class StreamResolver:
    def __init__(self) -> None:
        self._cache: dict[str, ResolvedStream] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._warm_task: asyncio.Task[None] | None = None
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS),
            follow_redirects=True,
        )

    def _lock(self, slug: str) -> asyncio.Lock:
        if slug not in self._locks:
            self._locks[slug] = asyncio.Lock()
        return self._locks[slug]

    def warm_slugs(self) -> list[str]:
        if PREWARM_SLUGS:
            return [s for s in PREWARM_SLUGS if s in VALID_SLUGS]
        return [str(ch["slug"]) for ch in CHANNELS]

    def cached(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._cache.values()]

    def invalidate(self, slug: str) -> None:
        self._cache.pop(slug, None)

    async def get(self, slug: str, *, force: bool = False) -> ResolvedStream:
        if slug not in VALID_SLUGS:
            raise HTTPException(status_code=404, detail=f"Unknown channel slug: {slug}")

        cached = self._cache.get(slug)
        if cached and not force and cached.age < TOKEN_TTL_SECONDS:
            return cached

        async with self._lock(slug):
            cached = self._cache.get(slug)
            if cached and not force and cached.age < TOKEN_TTL_SECONDS:
                return cached

            url = await resolve_via_api(self.client, slug)
            source = "api"
            if not url and USE_BROWSER_FALLBACK:
                url = await resolve_via_browser(self.client, slug)
                source = "browser"

            if not url:
                raise HTTPException(
                    status_code=502,
                    detail=f"Could not resolve a stream URL for {slug}",
                )

            resolved = ResolvedStream(slug=slug, url=url, source=source)
            self._cache[slug] = resolved
            return resolved

    async def prepare_media(self, stream: ResolvedStream) -> ResolvedStream:
        """Resolve forced-quality media playlist URL and touch the CDN token."""
        try:
            body, effective_url = await fetch_playlist_with_retry(
                stream.slug, upstream_url=stream.url
            )
            media_url = effective_url
            quality: str | None = None

            target_h = capped_height(None)
            if target_h > 0 and "#EXT-X-STREAM-INF" in body:
                variants = parse_master_variants(body, effective_url)
                chosen = pick_variant(variants, target_h)
                if chosen:
                    quality = (
                        f"{chosen.width}x{chosen.height}"
                        if chosen.height
                        else f"bw={chosen.bandwidth}"
                    )
                    media_url = chosen.url
                    # Touch the media playlist so the token stays hot.
                    await fetch_playlist_with_retry(stream.slug, upstream_url=media_url)

            stream.media_url = media_url
            stream.quality = quality
            stream.last_ok_at = time.time()
            self._cache[stream.slug] = stream
        except Exception as exc:
            logger.warning("prepare_media failed for %s: %s", stream.slug, exc)
        return stream

    async def warm_all(self, *, force: bool = True) -> dict[str, str]:
        """Refresh every configured channel so streams open instantly."""
        wanted = set(self.warm_slugs())
        logger.info("Warming %s channels…", len(wanted))

        batch = await resolve_all_via_api(self.client)
        now = time.time()
        for slug, url in batch.items():
            if slug not in wanted:
                continue
            self._cache[slug] = ResolvedStream(
                slug=slug, url=url, source="api-batch", resolved_at=now
            )

        missing = [s for s in wanted if s not in batch]
        for slug in missing:
            try:
                await self.get(slug, force=True)
            except Exception as exc:
                logger.warning("Warm resolve failed for %s: %s", slug, exc)

        # Prefetch 720p media URLs in parallel (bounded).
        sem = asyncio.Semaphore(4)

        async def _prep(slug: str) -> None:
            stream = self._cache.get(slug)
            if not stream:
                return
            async with sem:
                await self.prepare_media(stream)

        await asyncio.gather(*[_prep(s) for s in wanted], return_exceptions=True)

        ready = sum(1 for s in wanted if s in self._cache)
        logger.info("Warm complete: %s/%s channels cached", ready, len(wanted))
        return {s: self._cache[s].url for s in wanted if s in self._cache}

    async def warm_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(60, WARM_INTERVAL_SECONDS))
                try:
                    await self.warm_all(force=True)
                except Exception as exc:
                    logger.exception("Warm loop error: %s", exc)
        except asyncio.CancelledError:
            pass

    def start_warmer(self) -> None:
        if self._warm_task and not self._warm_task.done():
            return
        self._warm_task = asyncio.create_task(self.warm_loop())

    async def stop_warmer(self) -> None:
        if self._warm_task:
            self._warm_task.cancel()
            try:
                await self._warm_task
            except asyncio.CancelledError:
                pass
            self._warm_task = None

    async def close(self) -> None:
        await self.stop_warmer()
        await self.client.aclose()


def _parse_media_segments(body: str, base_url: str) -> list[tuple[float, str, int]]:
    """Return [(duration, absolute_url, media_sequence), …] from a media playlist."""
    target = 10
    media_seq = 0
    pending_dur = 10.0
    all_entries: list[tuple[float, str]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = max(1, int(float(line.split(":", 1)[1])))
            except ValueError:
                pass
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                media_seq = int(line.split(":", 1)[1])
            except ValueError:
                pass
        elif line.startswith("#EXTINF:"):
            try:
                pending_dur = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                pending_dur = float(target)
        elif line and not line.startswith("#"):
            all_entries.append((pending_dur, urljoin(base_url, line)))

    first_seq = media_seq
    return [
        (dur, url, first_seq + idx)
        for idx, (dur, url) in enumerate(all_entries)
    ]


def live_playback_entries(
    entries: list[tuple[float, str, int]],
    *,
    offset_seconds: float | None = None,
    limit: int | None = None,
) -> list[tuple[float, str, int]]:
    """Segments a player starting `offset_seconds` behind the live edge will request.

    The injected `#EXT-X-START` seeks to `end - offset` and hls.js then buffers
    forward toward the edge. Segments entirely older than that seek are not
    requested. `limit` (CACHE_SEGMENTS) caps the window, keeping the seek point
    rather than the newest edge. Offset 0 keeps the newest `limit` segments.
    """
    if not entries:
        return []
    cap = CACHE_SEGMENTS if limit is None else max(1, limit)
    offset = LIVE_START_OFFSET_SECONDS if offset_seconds is None else offset_seconds
    if offset <= 0:
        return list(entries[-cap:])
    total = sum(dur for dur, _url, _seq in entries)
    start_at = max(0.0, total - offset)
    cursor = 0.0
    begin = 0
    for index, (dur, _url, _seq) in enumerate(entries):
        # `>` so a seek that lands on a boundary starts at that segment.
        if cursor + dur > start_at + 1e-6:
            begin = index
            break
        cursor += dur
    window = entries[begin:]
    if len(window) > cap:
        window = window[:cap]
    return window


@dataclass
class CachedSegment:
    token: str
    url: str
    duration: float
    data: bytes
    content_type: str
    media_sequence: int
    stored_at: float = field(default_factory=time.time)


@dataclass
class ChannelRing:
    slug: str
    segments: list[CachedSegment] = field(default_factory=list)
    target_duration: int = 10
    quality: str | None = None
    updated_at: float = 0.0
    error: str | None = None

    @property
    def ready(self) -> bool:
        return len(self.segments) >= 2

    def playlist(self) -> str:
        if not self.segments:
            return ""
        first_seq = self.segments[0].media_sequence
        lines = [
            "#EXTM3U",
            f"#EXT-X-START:TIME-OFFSET=-{LIVE_START_OFFSET_SECONDS:.3f},PRECISE=YES",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{self.target_duration}",
            f"#EXT-X-MEDIA-SEQUENCE:{first_seq}",
        ]
        for seg in self.segments:
            lines.append(f"#EXTINF:{seg.duration:.6f},")
            lines.append(f"/stream/{self.slug}/s/{seg.token}")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "ready": self.ready,
            "segments": len(self.segments),
            "quality": self.quality,
            "age_seconds": round(time.time() - self.updated_at, 1) if self.updated_at else None,
            "error": self.error,
            "bytes": sum(len(s.data) for s in self.segments),
        }


class SegmentBuffer:
    """Keeps the latest CACHE_SEGMENTS for every warm channel in memory."""

    def __init__(self, res: StreamResolver) -> None:
        self.resolver = res
        self.rings: dict[str, ChannelRing] = {}
        self._by_token: dict[str, CachedSegment] = {}
        self._by_canonical: dict[str, CachedSegment] = {}
        self._inflight: dict[str, asyncio.Task] = {}
        self._task: asyncio.Task[None] | None = None
        self._sem = asyncio.Semaphore(BUFFER_FETCH_CONCURRENCY)
        self._slug_locks: dict[str, asyncio.Lock] = {}

    def _lock(self, slug: str) -> asyncio.Lock:
        if slug not in self._slug_locks:
            self._slug_locks[slug] = asyncio.Lock()
        return self._slug_locks[slug]

    def get_segment(self, token: str) -> CachedSegment | None:
        return self._by_token.get(token)

    def _remember(self, slug: str, token: str, seg: CachedSegment) -> None:
        self._by_token[token] = seg
        if SEGMENT_SINGLE_FLIGHT:
            self._by_canonical[canonical_segment_key(slug, seg.url)] = seg
        self._evict_to_cap()

    def _unique_segments(self) -> list[CachedSegment]:
        seen: set[int] = set()
        out: list[CachedSegment] = []
        for seg in list(self._by_token.values()) + list(self._by_canonical.values()):
            marker = id(seg)
            if marker in seen:
                continue
            seen.add(marker)
            out.append(seg)
        return out

    def _evict_to_cap(self) -> None:
        cap = CACHE_MAX_BYTES
        if cap <= 0:
            return
        segs = self._unique_segments()
        total = sum(len(seg.data) for seg in segs)
        if total <= cap:
            return
        on_ring = {id(seg) for ring in self.rings.values() for seg in ring.segments}
        orphans = sorted(
            (seg for seg in segs if id(seg) not in on_ring),
            key=lambda seg: seg.stored_at,
        )
        ringed = sorted(
            (seg for seg in segs if id(seg) in on_ring),
            key=lambda seg: seg.stored_at,
        )
        for victim in orphans + ringed:
            if total <= cap:
                break
            total -= len(victim.data)
            self._forget(victim)
            for ring in self.rings.values():
                ring.segments = [seg for seg in ring.segments if seg is not victim]

    def _forget(self, seg: CachedSegment) -> None:
        for token, other in list(self._by_token.items()):
            if other is seg:
                self._by_token.pop(token, None)
        for key, other in list(self._by_canonical.items()):
            if other is seg:
                self._by_canonical.pop(key, None)

    def canonical_hit(self, slug: str, token: str, url: str) -> CachedSegment | None:
        if not SEGMENT_SINGLE_FLIGHT:
            return None
        hit = self._by_canonical.get(canonical_segment_key(slug, url))
        if hit is not None:
            self._by_token[token] = hit
        return hit

    async def _download_shared(self, slug: str, url: str) -> tuple[bytes, str]:
        """One in-flight GET per canonical segment. Waiters share that task."""
        if not SEGMENT_SINGLE_FLIGHT:
            return await self._download(url, slug=slug)
        key = canonical_segment_key(slug, url)
        # No await between the check and the insert, so two coroutines on this
        # loop cannot both miss.
        existing = self._inflight.get(key)
        if existing is None:
            existing = asyncio.create_task(self._download(url, slug=slug))
            self._inflight[key] = existing
            owner = True
        else:
            owner = False
            traffic.singleflight_joins += 1
        try:
            return await existing
        finally:
            if owner and self._inflight.get(key) is existing:
                self._inflight.pop(key, None)

    def get_ring(self, slug: str) -> ChannelRing | None:
        return self.rings.get(slug)

    async def _download(self, url: str, *, slug: str = "") -> tuple[bytes, str]:
        async with self._sem:
            resp = await fetch_upstream(url, slug=slug)
        if resp.status_code >= 400:
            raise RuntimeError(f"segment HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "video/MP2T")
        body = resp.content
        traffic.note_segment_fetch(slug, len(body))
        await throttle_upstream(len(body))
        return body, ctype

    async def _ensure_cached(
        self, slug: str, dur: float, url: str, seq: int
    ) -> CachedSegment | None:
        token = pack_url(url)
        hit = self.get_segment(token) or self.canonical_hit(slug, token, url)
        if hit is not None:
            hit.media_sequence = seq
            hit.duration = dur
            self._remember(slug, token, hit)
            return hit
        try:
            data, ctype = await self._download_shared(slug, url)
        except Exception as exc:
            logger.debug("prefetch failed %s: %s", slug, exc)
            return None
        existing = self.canonical_hit(slug, token, url)
        if existing is not None:
            existing.media_sequence = seq
            existing.duration = dur
            return existing
        seg = CachedSegment(
            token=token,
            url=url,
            duration=dur,
            data=data,
            content_type=ctype,
            media_sequence=seq,
        )
        self._remember(slug, token, seg)
        return seg

    async def prefetch_playlist(self, slug: str, body: str, base_url: str) -> None:
        """Download the live-offset playback window into RAM before the player asks."""
        if not SEGMENT_CACHE or "#EXT-X-STREAM-INF" in body:
            return
        entries = _parse_media_segments(body, base_url)
        # Playhead first. The live edge is included only when it sits inside
        # the offset window the player will actually buffer.
        todo = live_playback_entries(entries)
        if not todo:
            return
        await asyncio.gather(
            *[self._ensure_cached(slug, dur, url, seq) for dur, url, seq in todo],
            return_exceptions=True,
        )

    async def download_and_cache(
        self, slug: str, token: str, url: str
    ) -> CachedSegment:
        hit = self.get_segment(token) or self.canonical_hit(slug, token, url)
        if hit is not None:
            return hit
        data, ctype = await self._download_shared(slug, url)
        existing = self.canonical_hit(slug, token, url)
        if existing is not None:
            return existing
        seg = CachedSegment(
            token=token,
            url=url,
            duration=10.0,
            data=data,
            content_type=ctype,
            media_sequence=0,
        )
        self._remember(slug, token, seg)
        return seg

    async def refresh_slug(self, slug: str) -> None:
        ring = self.rings.setdefault(slug, ChannelRing(slug=slug))
        async with self._lock(slug):
            try:
                body, effective_url, quality = await fetch_forced_quality_playlist(slug)
                ring.quality = quality

                parsed = _parse_media_segments(body, effective_url)
                if not parsed:
                    ring.error = "empty playlist"
                    return

                target = 10
                for raw in body.splitlines():
                    if raw.strip().startswith("#EXT-X-TARGETDURATION:"):
                        try:
                            target = max(1, int(float(raw.split(":", 1)[1])))
                        except ValueError:
                            pass
                        break

                keep = live_playback_entries(parsed)
                existing = {s.url: s for s in ring.segments}

                new_segs: list[CachedSegment] = []
                for dur, url, seq in keep:
                    hit = existing.get(url)
                    if hit is not None:
                        hit.media_sequence = seq
                        hit.duration = dur
                        new_segs.append(hit)
                        self._by_token[hit.token] = hit
                        continue
                    cached = await self._ensure_cached(slug, dur, url, seq)
                    if cached is not None:
                        new_segs.append(cached)

                for old in ring.segments:
                    if old.token not in {s.token for s in new_segs}:
                        self._forget(old)
                ring.segments = new_segs
                ring.target_duration = target
                ring.updated_at = time.time()
                ring.error = None
            except Exception as exc:
                ring.error = str(exc)
                logger.warning("Segment buffer refresh failed for %s: %s", slug, exc)

    def slugs_to_buffer(self) -> list[str]:
        """Channels the background loop may download segments for."""
        if PREWARM_SEGMENTS:
            return self.resolver.warm_slugs()
        return traffic.active_slugs()

    async def refresh_all(self, *, slugs: list[str] | None = None) -> None:
        if slugs is None:
            slugs = self.slugs_to_buffer()
        if not slugs:
            return
        await asyncio.gather(
            *[self.refresh_slug(s) for s in slugs], return_exceptions=True
        )

    async def loop(self) -> None:
        try:
            while True:
                try:
                    await self.refresh_all()
                except Exception as exc:
                    logger.exception("Segment buffer loop error: %s", exc)
                await asyncio.sleep(max(1.0, BUFFER_POLL_SECONDS))
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if not SEGMENT_CACHE:
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self.loop())
        logger.info(
            "Segment buffer started (keep=%s poll=%.1fs concurrency=%s)",
            CACHE_SEGMENTS,
            BUFFER_POLL_SECONDS,
            BUFFER_FETCH_CONCURRENCY,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> dict[str, Any]:
        rings = [self.rings[s].to_dict() for s in self.resolver.warm_slugs() if s in self.rings]
        return {
            "enabled": SEGMENT_CACHE,
            "keep_segments": CACHE_SEGMENTS,
            "poll_seconds": BUFFER_POLL_SECONDS,
            "ready_count": sum(1 for r in rings if r["ready"]),
            "total_bytes": sum(r["bytes"] for r in rings),
            "channels": rings,
        }


resolver = StreamResolver()
segment_buffer = SegmentBuffer(resolver)


# ─── Playlist rewriting ───────────────────────────────────────────────────────

_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')
_RES_RE = re.compile(r"RESOLUTION=(\d+)x(\d+)", re.I)
_BW_RE = re.compile(r"BANDWIDTH=(\d+)", re.I)


@dataclass
class HlsVariant:
    url: str
    width: int | None = None
    height: int | None = None
    bandwidth: int = 0


def parse_master_variants(body: str, base_url: str) -> list[HlsVariant]:
    """Parse #EXT-X-STREAM-INF entries from a master playlist."""
    variants: list[HlsVariant] = []
    lines = [ln.strip() for ln in body.splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXT-X-STREAM-INF:"):
            info = line
            i += 1
            while i < len(lines) and not lines[i]:
                i += 1
            if i < len(lines) and not lines[i].startswith("#"):
                res = _RES_RE.search(info)
                bw = _BW_RE.search(info)
                variants.append(
                    HlsVariant(
                        url=urljoin(base_url, lines[i]),
                        width=int(res.group(1)) if res else None,
                        height=int(res.group(2)) if res else None,
                        bandwidth=int(bw.group(1)) if bw else 0,
                    )
                )
        i += 1
    return variants


def capped_height(requested: int | None) -> int:
    """Height to select. `None` uses FORCE_HEIGHT. Values above MAX_HEIGHT clamp down."""
    target = FORCE_HEIGHT if requested is None else int(requested)
    if MAX_HEIGHT > 0 and target > MAX_HEIGHT:
        return MAX_HEIGHT
    return target


_MEDIA_ONLY_LOGGED: set[str] = set()


def _note_direct_media_playlist(slug: str) -> None:
    if slug in _MEDIA_ONLY_LOGGED:
        return
    _MEDIA_ONLY_LOGGED.add(slug)
    logger.info(
        "Upstream for %s is already a media playlist; FORCE_HEIGHT is not applied",
        slug,
    )


def pick_variant(variants: list[HlsVariant], target_height: int) -> HlsVariant | None:
    """Prefer exact target height, else highest ≤ target, else lowest above target."""
    if not variants:
        return None

    exact = [v for v in variants if v.height == target_height]
    if exact:
        return max(exact, key=lambda v: v.bandwidth)

    below = [v for v in variants if v.height is not None and v.height <= target_height]
    if below:
        return max(below, key=lambda v: (v.height or 0, v.bandwidth))

    above = [v for v in variants if v.height is not None and v.height > target_height]
    if above:
        return min(above, key=lambda v: (v.height or 0, -v.bandwidth))

    # No RESOLUTION tags — pick a mid-tier bandwidth (typical 720p range).
    by_bw = sorted(variants, key=lambda v: v.bandwidth)
    return by_bw[min(len(by_bw) - 1, max(0, len(by_bw) // 2))]


def _proxy_path(slug: str, absolute_url: str, *, playlist: bool) -> str:
    kind = "p" if playlist else "s"
    return f"/stream/{slug}/{kind}/{pack_url(absolute_url)}"


def rewrite_playlist(body: str, slug: str, base_url: str) -> str:
    """Point every manifest/segment reference at this server."""
    is_master = "#EXT-X-STREAM-INF" in body
    out: list[str] = []

    for raw_line in body.splitlines():
        line = raw_line.strip()

        if not line:
            out.append(raw_line)
            continue

        # Drop upstream START tags — we inject our own offset below.
        if line.startswith("#EXT-X-START:"):
            continue

        if line.startswith("#"):
            # EXT-X-MEDIA points at playlists; EXT-X-KEY / EXT-X-MAP point at files.
            def replace_uri(match: re.Match[str]) -> str:
                target = urljoin(base_url, match.group(1))
                as_playlist = line.startswith("#EXT-X-MEDIA")
                return f'URI="{_proxy_path(slug, target, playlist=as_playlist)}"'

            out.append(_URI_ATTR_RE.sub(replace_uri, raw_line))
            continue

        absolute = urljoin(base_url, line)
        out.append(_proxy_path(slug, absolute, playlist=is_master))

    rewritten = "\n".join(out) + "\n"

    # Media playlists only: start N seconds earlier than the live edge.
    if (
        not is_master
        and LIVE_START_OFFSET_SECONDS > 0
        and "#EXTM3U" in rewritten
    ):
        start_tag = (
            f"#EXT-X-START:TIME-OFFSET=-{LIVE_START_OFFSET_SECONDS:.3f},PRECISE=YES"
        )
        rewritten = rewritten.replace("#EXTM3U", f"#EXTM3U\n{start_tag}", 1)

    return rewritten


async def fetch_upstream(
    url: str, *, slug: str = "", headers: dict[str, str] | None = None
) -> httpx.Response:
    """GET one URL, following redirects only when each hop stays on the allowlist."""
    current = url
    for _hop in range(6):
        ensure_upstream_allowed(current, slug=slug)
        resp = await resolver.client.get(
            current,
            headers=headers or UPSTREAM_HEADERS,
            follow_redirects=False,
        )
        traffic.note_upstream(slug, len(resp.content))
        if resp.status_code not in {301, 302, 303, 307, 308}:
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        current = urljoin(str(resp.url), location)
    raise HTTPException(status_code=502, detail="Too many upstream redirects")


async def fetch_playlist_with_retry(slug: str, *, upstream_url: str | None = None) -> tuple[str, str]:
    """Return (body, effective_url). Re-resolves once if the token expired."""
    if upstream_url is None:
        stream = await resolver.get(slug)
        upstream_url = stream.url

    resp = await fetch_upstream(upstream_url, slug=slug)
    if resp.status_code in (401, 403, 404, 410):
        logger.info(
            "Upstream %s for %s — re-resolving token", resp.status_code, slug
        )
        resolver.invalidate(slug)
        stream = await resolver.get(slug, force=True)
        upstream_url = stream.url
        resp = await fetch_upstream(upstream_url, slug=slug)

    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Upstream returned HTTP {resp.status_code} for {slug}",
        )

    return resp.text, str(resp.url)


async def fetch_forced_quality_playlist(
    slug: str, *, height: int | None = None
) -> tuple[str, str, str | None]:
    """Fetch playlist, pin to target height when the upstream is a master.

    Returns (body, effective_url, quality_label).
    Uses pre-warmed media_url when available so opens are instant.
    """
    target_h = capped_height(height)
    stream = await resolver.get(slug)
    quality_label = stream.quality

    # Reuse warm media only when no explicit override (warm matches FORCE_HEIGHT).
    if (
        height is None
        and stream.media_url
        and stream.age < TOKEN_TTL_SECONDS
    ):
        try:
            body, effective_url = await fetch_playlist_with_retry(
                slug, upstream_url=stream.media_url
            )
            # If we somehow still got a master, fall through to normal selection.
            if "#EXT-X-STREAM-INF" not in body:
                stream.last_ok_at = time.time()
                return body, effective_url, quality_label
        except Exception as exc:
            logger.info("Warm media URL stale for %s (%s) — re-preparing", slug, exc)
            resolver.invalidate(slug)
            stream = await resolver.get(slug, force=True)

    body, effective_url = await fetch_playlist_with_retry(slug, upstream_url=stream.url)
    quality_label = None

    if target_h > 0 and "#EXT-X-STREAM-INF" in body:
        variants = parse_master_variants(body, effective_url)
        chosen = pick_variant(variants, target_h)
        if chosen:
            quality_label = (
                f"{chosen.width}x{chosen.height}"
                if chosen.height
                else f"bw={chosen.bandwidth}"
            )
            logger.info("Forcing ~%sp for %s → %s", target_h, slug, quality_label)
            body, effective_url = await fetch_playlist_with_retry(
                slug, upstream_url=chosen.url
            )
            if height is None:
                stream.media_url = chosen.url
                stream.quality = quality_label
                stream.last_ok_at = time.time()
        else:
            logger.warning("No variants found to force %sp for %s", target_h, slug)
    else:
        stream.media_url = effective_url
        stream.last_ok_at = time.time()
        if "#EXT-X-STREAM-INF" not in body:
            _note_direct_media_playlist(slug)
            if not quality_label:
                quality_label = "media"
                stream.quality = quality_label

    if quality_label:
        traffic.channel(slug).quality = quality_label
    return body, effective_url, quality_label


# ─── FastAPI app ──────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(_app: FastAPI):
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    log_task = asyncio.create_task(_traffic_log_loop())
    if PREWARM_ON_START:
        # Warm in background so the HTTP server accepts connections immediately.
        async def _boot_warm() -> None:
            try:
                await resolver.warm_all(force=True)
            except Exception as exc:
                logger.exception("Initial warm failed: %s", exc)
            resolver.start_warmer()
            # Start continuous segment prefetch after tokens are warm.
            if SEGMENT_CACHE:
                if PREWARM_SEGMENTS:
                    try:
                        await segment_buffer.refresh_all()
                    except Exception as exc:
                        logger.exception("Initial segment buffer fill failed: %s", exc)
                segment_buffer.start()

        asyncio.create_task(_boot_warm())
    elif SEGMENT_CACHE:
        segment_buffer.start()
    yield
    log_task.cancel()
    try:
        await log_task
    except asyncio.CancelledError:
        pass
    await segment_buffer.stop()
    await resolver.close()


app = FastAPI(title="Sepehr HLS Stream Server", version="2.0.0", lifespan=lifespan)

STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PLAYLIST_MEDIA_TYPE = "application/vnd.apple.mpegurl"
# immutable (default): playlists max-age=1, segments immutable for a day.
# legacy: the previous no-store playlists and max-age=30 segments.
CACHE_CONTROL_MODE = os.environ.get("CACHE_CONTROL_MODE", "immutable").strip().lower()


def playlist_cache_control() -> str:
    if CACHE_CONTROL_MODE == "legacy":
        return "no-cache, no-store"
    return "public, max-age=1"


def segment_cache_control() -> str:
    if CACHE_CONTROL_MODE == "legacy":
        return "public, max-age=30"
    return "public, max-age=86400, immutable"


def cache_headers(*, playlist: bool) -> dict[str, str]:
    control = playlist_cache_control() if playlist else segment_cache_control()
    return {"Cache-Control": control, "Access-Control-Allow-Origin": "*"}


@app.get("/stream/{slug}/index.m3u8")
async def stream_index(slug: str, request: Request, h: int | None = None):
    """Entry point for VLC / hls.js — always proxy the live upstream playlist.

    Segment bytes may still be served from the RAM ring (/s/{token}), but the
    manifest must stay a sliding live window. Serving the ring's fixed 6-segment
    snapshot caused players to stall every ~20s at the end of each batch.
    """
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail=f"Unknown channel slug: {slug}")
    traffic.note_viewer(slug, _client_host(request))

    body, effective_url, quality = await fetch_forced_quality_playlist(slug, height=h)
    rewritten = rewrite_playlist(body, slug, effective_url)
    if SEGMENT_CACHE and "#EXT-X-STREAM-INF" not in body:
        entries = _parse_media_segments(body, effective_url)
        window = live_playback_entries(entries)
        # Wait for the window the player will request (seek point through the
        # edge), not the three newest segments. Downloads run together, so
        # open waits one round trip and the following segment requests hit RAM.
        if window:
            await asyncio.gather(
                *[
                    segment_buffer._ensure_cached(slug, dur, url, seq)
                    for dur, url, seq in window
                ],
                return_exceptions=True,
            )
        asyncio.create_task(segment_buffer.prefetch_playlist(slug, body, effective_url))
    headers = cache_headers(playlist=True)
    ring = segment_buffer.get_ring(slug) if SEGMENT_CACHE else None
    headers["X-Stream-Buffer"] = "live"
    if ring and ring.ready:
        headers["X-Stream-Segment-Cache"] = "warm"
    if quality:
        headers["X-Stream-Quality"] = quality
        traffic.channel(slug).quality = quality
    traffic.note_downstream(slug, len(rewritten.encode("utf-8")))
    return Response(
        content=rewritten,
        media_type=PLAYLIST_MEDIA_TYPE,
        headers=headers,
    )


@app.get("/stream/{slug}/p/{token}")
async def stream_nested_playlist(slug: str, token: str, request: Request):
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail=f"Unknown channel slug: {slug}")
    traffic.note_viewer(slug, _client_host(request))
    body, effective_url = await fetch_playlist_with_retry(slug, upstream_url=unpack_url(token))
    rewritten = rewrite_playlist(body, slug, effective_url)
    traffic.note_downstream(slug, len(rewritten.encode("utf-8")))
    return Response(
        content=rewritten,
        media_type=PLAYLIST_MEDIA_TYPE,
        headers=cache_headers(playlist=True),
    )


@app.get("/stream/{slug}/s/{token}")
async def stream_segment(slug: str, token: str, request: Request):
    """Serve a segment from the RAM buffer when possible; otherwise proxy CDN."""
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail=f"Unknown channel slug: {slug}")
    traffic.note_viewer(slug, _client_host(request))

    cached = segment_buffer.get_segment(token) if SEGMENT_CACHE else None
    if cached is None and SEGMENT_CACHE and SEGMENT_SINGLE_FLIGHT:
        cached = segment_buffer.canonical_hit(slug, token, unpack_url(token))
    if cached is not None:
        headers = cache_headers(playlist=False)
        headers["Content-Length"] = str(len(cached.data))
        headers["X-Stream-Buffer"] = "hit"
        traffic.note_hit(slug, len(cached.data))
        return Response(
            content=cached.data,
            media_type=cached.content_type or "video/MP2T",
            headers=headers,
        )

    upstream_url = unpack_url(token)
    traffic.note_client_miss(slug)
    if SEGMENT_CACHE:
        try:
            cached = await segment_buffer.download_and_cache(slug, token, upstream_url)
            headers = cache_headers(playlist=False)
            headers["Content-Length"] = str(len(cached.data))
            headers["X-Stream-Buffer"] = "miss-cached"
            traffic.note_downstream(slug, len(cached.data))
            return Response(
                content=cached.data,
                media_type=cached.content_type or "video/MP2T",
                headers=headers,
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("Segment cache fill failed %s: %s", slug, exc)

    headers = dict(UPSTREAM_HEADERS)
    if range_header := request.headers.get("range"):
        headers["Range"] = range_header

    async with segment_buffer._sem:
        upstream = await fetch_upstream(upstream_url, slug=slug, headers=headers)

    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Segment fetch failed with HTTP {upstream.status_code}",
        )

    body = upstream.content
    traffic.note_segment_fetch(slug, len(body))
    traffic.note_downstream(slug, len(body))
    passthrough = cache_headers(playlist=False)
    passthrough["Content-Length"] = str(len(body))
    passthrough["X-Stream-Buffer"] = "miss"
    if upstream.headers.get("content-range"):
        passthrough["Content-Range"] = upstream.headers["content-range"]
    return Response(
        content=body,
        status_code=upstream.status_code,
        headers=passthrough,
        media_type=upstream.headers.get("content-type", "video/mp2t"),
    )


@app.get("/preview/{slug}")
async def preview_channel(slug: str, request: Request):
    """Browser player that only loads the local stream from this server."""
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail=f"Unknown channel slug: {slug}")
    traffic.note_viewer(slug, _client_host(request))
    if not EMBED_PLAYER_HTML.exists():
        raise HTTPException(status_code=500, detail="embed_player.html missing")

    channel = next(ch for ch in CHANNELS if ch["slug"] == slug)
    html = EMBED_PLAYER_HTML.read_text(encoding="utf-8")
    html = html.replace("__CHANNEL_NAME__", str(channel["name"]))
    html = html.replace("__HLS_URL__", f"/stream/{slug}/index.m3u8")
    return HTMLResponse(html)


_IFRAME_PLAYER_HTML = """<!DOCTYPE html>
<html lang="fa">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__CHANNEL_NAME__</title>
  <script src="/static/hls.min.js"></script>
  <style>
    html, body {
      margin: 0; padding: 0;
      width: 100%; height: 100%;
      background: #000; overflow: hidden;
    }
    video {
      display: block; width: 100%; height: 100%;
      object-fit: contain; background: #000;
    }
    #status {
      position: absolute; inset: 0;
      display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      gap: 0.6rem; background: rgba(0,0,0,0.88);
      text-align: center; padding: 2rem;
      font-family: system-ui, Tahoma, sans-serif; color: #eee; z-index: 5;
    }
    #status.hidden { display: none; }
    .spin {
      width: 36px; height: 36px;
      border: 3px solid #444; border-top-color: #fff;
      border-radius: 50%; animation: spin .8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #msg { font-size: 0.9rem; opacity: 0.75; margin-top: 0.4rem; }
  </style>
</head>
<body>
  <div id="status">
    <div class="spin"></div>
    <div id="msg">در حال بارگذاری…</div>
  </div>
  <video id="player" autoplay playsinline></video>
  <script>
  (function () {
    const HLS_URL = "__HLS_URL__";
    const video   = document.getElementById('player');
    const status  = document.getElementById('status');
    const msg     = document.getElementById('msg');

    function show(t) { msg.textContent = t; status.classList.remove('hidden'); }
    function hide()  { status.classList.add('hidden'); }

    async function startPlay() {
      video.muted  = false;
      video.volume = 1.0;
      try { await video.play(); } catch (_) {
        video.muted = true;
        try { await video.play(); } catch (_) {}
        const unmute = () => {
          video.muted  = false;
          video.volume = 1.0;
          video.play().catch(() => {});
        };
        window.addEventListener('pointerdown', unmute, { once: true });
        window.addEventListener('keydown',     unmute, { once: true });
      }
    }

    async function waitReady(url, attempts) {
      for (let i = 1; i <= attempts; i++) {
        show('بارگذاری (' + i + '/' + attempts + ')');
        try {
          const r = await fetch(url, { cache: 'no-store' });
          if (r.ok) {
            const text = await r.text();
            if (text.includes('#EXTM3U')) return true;
          }
        } catch (_) {}
        await new Promise(r => setTimeout(r, 2000));
      }
      return false;
    }

    async function play() {
      if (!await waitReady(HLS_URL, 30)) {
        show('جریان در دسترس نیست');
        return;
      }
      show('اتصال…');

      if (window.Hls && Hls.isSupported()) {
        const hls = new Hls({
          enableWorker: true,
          lowLatencyMode: false,
          liveSyncDurationCount: 4,
          liveMaxLatencyDurationCount: 10,
          maxLiveSyncPlaybackRate: 1,
          liveDurationInfinity: true,
          maxBufferLength: 60,
          maxMaxBufferLength: 120,
          backBufferLength: 30,
          startFragPrefetch: true,
          testBandwidth: false,
          abrEwmaDefaultEstimate: 4_000_000,
          fragLoadingTimeOut: 25000,
          fragLoadingMaxRetry: 8,
          manifestLoadingTimeOut: 15000,
          manifestLoadingMaxRetry: 6,
          levelLoadingMaxRetry: 6,
        });
        hls.loadSource(HLS_URL);
        hls.attachMedia(video);
        let started = false;
        const tryStart = () => {
          if (started) return;
          started = true;
          hide();
          startPlay();
        };
        hls.on(Hls.Events.MANIFEST_PARSED, tryStart);
        hls.on(Hls.Events.FRAG_BUFFERED,   tryStart);
        hls.on(Hls.Events.ERROR, (_, data) => {
          if (data.fatal) {
            show('خطا — تلاش مجدد…');
            setTimeout(play, 3000);
          }
        });
      } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
        video.src = HLS_URL;
        video.addEventListener('loadedmetadata', () => { hide(); startPlay(); }, { once: true });
      } else {
        show('مرورگر از HLS پشتیبانی نمی‌کند');
      }
    }

    play();
  })();
  </script>
</body>
</html>"""


@app.get("/iframe/{slug}")
async def iframe_channel(slug: str, request: Request):
    """Clean, iframe-optimised player for embedding in TV and IPTV panels.

    No debug bar, no server info — pure fullscreen HLS player.
    CORS / X-Frame-Options are intentionally open so any origin can embed this.
    """
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail=f"Unknown channel slug: {slug}")
    traffic.note_viewer(slug, _client_host(request))
    channel = next(ch for ch in CHANNELS if ch["slug"] == slug)
    html = _IFRAME_PLAYER_HTML
    html = html.replace("__CHANNEL_NAME__", str(channel["name"]))
    html = html.replace("__HLS_URL__", f"/stream/{slug}/index.m3u8")
    return HTMLResponse(
        html,
        headers={
            # Allow any origin to embed this page in an <iframe>.
            "Content-Security-Policy": "frame-ancestors *",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.get("/playlist.m3u")
async def playlist_m3u():
    lines = ["#EXTM3U"]
    for ch in CHANNELS:
        slug = ch["slug"]
        lines.append(f'#EXTINF:-1 tvg-id="{slug}" tvg-name="{ch["name"]}",{ch["name"]}')
        lines.append(f"{PUBLIC_BASE_URL}/stream/{slug}/index.m3u8")
    return PlainTextResponse("\n".join(lines) + "\n", media_type=PLAYLIST_MEDIA_TYPE)


@app.get("/channels")
async def channels():
    return {
        "channels": [
            {
                **ch,
                "embed_url": SEPEHR_FRAME_URL.format(slug=ch["slug"]),
                "hls_url": f"{PUBLIC_BASE_URL}/stream/{ch['slug']}/index.m3u8",
                "preview_url": f"{PUBLIC_BASE_URL}/preview/{ch['slug']}",
                "iframe_url": f"{PUBLIC_BASE_URL}/iframe/{ch['slug']}",
            }
            for ch in CHANNELS
        ]
    }


@app.get("/stats")
async def stats(request: Request):
    """Upstream vs downstream bytes. Loopback, or Bearer / ?token= when STATS_TOKEN is set.

    The body never includes CDN URLs or playlist tokens.
    """
    if not _stats_authorized(request):
        raise HTTPException(status_code=403, detail="Stats are not available to this client")
    return traffic.snapshot()


@app.get("/sessions")
async def sessions():
    wanted = resolver.warm_slugs()
    cached = {s["slug"]: s for s in resolver.cached()}
    buf = segment_buffer.status()
    return {
        "warm_interval_seconds": WARM_INTERVAL_SECONDS,
        "prewarm_on_start": PREWARM_ON_START,
        "wanted": wanted,
        "ready_count": sum(1 for s in wanted if s in cached),
        "resolved": [cached.get(s) or {"slug": s, "ready": False} for s in wanted],
        "segment_buffer": buf,
    }


@app.post("/warm")
async def warm_now():
    """Force-refresh all channel tokens and refill segment buffers."""
    result = await resolver.warm_all(force=True)
    if SEGMENT_CACHE:
        # Operator action: fill every warm slug, including ones with no viewer.
        await segment_buffer.refresh_all(slugs=resolver.warm_slugs())
    return {
        "warmed": list(result.keys()),
        "count": len(result),
        "segment_buffer": segment_buffer.status(),
    }


@app.get("/resolve/{slug}")
async def resolve_debug(slug: str, force: bool = False):
    """Inspect what upstream URL the server resolved (troubleshooting)."""
    stream = await resolver.get(slug, force=force)
    if force or not stream.media_url:
        await resolver.prepare_media(stream)
    return stream.to_dict()


@app.delete("/stream/{slug}")
async def drop_cached_stream(slug: str):
    if slug not in VALID_SLUGS:
        raise HTTPException(status_code=404, detail=f"Unknown channel slug: {slug}")
    resolver.invalidate(slug)
    return {"invalidated": slug}


@app.get("/")
async def root():
    rows = "".join(
        f"<tr><td>{ch['num']}</td><td>{ch['name']}</td>"
        f"<td>"
        f"<a href='/preview/{ch['slug']}' target='_blank'>preview</a> &middot; "
        f"<a href='/iframe/{ch['slug']}' target='_blank'>iframe</a>"
        f"</td>"
        f"<td><code>{PUBLIC_BASE_URL}/stream/{ch['slug']}/index.m3u8</code></td></tr>"
        for ch in CHANNELS
    )
    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="fa">
<head>
  <meta charset="utf-8" />
  <title>Sepehr HLS Stream Server</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#111; color:#eee; margin:2rem; }}
    a {{ color:#7dd3fc; }}
    table {{ border-collapse: collapse; width:100%; max-width:1100px; }}
    th, td {{ border:1px solid #333; padding:.6rem .8rem; text-align:left; }}
    th {{ background:#1a1a1a; }}
    code {{ font-size:.85rem; color:#a7f3d0; }}
    .box {{ background:#1a1a1a; padding:1rem 1.2rem; border-radius:8px;
            margin-bottom:1.5rem; max-width:1100px; }}
  </style>
</head>
<body>
  <h1>Sepehr HLS Stream Server</h1>
  <div class="box">
    <p>The server resolves the real sepehrtv HLS stream and proxies it.
       Clients only talk to this server — no re-encoding, original quality.</p>
    <p>Browser preview: <a href="/preview/tv2">/preview/tv2</a>
       &middot; Iframe embed: <a href="/iframe/tv2">/iframe/tv2</a></p>
    <p>VLC: <code>{PUBLIC_BASE_URL}/stream/tv2/index.m3u8</code></p>
    <p>All channels: <a href="/playlist.m3u">/playlist.m3u</a>
       &middot; Debug: <a href="/resolve/tv2">/resolve/tv2</a>
       &middot; Cache: <a href="/sessions">/sessions</a></p>
  </div>
  <table>
    <thead><tr><th>#</th><th>Channel</th><th>Preview / iframe</th><th>HLS URL</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</body>
</html>"""
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host=HOST, port=PORT, reload=False)
