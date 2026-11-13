from google import genai
from google.genai import types

from .config import GEMINI_API_KEY, GEMINI_MODEL


async def explain_destination(url: str) -> str | None:
    if not GEMINI_API_KEY:
        return None
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
        result = await client.aio.models.generate_content(
            model=GEMINI_MODEL,
            contents=f"Give a cautious, brief explanation of what can be inferred from this destination URL: {url}",
            config=types.GenerateContentConfig(
                system_instruction="Do not claim a URL is safe or trustworthy based only on its address. Do not infer facts you cannot verify. Keep it under 50 words."
            ),
        )
        return (result.text or "").strip() or None
    except Exception:
        return None
