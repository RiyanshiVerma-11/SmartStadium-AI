import re
from typing import Any

from pydantic import BaseModel


class FanProfile(BaseModel):
    name: str = "Guest 112"
    seat_section: str = "112"
    preferred_gate: str = "gate_c"
    food_preference: str = "vegetarian"
    accessibility_need: str = "none"
    ticket_type: str = "vip"


class DecisionEngine:
    """Hybrid safety-first assistant with deterministic actions and optional LLM narration."""

    GATE_ZONE_MAP = {
        "gate_a": "north_gate",
        "gate_b": "concourse_a",
        "gate_c": "south_gate",
    }

    def __init__(self, narrator: Any | None = None):
        self.narrator = narrator

    async def evaluate(self, state: dict[str, Any], prompt: str, profile: FanProfile) -> dict[str, Any]:
        query = (prompt or "").strip()
        lowered = query.lower()
        intent = self._detect_intent(lowered)

        if intent == "emergency":
            response = self._handle_emergency(state, query, profile)
        elif intent == "shortest_line":
            response = self._handle_shortest_line(state, query, profile)
        elif intent == "restroom":
            response = self._handle_restroom(state, query, profile)
        elif intent == "food":
            response = self._handle_food(state, query, profile)
        elif intent == "gate":
            response = self._handle_gate(state, query, profile)
        elif intent == "accessibility":
            response = self._handle_accessibility(state, query, profile)
        else:
            response = self._handle_general(state, query, profile)

        if self.narrator and self.narrator.enabled:
            llm_text = await self.narrator.explain(state=state, prompt=query, response=response, profile=profile.model_dump())
            if llm_text:
                response["narrative"] = llm_text
                response["provider"] = "gemini"

        if "provider" not in response:
            response["provider"] = "rules"

        return response

    def _detect_intent(self, lowered: str) -> str:
        if re.search(r"\b(emergency|help|lost child|injured|fire|stampede|panic|evacuate|suspicious)\b", lowered):
            return "emergency"
        if re.search(r"\b(shortest|least crowded|fastest|quickest)\b", lowered):
            return "shortest_line"
        if re.search(r"\b(restroom|bathroom|toilet|washroom)\b", lowered):
            return "restroom"
        if re.search(r"\b(food|snack|drink|concession|hot dog|beer)\b", lowered):
            return "food"
        if re.search(r"\b(accessible|wheelchair|elevator|mobility|hearing|vision)\b", lowered):
            return "accessibility"
        if re.search(r"\b(gate|entry|entrance|line)\b", lowered):
            return "gate"
        return "general"

    def _handle_emergency(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        message = (
            f"{profile.name}, I am escalating this to stadium staff now. Stay where you are if it is safe "
            "and move toward the nearest marked staff point."
        )
        reasoning = (
            "Emergency keywords or incident cues were detected, so the assistant bypassed normal routing advice "
            "and prioritized intervention."
        )
        if state.get("emergency_override"):
            message = "Emergency mode is already active. Follow the nearest exit signage and staff instructions immediately."
            reasoning = "Command center evacuation override is active, so the safest action is immediate egress."

        return {
            "intent": "emergency",
            "message": message,
            "reasoning": reasoning,
            "severity": "critical",
            "query": query,
            "confidence": 0.98,
            "escalation_priority": "critical",
            "staffing_recommendation": state["directives"]["staffing_recommendation"]["summary"],
            "action": {"type": "alert_staff", "priority": "high"},
        }

    def _handle_shortest_line(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        gate_name, gate_zone, gate_status = self._best_gate(state, preferred_gate=profile.preferred_gate)
        return {
            "intent": "shortest_line",
            "message": f"{profile.name}, use {gate_name}. It is the least crowded route to Section {profile.seat_section}.",
            "reasoning": (
                f"{gate_name} maps to {self._pretty_label(gate_zone)} and is currently {gate_status}. "
                "That gives the best projected wait time right now."
            ),
            "severity": "info",
            "query": query,
            "confidence": 0.84,
            "escalation_priority": state["directives"]["escalation_priority"],
            "staffing_recommendation": state["directives"]["staffing_recommendation"]["summary"],
            "action": {"type": "route", "target": gate_name},
        }

    def _handle_gate(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        gate_name, gate_zone, gate_status = self._best_gate(state, preferred_gate=profile.preferred_gate)
        return {
            "intent": "gate",
            "message": f"Head to {gate_name}. It matches your route profile and has the smoothest flow at the moment.",
            "reasoning": (
                f"{gate_name} is linked to {self._pretty_label(gate_zone)}, which is currently {gate_status} "
                "relative to the other gate zones."
            ),
            "severity": "info",
            "query": query,
            "confidence": 0.82,
            "escalation_priority": state["directives"]["escalation_priority"],
            "staffing_recommendation": state["directives"]["staffing_recommendation"]["summary"],
            "action": {"type": "route", "target": gate_name},
        }

    def _handle_restroom(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        wait = state["wait_times_minutes"]["restrooms"]
        if wait <= 4:
            message = f"Restrooms look clear right now, with about a {wait}-minute wait."
        elif wait <= 8:
            message = f"Restrooms are manageable right now, around {wait} minutes."
        else:
            message = f"Restrooms are backed up at about {wait} minutes. If you can wait, traffic should ease soon."

        return {
            "intent": "restroom",
            "message": message,
            "reasoning": "Restroom guidance is based on the current simulated live wait-time feed.",
            "severity": "info",
            "query": query,
            "confidence": 0.8,
            "escalation_priority": state["directives"]["escalation_priority"],
            "staffing_recommendation": state["directives"]["staffing_recommendation"]["summary"],
            "action": {"type": "inform", "target": "restrooms"},
        }

    def _handle_food(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        wait = state["wait_times_minutes"]["food_stalls"]
        pickup = state["route_plan"]["food_pickup"]
        if wait <= 8:
            message = f"Good time to order {profile.food_preference} food. Estimated wait is {wait} minutes via {pickup}."
        else:
            message = f"Food lines are around {wait} minutes. I would route you first, then use {pickup} after congestion drops."

        return {
            "intent": "food",
            "message": message,
            "reasoning": "Food recommendations compare the live concession queue with your fan profile and pickup lane.",
            "severity": "info",
            "query": query,
            "confidence": 0.79,
            "escalation_priority": state["directives"]["escalation_priority"],
            "staffing_recommendation": state["directives"]["staffing_recommendation"]["summary"],
            "action": {"type": "inform", "target": "food_stalls"},
        }

    def _handle_accessibility(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        route = state["route_plan"]["accessibility_route"]
        return {
            "intent": "accessibility",
            "message": route,
            "reasoning": "Accessible routing considers your stated accessibility need and the current risk profile of the venue.",
            "severity": "info",
            "query": query,
            "confidence": 0.88,
            "escalation_priority": state["directives"]["escalation_priority"],
            "staffing_recommendation": state["directives"]["staffing_recommendation"]["summary"],
            "action": {"type": "route", "target": "accessible_path"},
        }

    def _handle_general(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        gate_name, _, _ = self._best_gate(state, preferred_gate=profile.preferred_gate)
        scenario = self._pretty_label(state["scenario"])
        improvement = state["evaluation"]["improvements"]["avg_wait_improvement_pct"]
        return {
            "intent": "general",
            "message": (
                f"{scenario} is active. I would route you via {gate_name}, and the AI flow plan is reducing average waits by "
                f"{improvement}% in this scenario."
            ),
            "reasoning": (
                "The assistant compared gate density, service waits, emergency status, and your fan profile before answering."
            ),
            "severity": "info",
            "query": query,
            "confidence": 0.76,
            "escalation_priority": state["directives"]["escalation_priority"],
            "staffing_recommendation": state["directives"]["staffing_recommendation"]["summary"],
            "action": {"type": "inform", "target": gate_name},
        }

    def _best_gate(self, state: dict[str, Any], preferred_gate: str) -> tuple[str, str, str]:
        ranking = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        gate_items = sorted(
            self.GATE_ZONE_MAP.items(),
            key=lambda item: (ranking[state["heatmaps"][item[1]]], 0 if item[0] == preferred_gate else 1),
        )
        gate_key, zone_key = gate_items[0]
        return self._pretty_label(gate_key), zone_key, state["heatmaps"][zone_key]

    @staticmethod
    def _pretty_label(value: str) -> str:
        return " ".join(part.capitalize() for part in value.split("_"))
