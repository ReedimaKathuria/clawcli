from __future__ import annotations
import json, os
import httpx
KEY = os.getenv("GEMINI_API_KEY", "")
PROVIDER = "gemini" if KEY else None
MODEL = os.getenv("LLM_MODEL") or "gemini-2.5-flash"

def enabled():
    return bool(KEY)

async def complete(system, user, max_tokens=800):
    if not KEY:
        return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
        body = {"system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": 2048, "responseMimeType": "application/json"}}
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(url, headers={"x-goog-api-key": KEY}, json=body)
            r.raise_for_status()
            return "".join(p.get("text", "") for p in r.json()["candidates"][0]["content"]["parts"])
    except Exception as e:
        print("[llm] gemini call failed:", type(e).__name__, getattr(getattr(e, "response", None), "status_code", ""))
        return None

def parse_json(text):
    try:
        return json.loads(text[text.find("{"):text.rfind("}") + 1])
    except Exception:
        return None
