import re
import json
import heapq
import time
import logging
import numpy as np
from typing import Any
from functools import lru_cache

from pydantic import BaseModel

logger = logging.getLogger("SmartStadium-AI-Engine")


# Time-based cache for Dijkstra routing results (30-second TTL)
class RouteCache:
    """Simple time-based cache for routing results with 30-second TTL."""
    _cache: dict[str, tuple[Any, float]] = {}
    _ttl_seconds = 30

    @classmethod
    def get(cls, key: str) -> Any | None:
        """Get cached value if not expired."""
        if key in cls._cache:
            value, timestamp = cls._cache[key]
            if time.time() - timestamp < cls._ttl_seconds:
                return value
            # Expired - remove
            del cls._cache[key]
        return None

    @classmethod
    def set(cls, key: str, value: Any) -> None:
        """Store value with current timestamp."""
        cls._cache[key] = (value, time.time())

    @classmethod
    def clear(cls) -> None:
        """Clear all cached routes."""
        cls._cache.clear()


class FanProfile(BaseModel):
    """
    Data model representing a fan's preferences and context.
    
    Attributes:
        name: The fan's display name.
        seat_section: The fan's assigned seating section.
        preferred_gate: The fan's default or preferred entry/exit gate.
        food_preference: Dietary preference for concession routing.
        accessibility_need: Specific accessibility requirements (e.g., wheelchair).
        ticket_type: The tier of the ticket (e.g., vip, standard).
    """
    name: str = "Guest 112"
    seat_section: str = "112"
    preferred_gate: str = "gate_c"
    food_preference: str = "vegetarian"
    accessibility_need: str = "none"
    ticket_type: str = "vip"
    preferred_language: str = "en"


