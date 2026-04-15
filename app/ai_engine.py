import re
from typing import Any


class DecisionEngine:
    """Rule-based stadium assistant tuned for live demo reliability."""

    GATE_ZONE_MAP = {
        "gate_a": "north_gate",
        "gate_b": "concourse_a",
        "gate_c": "south_gate",
    }

    def evaluate(self, state: dict[str, Any], prompt: str) -> dict[str, Any]:
        query = (prompt or "").strip()
        lowered = query.lower()
        intent = self._detect_intent(lowered)

        if intent == "emergency":
            return self._handle_emergency(state, query)
        if intent == "shortest_line":
            return self._handle_shortest_line(state, query)
        if intent == "restroom":
            return self._handle_restroom(state, query)
        if intent == "food":
            return self._handle_food(state, query)
        if intent == "gate":
            return self._handle_gate(state, query)

        return self._handle_general(state, query)

    def _detect_intent(self, lowered: str) -> str:
        if re.search(r"\b(emergency|help|lost child|injured|fire|stampede|panic|evacuate)\b", lowered):
            return "emergency"
        if re.search(r"\b(shortest|least crowded|fastest|quickest)\b", lowered):
            return "shortest_line"
        if re.search(r"\b(restroom|bathroom|toilet|washroom)\b", lowered):
            return "restroom"
        if re.search(r"\b(food|snack|drink|concession|hot dog|beer)\b", lowered):
            return "food"
        if re.search(r"\b(gate|entry|entrance|line)\b", lowered):
            return "gate"
        return "general"

    def _handle_emergency(self, state: dict[str, Any], query: str) -> dict[str, Any]:
        message = (
            "I am escalating this to stadium staff now. Stay where you are if it is safe, "
            "or move toward the nearest marked staff point."
        )
        reasoning = (
            "Emergency keywords were detected, so the assistant bypassed normal routing advice "
            "and prioritized staff intervention."
        )
        if state.get("emergency_override"):
            message = (
                "Emergency mode is already active. Follow the nearest exit signage and staff instructions immediately."
            )
            reasoning = (
                "The venue is in evacuation mode, so the safest action is to follow the active exit route."
            )

        return {
            "intent": "emergency",
            "message": message,
            "reasoning": reasoning,
            "severity": "critical",
            "query": query,
            "action": {"type": "alert_staff", "priority": "high"},
        }

    def _handle_shortest_line(self, state: dict[str, Any], query: str) -> dict[str, Any]:
        gate_name, gate_zone, gate_status = self._best_gate(state)
        return {
            "intent": "shortest_line",
            "message": f"I recommend {gate_name}, it's the least crowded right now.",
            "reasoning": (
                f"{gate_name} maps to {self._pretty_label(gate_zone)} and is currently {gate_status}. "
                "That makes it the best route for a faster entry."
            ),
            "severity": "info",
            "query": query,
            "action": {"type": "route", "target": gate_name},
        }

    def _handle_gate(self, state: dict[str, Any], query: str) -> dict[str, Any]:
        gate_name, gate_zone, gate_status = self._best_gate(state)
        return {
            "intent": "gate",
            "message": f"Head to {gate_name}. It has the smoothest flow at the moment.",
            "reasoning": (
                f"{gate_name} is linked to {self._pretty_label(gate_zone)}, which is currently {gate_status} "
                "compared with the other gate zones."
            ),
            "severity": "info",
            "query": query,
            "action": {"type": "route", "target": gate_name},
        }

    def _handle_restroom(self, state: dict[str, Any], query: str) -> dict[str, Any]:
        wait = state["wait_times_minutes"]["restrooms"]
        if wait <= 4:
            message = f"Restrooms look clear right now, with about a {wait}-minute wait."
        elif wait <= 8:
            message = f"Restrooms are manageable right now, around {wait} minutes."
        else:
            message = f"Restrooms are backed up right now at about {wait} minutes. If you can wait, traffic should ease soon."

        return {
            "intent": "restroom",
            "message": message,
            "reasoning": "Restroom guidance is based on the current live wait-time feed from the venue telemetry stream.",
            "severity": "info",
            "query": query,
            "action": {"type": "inform", "target": "restrooms"},
        }

    def _handle_food(self, state: dict[str, Any], query: str) -> dict[str, Any]:
        wait = state["wait_times_minutes"]["food_stalls"]
        if wait <= 8:
            message = f"This is a good time to grab food. The food stalls are only about {wait} minutes right now."
        else:
            message = (
                f"Food lines are running around {wait} minutes right now. If you're flexible, wait a bit or use the calmer gate route first."
            )

        return {
            "intent": "food",
            "message": message,
            "reasoning": "Food recommendations compare the live concession wait time against the current crowd pattern.",
            "severity": "info",
            "query": query,
            "action": {"type": "inform", "target": "food_stalls"},
        }

    def _handle_general(self, state: dict[str, Any], query: str) -> dict[str, Any]:
        gate_name, _, _ = self._best_gate(state)
        scenario = self._pretty_label(state["scenario"])
        return {
            "intent": "general",
            "message": f"Things look stable in {scenario}. If you need the quickest route, I would send you to {gate_name}.",
            "reasoning": (
                "The assistant compared current gate density, service wait times, and emergency status before answering."
            ),
            "severity": "info",
            "query": query,
            "action": {"type": "inform", "target": gate_name},
        }

    def _best_gate(self, state: dict[str, Any]) -> tuple[str, str, str]:
        ranking = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        gate_key, zone_key = min(
            self.GATE_ZONE_MAP.items(),
            key=lambda item: ranking[state["heatmaps"][item[1]]],
        )
        return self._pretty_label(gate_key), zone_key, state["heatmaps"][zone_key]

    @staticmethod
    def _pretty_label(value: str) -> str:
        return " ".join(part.capitalize() for part in value.split("_"))
