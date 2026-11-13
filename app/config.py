import hashlib
import os
import secrets

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

# Telegram accepts only A-Z, a-z, 0-9, underscore and hyphen in webhook
# secret_token. Render-generated secrets may contain other characters, so
# hash the configured secret to a fixed-length hexadecimal token.
_raw_webhook_secret = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_SECRET = (
    hashlib.sha256(_raw_webhook_secret.encode("utf-8")).hexdigest()
    if _raw_webhook_secret
    else secrets.token_hex(32)
)

MAX_REDIRECTS = max(1, min(int(os.getenv("MAX_REDIRECTS", "8")), 10))
REQUEST_TIMEOUT = max(3.0, min(float(os.getenv("REQUEST_TIMEOUT", "10")), 20.0))
