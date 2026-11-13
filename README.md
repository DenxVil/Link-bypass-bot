# Telegram Link Resolver

A webhook-based Telegram bot for resolving publicly accessible HTTP(S) redirect chains.

## Render deployment
Deploy this repository as a Render Blueprint. Set `BOT_TOKEN` and (optionally) `GEMINI_API_KEY` in the Render service Environment page. Never commit credentials. The free web service may spin down while idle, so the first webhook after inactivity can be delayed.

## Local
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Limits
Resolves HTTP redirects only. It does not defeat CAPTCHAs, logins, paywalls, JavaScript challenges, anti-bot protections, or other access controls. Some destinations cannot be resolved without a browser session. SSRF protections are best-effort and should be supplemented with network-level egress controls for production use.
