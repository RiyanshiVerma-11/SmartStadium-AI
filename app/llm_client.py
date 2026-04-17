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
        print(f"[AI-DIAGNOSTIC] Gemini Narrator Initialized. Enabled: {self.enabled}")
        if self.enabled:
            # Mask key for security: only show first 4 and last 4
            masked = f"{self.api_key[:4]}...{self.api_key[-4:]}" if len(self.api_key) > 8 else "****"
            print(f"[AI-DIAGNOSTIC] API Key Found: {masked}")
        else:
            print("[AI-DIAGNOSTIC] NO API KEY FOUND. Environment variable GEMINI_API_KEY is empty.")

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

        import time
        for attempt in range(2):
            try:
                with urllib.request.urlopen(request, timeout=20) as api_response:
                    response_body = api_response.read().decode("utf-8")
                    parsed = json.loads(response_body)
                    break # Success!
            except urllib.error.HTTPError as e:
                error_body = e.read().decode()
                print(f"[AI-DIAGNOSTIC] API HTTP ERROR {e.code}: {e.reason}")
                return None
            except Exception as e:
                print(f"[AI-DIAGNOSTIC] Connection attempt {attempt+1} failed: {str(e)}")
                if attempt == 0:
                    time.sleep(1)
                    continue
                return None

        candidates = parsed.get("candidates") or []
        if not candidates:
            if "error" in parsed:
                print(f"[AI-DIAGNOSTIC] API returned error: {parsed['error'].get('message')}")
            return None
        
        parts = candidates[0].get("content", {}).get("parts", [])
        text_parts = [part.get("text", "").strip() for part in parts if part.get("text")]
        return " ".join(text_parts).strip() or None
