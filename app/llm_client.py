import json
import os
import urllib.error
import urllib.request
from typing import Any


class GeminiNarrator:
    """Optional Gemini summarizer for richer natural-language explanations."""

    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()
        self.enabled = bool(self.api_key)

    async def explain(self, state: dict[str, Any], prompt: str, response: dict[str, Any], profile: dict[str, Any]) -> str | None:
        if not self.enabled:
            return None

        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
            f"?key={self.api_key}"
        )
        instruction = (
            "You are a stadium operations AI assistant. Expand the deterministic routing answer into a concise, "
            "practical explanation for a hackathon demo. Do not invent sensors or actions that are not present. "
            "Keep it under 70 words. Mention personalization and safety when relevant."
        )
        payload = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "instruction": instruction,
                                    "prompt": prompt,
                                    "profile": profile,
                                    "scenario": state["scenario"],
                                    "route_plan": state["route_plan"],
                                    "risk_level": state["risk_level"],
                                    "response": response,
                                }
                            )
                        }
                    ]
                }
            ]
        }
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=8) as api_response:
                parsed = json.loads(api_response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError):
            return None

        candidates = parsed.get("candidates") or []
        if not candidates:
            return None
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "").strip() for part in parts if part.get("text")]
        return " ".join(text_parts).strip() or None
