import os
import secrets

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip() or secrets.token_urlsafe(32)
MAX_REDIRECTS = max(1, min(int(os.getenv("MAX_REDIRECTS", "10")), 15))
REQUEST_TIMEOUT = max(3.0, min(float(os.getenv("REQUEST_TIMEOUT", "12")), 20.0))
