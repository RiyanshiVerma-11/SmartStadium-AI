import os
import httpx
import json
import asyncio
import logging
from typing import Any, Dict, Optional, List
from pydantic import BaseModel
from cachetools import TTLCache
from google import genai  # Naya SDK
from google.genai import types

logger = logging.getLogger("SmartStadium-Gemini")

class AIResponse(BaseModel):
    narrative: str
    accessibility_notes: str
    suggested_route: Optional[str] = None
    crowd_prediction: Optional[str] = None
    recommended_gate: Optional[str] = None
    staff_action: Optional[str] = None
    suggested_pa_announcement: Optional[str] = None
    
class GeminiService:
    def __init__(self) -> None:
        self.api_key: Optional[str] = os.getenv("GEMINI_API_KEY")
        # Level 4 Refinement: Sanitize model ID to remove environment variable prefixes if present
        raw_model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        self.model_id = raw_model.split("=")[-1] if "=" in raw_model else raw_model
        
        self.client = None
        if self.api_key:
            # Reverted to v1beta for better structured output support (response_mime_type)
            self.client = genai.Client(
                api_key=self.api_key,
                http_options={'api_version': 'v1beta'}
            )
        
        self._cache: TTLCache = TTLCache(maxsize=100, ttl=300)
        self._embedding_cache: TTLCache = TTLCache(maxsize=500, ttl=300)

    async def get_embedding(self, text: str) -> Optional[List[float]]:
        """Naye SDK ke saath optimized embedding."""
        if not self.client:
            logger.warning("Gemini Client not initialized. Check API Key.")
            return None
            
        if text in self._embedding_cache:
            return self._embedding_cache[text]

        try:
            # Optimized async embedding with new SDK
            result = await self.client.aio.models.embed_content(
                model="text-embedding-004", 
                contents=text
            )
            embedding = result.embeddings[0].values
            self._embedding_cache[text] = embedding
            return embedding
        except Exception as e:
            logger.error(f"Embedding Error: {e}")
            return None

    async def get_embeddings(self, texts: List[str]) -> List[Optional[List[float]]]:
        """Naye SDK ke saath batch embedding support."""
        if not self.client:
            return [None] * len(texts)

        results: List[Optional[List[float]]] = [None] * len(texts)
        to_fetch_indices = []
        to_fetch_texts = []

        for i, text in enumerate(texts):
            if text in self._embedding_cache:
                results[i] = self._embedding_cache[text]
            else:
                to_fetch_indices.append(i)
                to_fetch_texts.append(text)

        if not to_fetch_texts:
            return results

        try: # Fixed method name
            result = await self.client.aio.models.embed_content(
                model="text-embedding-004",
                contents=to_fetch_texts
            )
            for idx, emb_data in zip(to_fetch_indices, result.embeddings):
                val = emb_data.values
                self._embedding_cache[texts[idx]] = val
                results[idx] = val
        except Exception as e:
            logger.error(f"Batch Embedding Error: {e}")

        return results

    async def get_decision(
        self, 
        scenario_label: str, 
        alert_msg: str, 
        stadium_state: Dict[str, Any], 
        user_context: Dict[str, Any]
    ) -> AIResponse:
        """Request an AI decision using a direct async call for hackathon reliability."""
        cache_key = f"{scenario_label}:{alert_msg}:{user_context.get('seat_section')}:{user_context.get('accessibility_need')}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if not self.client:
            return self._mock_response(alert_msg)

        # Refactored to simple direct async call
        prompt = (
            "You are the SmartStadium AI Command Center Advisor. Analyze the provided telemetry data and User Context to generate high-precision operational directives. "
            "Your goal is predictive safety and accessibility optimization.\n\n"
            "Return a JSON object with the following keys:\n"
            "- narrative: Calming, professional status update for the fan.\n"
            "- accessibility_notes: Critical mobility-aware guidance based on user needs.\n"
            "- suggested_route: Optimal path from section to gate.\n"
            "- crowd_prediction: Proactive flow analysis (e.g., 'Expect surge in 5 mins').\n"
            "- recommended_gate: Best exit based on heatmap density.\n"
            "- staff_action: Specific operational directive for personnel (incident intent).\n"
            "- suggested_pa_announcement: Authoritative script for stadium-wide broadcast.\n\n"
            f"Scenario: {scenario_label}\n"
            f"Alert: {alert_msg}\n"
            f"Heatmap: {json.dumps(stadium_state.get('heatmaps', {}))}\n"
            f"Predictive Warnings: {json.dumps(stadium_state.get('predictive_warnings', []))}\n"
            f"User Section: {user_context.get('seat_section', 'Unknown')}\n"
            f"Accessibility Needs: {user_context.get('accessibility_need', 'None')}"
        )

        try:
            response = await self.client.aio.models.generate_content(
                model=self.model_id,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )

            raw_text = response.text
            if "```json" in raw_text:
                raw_text = raw_text.split("```json")[1].split("```")[0].strip()
            elif "```" in raw_text:
                raw_text = raw_text.split("```")[1].split("```")[0].strip()

            result = json.loads(raw_text)
            ai_response = AIResponse(**result)
            self._cache[cache_key] = ai_response
            return ai_response
        except Exception as e:
            logger.error(f"Decision API Error: {e}")
            return self._mock_response(alert_msg, scenario_label)

    def _mock_response(self, alert_msg: str, scenario: str = "normal") -> AIResponse:
        prefix = "🚨 EMERGENCY" if scenario in ["suspicious_object", "medical_emergency"] else "ℹ️ Update"
        return AIResponse(
            narrative=f"{prefix}: {alert_msg}. Following standard safety protocols.",
            accessibility_notes="All priority routes are open.",
            suggested_route="Follow green markers to Gate A.",
            crowd_prediction="Stable flow.",
            recommended_gate="Gate A",
            staff_action="Monitor flow near Section 110.",
            suggested_pa_announcement=f"{prefix}: {alert_msg}. Please follow staff guidance."
        )

# Export for main.py
gemini_client = GeminiService()