class DecisionEngine:
    """Hybrid safety-first assistant with deterministic actions and optional LLM narration."""

    GATE_ZONE_MAP = {
        "gate_a": "north_gate",
        "gate_b": "concourse_a",
        "gate_c": "south_gate",
        "gate_d": "parking_east",
    }

    # Lightweight adjacency list for graph-based routing (Seat Section -> Concourse -> Gate)
    STADIUM_GRAPH = {
        "112": {"concourse_a": 2, "concourse_b": 5},
        "113": {"concourse_a": 3, "concourse_c": 4},
        "concourse_a": {"112": 2, "113": 3, "gate_a": 1, "gate_b": 6},
        "concourse_b": {"112": 5, "gate_b": 2, "gate_c": 7},
        "concourse_c": {"113": 4, "gate_c": 3, "gate_d": 4},
        "gate_a": {"concourse_a": 1},
        "gate_b": {"concourse_a": 6, "concourse_b": 2},
        "gate_c": {"concourse_b": 7, "concourse_c": 3},
        "gate_d": {"concourse_c": 4},
    }

    # Semantic POI Map: Descriptions mapped to Graph Nodes
    STADIUM_POIS = {
        "north_gate": "Main entrance at the north side with security checkpoints.",
        "south_gate": "South entrance near the main parking structure.",
        "concourse_a": "Main concourse with restrooms, snacks, and burger stalls.",
        "concourse_b": "Secondary concourse serving the east side seating.",
        "concourse_c": "West concourse with accessible elevators and premium lounges.",
        "gate_d": "East exit primarily for parking and ride-share access.",
    }

    def __init__(self, narrator: Any | None = None):
        """
        Initialize the DecisionEngine.

        Args:
            narrator: An optional LLM client (e.g., GeminiService) used to provide
                      natural language explanations and advanced reasoning.
        """
        self.narrator = narrator
        self._poi_embeddings: dict[str, list[float]] = {}

    async def _get_semantic_target(self, query: str) -> str | None:
        """Use vector similarity to find the best POI match for a query."""
        if not self.narrator:
            return None

        # Lazy-load/Cache POI embeddings
        if not self._poi_embeddings:
            nodes = list(self.STADIUM_POIS.keys())
            descriptions = [self.STADIUM_POIS[n] for n in nodes]
            if hasattr(self.narrator, "get_embeddings"):
                embeddings = await self.narrator.get_embeddings(descriptions)
                for node_id, emb in zip(nodes, embeddings):
                    if emb:
                        self._poi_embeddings[node_id] = emb
            elif hasattr(self.narrator, "get_embedding"):
                for node_id, description in self.STADIUM_POIS.items():
                    emb = await self.narrator.get_embedding(description)
                    if emb:
                        self._poi_embeddings[node_id] = emb

        # Return early if no POI embeddings are available to prevent vectorized math crashes
        if not self._poi_embeddings:
            return None

        if not hasattr(self.narrator, "get_embedding"):
            return None

        query_emb = await self.narrator.get_embedding(query)
        if not query_emb:
            return None

        # Sanity Check: If the model name was malformed in the narrator, 
        # we skip semantic targeting to prevent a crash, falling back to rule-engine.
        if not isinstance(query_emb, (list, np.ndarray)) or len(query_emb) == 0:
            logger.error("Invalid embedding received. Skipping semantic target resolution.")
            return None

        # Senior Engineer Refinement: Fully Vectorized Cosine Similarity
        # Handle zero vectors and avoid division by zero using a small epsilon (1e-9)
        nodes = list(self._poi_embeddings.keys())
        embeddings = np.array([self._poi_embeddings[n] for n in nodes], dtype=np.float32)
        query_vector = np.array(query_emb, dtype=np.float32)
        
        # Compute norms and similarity in a single vectorized pass
        node_norms = np.linalg.norm(embeddings, axis=1)
        query_norm = np.linalg.norm(query_vector)
        denominators = (node_norms * query_norm) + 1e-9
        similarities = np.dot(embeddings, query_vector) / denominators
        
        best_idx = np.argmax(similarities)
        if similarities[best_idx] > 0.7:
            return nodes[best_idx]
        return None

    async def evaluate(self, state: dict[str, Any], prompt: str, profile: FanProfile) -> dict[str, Any]:
        """
        Evaluate a fan's query against the current stadium state to generate a decision.

        Args:
            state: The current telemetry and operational state of the stadium.
            prompt: The fan's raw natural language query.
            profile: The FanProfile object containing user preferences.

        Returns:
            A dictionary containing the calculated intent, deterministic response, 
            and optional LLM narrative augmentation.
        """
        query = (prompt or "").strip()
        lowered = query.lower()
        intent = self._detect_intent(lowered)

        # Semantic Search Enhancement
        semantic_node = await self._get_semantic_target(query)
        if semantic_node:
            # If we find a specific location, refine the response
            response = self._handle_general(state, query, profile)
            response["action"] = {"type": "route", "target": semantic_node}
            response["message"] = f"I've found the best spot for you! Head toward {self._pretty_label(semantic_node)}."
            return response

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

        if self.narrator:
            llm_response = await self.narrator.get_decision(
                scenario_label=state["scenario_label"],
                alert_msg=response["message"],
                stadium_state=state,
                user_context=profile.model_dump()
            )
            if llm_response:
                response["narrative"] = llm_response.narrative
                response["accessibility_notes"] = llm_response.accessibility_notes
                if llm_response.suggested_route:
                    response["suggested_route"] = llm_response.suggested_route
                
                # New Decision Fields
                response["crowd_prediction"] = llm_response.crowd_prediction
                response["recommended_gate"] = llm_response.recommended_gate
                response["staff_action"] = llm_response.staff_action
                response["provider"] = "gemini"

        if "provider" not in response:
            response["provider"] = "rules"

        return response

    def _detect_intent(self, lowered: str) -> str:
        """
        Determine the core intent of a query using keyword heuristics.

        Args:
            lowered: The lowercase user query string.

        Returns:
            A string representing the classified intent (e.g., 'emergency', 'food').
        """
        if re.search(r"\b(emergency|help|sos|lost child|injured|fire|stampede|panic|evacuate|suspicious)\b", lowered):
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
        """
        Handle safety-critical emergency intents by prioritizing immediate intervention.

        Args:
            state: Current stadium telemetry.
            query: The user's query.
            profile: The user's profile.

        Returns:
            A high-severity decision dict with escalation priority.
        """
        message = (
            f"🚨 {profile.name}, EMERGENCY protocols activated. I am escalating this to stadium staff now. Stay where you are if it is safe "
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
            "action": {"type": "alert_staff", "priority": "critical", "ui_effect": "modal_flash"},
        }

    def _handle_shortest_line(self, state: dict[str, Any], query: str, profile: FanProfile) -> dict[str, Any]:
        gate_name, gate_zone, gate_status, _ = self._best_gate(state, preferred_gate=profile.preferred_gate)
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
        gate_name, gate_zone, gate_status, _ = self._best_gate(state, preferred_gate=profile.preferred_gate)
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
        gate_name, _, _, _ = self._best_gate(state, preferred_gate=profile.preferred_gate)
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

    def _best_gate(self, state: dict[str, Any], preferred_gate: str, start_node: str = "112") -> tuple[str, str, str, str]:
        """
        Determine the optimal gate using a lightweight graph-based Dijkstra algorithm,
        incorporating real-time crowd density as dynamic edge weights.

        Uses time-based caching (30s TTL) to avoid re-calculating routes when
        nothing has changed, improving latency for repeated queries.
        """
        # Create cache key from routing parameters
        cache_key = f"{start_node}:{preferred_gate}"

        # Check cache first - if recent result exists, return it
        cached_result = RouteCache.get(cache_key)
        if cached_result is not None:
            # Verify the heatmap hasn't changed significantly (compare density values)
            cached_heatmaps = cached_result.get("_heatmaps_hash")
            current_heatmaps = str(sorted(state["heatmaps"].items()))
            if cached_heatmaps == current_heatmaps:
                # Return cached gate result (without internal cache data)
                return (
                    cached_result["gate_name"],
                    cached_result["zone_key"],
                    cached_result["gate_status"],
                    cached_result["gate_id"]
                )

        ranking = {"low": 1.0, "medium": 1.5, "high": 3.0, "critical": 500.0}

        # Calculate dynamic edge weights based on real-time zone density
        distances = {node: float('infinity') for node in self.STADIUM_GRAPH}
        distances[start_node] = 0
        priority_queue = [(0, start_node)]

        while priority_queue:
            current_distance, current_node = heapq.heappop(priority_queue)

            if current_distance > distances[current_node]:
                continue

            for neighbor, weight in self.STADIUM_GRAPH[current_node].items():
                # Apply density penalties to gates
                density_penalty = 1.0
                if neighbor in self.GATE_ZONE_MAP:
                    zone_key = self.GATE_ZONE_MAP[neighbor]
                    density = state["heatmaps"][zone_key]
                    density_penalty = ranking[density]
                    
                    # Proactive Penalty: If a zone has a predictive warning, treat it as more congested
                    # than it currently is to steer fans away before it becomes 'critical'.
                    if any(pw["zone"] == zone_key for pw in state.get("predictive_warnings", [])):
                        density_penalty *= 1.5

                    # Slight bonus for preferred gate
                    if neighbor == preferred_gate:
                        density_penalty *= 0.8

                distance = current_distance + (weight * density_penalty)

                if distance < distances[neighbor]:
                    distances[neighbor] = distance
                    heapq.heappush(priority_queue, (distance, neighbor))

        # Find the best gate
        gates = ["gate_a", "gate_b", "gate_c", "gate_d"]
        best_gate_key = min(gates, key=lambda g: distances[g])
        zone_key = self.GATE_ZONE_MAP[best_gate_key]
        gate_status = state["heatmaps"][zone_key]

        # Cache the result with heatmap hash for validation
        result = {
            "gate_name": self._pretty_label(best_gate_key),
            "zone_key": zone_key,
            "gate_status": gate_status,
            "gate_id": best_gate_key,
            "_heatmaps_hash": str(sorted(state["heatmaps"].items()))
        }
        RouteCache.set(cache_key, result)

        return self._pretty_label(best_gate_key), zone_key, gate_status, best_gate_key

    @staticmethod
    def _pretty_label(value: str) -> str:
        return " ".join(part.capitalize() for part in value.split("_"))
