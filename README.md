# Telegram Link Resolver

A webhook-based Telegram bot for resolving publicly accessible HTTP(S) redirect chains.

## Features

- HTTP 301/302/303/307/308 redirect tracing.
- Static HTML, JSON, query-wrapper, and base64 destination extraction.
- Isolated headless Chromium fallback for ordinary JavaScript-driven navigation.
- SQLite cache for successful resolutions (24-hour default TTL).
- SQLite-backed per-user rate limiting (40 requests/hour by default).
- Telegram commands: `/start`, `/bypass URL`, `/help`, and `/about`.
- SSRF checks on the initial URL and browser-request URLs.

## Render deployment

Deploy this repository as a Render Blueprint. The build installs Playwright Chromium. Set `BOT_TOKEN` and (optionally) `GEMINI_API_KEY` in the Render service Environment page. Never commit credentials.

The free web service may spin down while idle. Its filesystem is ephemeral, so the SQLite cache and rate-limit history can be lost after a restart or redeploy. A persistent disk or managed database is needed for durable storage, and may incur cost.

## Local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Limits and safety

The browser fallback handles ordinary JavaScript navigation; it does not click advertisements, solve CAPTCHAs, bypass login/paywall or anti-bot challenges, rotate proxies to evade rate limits, or replay protected gateway tokens. Some sites require a real user session and cannot be resolved automatically.

SSRF checks are best-effort and should be supplemented with network-level egress controls for production use. A resolved URL is not a safety certification.
