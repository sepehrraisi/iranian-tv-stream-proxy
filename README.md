<h1 align="center">Iranian TV Stream Proxy</h1>

<p align="center">
  A resilient, cache-aware HLS proxy and web player for publicly available Iranian television channels.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/HLS-live-E11D48?style=flat-square" alt="HLS live streaming" />
  <img src="https://img.shields.io/badge/tests-35%20passing-22C55E?style=flat-square" alt="35 passing tests" />
  <img src="https://img.shields.io/badge/license-GPL--3.0-7C3AED?style=flat-square" alt="GPL-3.0 license" />
</p>

## Overview

The proxy resolves tokenized HLS playlists for Iranian TV channels, rewrites playlists to local URLs, relays segments, and presents streams through browser, IPTV, or VLC-friendly endpoints.

It was designed for unreliable live-stream origins and multiple simultaneous viewers:

```text
Provider API / official embed
              │
              ▼
      playlist resolver
              │
              ▼
 quality selection + URL rewriting
              │
              ▼
 bounded segment cache + single-flight fetch
              │
       ┌──────┼─────────┐
       ▼      ▼         ▼
    hls.js    VLC    IPTV clients
```

## Included channels

The default catalog contains 16 national Iranian channels, including TV1–TV5, IRINN, Varzesh, Namayesh, Nasim, Mostanad, Pouya, Ofogh, Salamat, Amouzesh, Quran, and Tamasha.

## Features

- Fast provider-API resolution when credentials are configured.
- Headless-browser fallback through the official embed player.
- Master-playlist quality selection and configurable height limits.
- Live-edge offset to avoid chasing not-yet-published CDN segments.
- Lazy segment fetching—idle channels do not consume segment bandwidth.
- Shared single-flight downloads for concurrent viewers.
- Memory-bounded segment cache with immutable segment headers.
- Rotating-token playlist support.
- Upstream hostname allowlisting to prevent arbitrary proxying and SSRF.
- Per-channel traffic, viewer, miss, quality, and cache statistics.
- Browser preview, fullscreen iframe player, M3U playlist, and JSON channel API.
- 35 automated compatibility, security, cache, memory, and playback tests.

## Quick start

```bash
git clone https://github.com/sepehrraisi/iranian-tv-stream-proxy.git
cd iranian-tv-stream-proxy
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

Load `.env` using your process manager or shell, then start:

```bash
uvicorn server:app --host 127.0.0.1 --port 8765
```

Open:

- Player: `http://127.0.0.1:8765/preview/tv2`
- Fullscreen embed: `http://127.0.0.1:8765/iframe/tv2`
- HLS: `http://127.0.0.1:8765/stream/tv2/index.m3u8`
- IPTV playlist: `http://127.0.0.1:8765/playlist.m3u`
- Channel catalog: `http://127.0.0.1:8765/channels`

## Resolution modes

### Provider API

The original OAuth values are defined in Beigi’s [`sepehrstreamgenerator/browser.py`](https://github.com/mobeigi/sepehr-stream-generator/blob/main/sepehrstreamgenerator/browser.py#L18-L22). To import those values without searching or copying them manually:

```bash
# From the parent directory of this project
git clone https://github.com/mobeigi/sepehr-stream-generator.git
cd iranian-tv-stream-proxy
cp .env.example .env
python scripts/import_upstream_oauth.py \
  --source ../sepehr-stream-generator/sepehrstreamgenerator/browser.py
```

The importer finds the consumer key, signature, and token, writes them to `.env`, and never displays their values. Review the upstream repository and its GPL-3.0 license before using them; third-party API values may change or be revoked.

You can also copy an authorized complete OAuth header into one environment variable:

```bash
SEPEHR_OAUTH_AUTHORIZATION='OAuth oauth_consumer_key="...", oauth_nonce="...", oauth_signature="...", oauth_signature_method="HMAC-SHA1", oauth_timestamp="...", oauth_token="...", oauth_version="1.0"'
```

Alternatively, set `SEPEHR_OAUTH_CONSUMER_KEY`, `SEPEHR_OAUTH_SIGNATURE`, and `SEPEHR_OAUTH_TOKEN` separately; the proxy generates the nonce and timestamp for each request.

Only use credentials you are authorized to use. `.env` is excluded from Git and must remain private.

### Official-player fallback

When API credentials are absent or the API response changes, the optional Playwright resolver opens the official embed and captures the HLS request made by the player. Install Chromium and leave `USE_BROWSER_FALLBACK=1`.

This fallback is for streams you are authorized to access. It does not bypass DRM or authentication.

## Performance controls

| Variable | Default | Purpose |
|---|---:|---|
| `FORCE_HEIGHT` | `480` | Preferred rendition height |
| `MAX_HEIGHT` | same as force height | Maximum client-requested height |
| `LIVE_START_OFFSET_SECONDS` | `36` | Playback headroom behind the live edge |
| `CACHE_SEGMENTS` | `8` | Segment count retained per playback window |
| `CACHE_MAX_BYTES` | `128 MiB` | Global memory cap for cached segment bytes |
| `VIEWER_IDLE_SECONDS` | `75` | Stop fetching after the last viewer becomes idle |
| `PREWARM_SEGMENTS` | `0` | Keep disabled to avoid downloading unwatched video |
| `SEGMENT_SINGLE_FLIGHT` | `1` | Share one upstream request among concurrent viewers |

See `.env.example` for the complete configuration.

## Tests

```bash
pytest -q
```

The suite uses a scripted local upstream and never contacts the live provider.

## Production deployment

Use the systemd template in `deploy/`, bind the application to loopback, and place it behind an HTTPS reverse proxy. Keep operational endpoints restricted and never expose signed upstream URLs or credentials.

## Responsible use

This project is a transport and reliability tool. You are responsible for respecting broadcaster terms, copyright, geographic restrictions, and applicable law. Do not use it to bypass authentication, DRM, paywalls, or other access controls.

## Acknowledgments

This project’s Sepehr stream-resolution approach was inspired by [mobeigi/sepehr-stream-generator](https://github.com/mobeigi/sepehr-stream-generator), created by Morteza Beigi. That project demonstrated how to retrieve current tokenized Sepehr stream URLs and generate an M3U playlist.

This repository builds on that foundation with an always-on FastAPI proxy, playlist rewriting, browser fallback, quality selection, lazy fetching, bounded segment caching, concurrent-request collapsing, traffic telemetry, embedded playback, and a dedicated automated test suite.

See [NOTICE](NOTICE) for attribution and licensing details.

## License

Released under the [GNU General Public License v3.0](LICENSE), consistent with the upstream project’s license.
