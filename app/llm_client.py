import os
import httpx
import json
import asyncio
from typing import Optional, Dict, Any
from pydantic import BaseModel

class AIResponse(BaseModel):
    narrative: str
    accessibility_notes: str
    suggested_route: Optional[str] = None
    crowd_prediction: Optional[str] = None
    recommended_gate: Optional[str] = None
    staff_action: Optional[str] = None

class GeminiNarrator:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"
        
    async def get_decision(self, scenario_label: str, alert_msg: str, stadium_state: Dict[str, Any], user_context: Dict[str, Any]) -> AIResponse:
        """
        Upgraded: Acts as a Decision Engine. Analyzes telemetry to predict 
        crowd flow and recommend operational actions.
        """
        if not self.api_key:
            return self._mock_response(alert_msg)

        prompt = f"""
        You are the SmartStadium AI Decision Engine. Analyze this real-time telemetry:
        Scenario: {scenario_label}
        Alert Message: {alert_msg}
        Crowd Density Data: {json.dumps(stadium_state.get('heatmaps', {}))}
        Wait Times: {json.dumps(stadium_state.get('wait_times_minutes', {}))}
        
        User Context:
        - Name: {user_context.get('name', 'Guest')}
        - Accessibility: {user_context.get('accessibility_need', 'None')}
        - Section: {user_context.get('seat_section', 'Unknown')}

        Task:
        1. Predict the crowd density trend for the next 10 minutes.
        2. Recommend the absolute best entry/exit gate to reduce congestion.
        3. Suggest a specific intervention for stadium staff.
        4. Provide a professional, calm narrative for the fan.

        Output ONLY valid JSON with keys:
        "narrative", "accessibility_notes", "suggested_route", "crowd_prediction", "recommended_gate", "staff_action"
        """

        url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "response_mime_type": "application/json",
            }
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
                
                content = data['candidates'][0]['content']['parts'][0]['text']
                result = json.loads(content)
                return AIResponse(**result)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                print("Gemini API Rate Limit (429) - Switching to safe local fallback. (Note: Free tier usage limit hit, no charges incurred)")
            else:
                print(f"Gemini API HTTP Error: {e}")
            return self._mock_response(alert_msg)
        except Exception as e:
            print(f"Gemini API Error: {e}")
            return self._mock_response(alert_msg)

    def _mock_response(self, alert_msg: str) -> AIResponse:
        return AIResponse(
            narrative=f"We've detected an update: {alert_msg}. Our teams are managing the flow to ensure your comfort. Please follow the illuminated green path for the smoothest exit.",
            accessibility_notes="All primary routes remain accessible. Elevator 4 is reserved for priority access.",
            suggested_route="Proceed to Concourse B via the ramp near Section 110.",
            crowd_prediction="Expect 10% density increase at South Gate soon.",
            recommended_gate="Gate A",
            staff_action="Deploy 2 stewards to Concourse A to assist with flow."
        )

# Singleton instance
gemini_client = GeminiNarrator()
