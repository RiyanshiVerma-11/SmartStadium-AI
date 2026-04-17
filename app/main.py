import asyncio
import json
import os
import time
from dotenv import load_dotenv

load_dotenv()
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.ai_engine import DecisionEngine, FanProfile
from app.llm_client import GeminiNarrator
from app.storage import Storage

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("SMARTSTADIUM_DB_PATH", DATA_DIR / "smartstadium.db"))

SCENARIO_LIBRARY: dict[str, dict[str, Any]] = {}

SCENARIO_LIBRARY.update(
    {
        "normal": {
            "label": "Normal Operations",
            "description": "Balanced ingress with predictable wait times and no active incidents.",
            "risk_level": "low",
            "incident_type": "routine_flow",
            "google_services": ["Google Maps Embed"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "medium",
                        "south_gate": "low",
                        "concourse_a": "medium",
                        "food_court": "medium",
                        "parking_east": "low",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 7,
                        "restrooms": 3,
                        "merch_stand": 6,
                        "security_check": 5,
                    },
                    "alerts": [
                        {
                            "type": "info",
                            "msg": "Operations stable. AI balancing keeps guest movement within target range.",
                            "reasoning": "No zone exceeds high density and all service queues remain inside comfort thresholds.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 14,
                        "max_zone_density": 72,
                        "reroute_success_rate": 48,
                        "evacuation_response_seconds": 210,
                        "guest_satisfaction": 72,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 8,
                        "max_zone_density": 44,
                        "reroute_success_rate": 81,
                        "evacuation_response_seconds": 142,
                        "guest_satisfaction": 88,
                    },
                    "recommended_actions": [
                        "Keep two ambassadors near North Gate for soft nudges toward Gate C.",
                        "Maintain one floating concessions runner near Concourse A.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Balanced staffing is sufficient.",
                        "moves": [
                            "1 concierge at North Gate",
                            "1 mobile concessions runner near food court",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "food_court",
                        "minutes_until_peak": 8,
                        "confidence": 0.66,
                    },
                },
                {
                    "heatmaps": {
                        "north_gate": "high",
                        "south_gate": "low",
                        "concourse_a": "medium",
                        "food_court": "medium",
                        "parking_east": "low",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 9,
                        "restrooms": 4,
                        "merch_stand": 7,
                        "security_check": 6,
                    },
                    "alerts": [
                        {
                            "type": "info",
                            "msg": "Arrival pulse at North Gate. Flow bounties are steering fans to the South Gate corridor.",
                            "reasoning": "Entry density is rising at North Gate but adjacent paths remain available.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 16,
                        "max_zone_density": 78,
                        "reroute_success_rate": 44,
                        "evacuation_response_seconds": 218,
                        "guest_satisfaction": 70,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 9,
                        "max_zone_density": 50,
                        "reroute_success_rate": 84,
                        "evacuation_response_seconds": 145,
                        "guest_satisfaction": 86,
                    },
                    "recommended_actions": [
                        "Open two more scan lanes on the north side.",
                        "Push a staff announcement toward South Gate routing.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Shift entry support toward the north side for five minutes.",
                        "moves": [
                            "2 scanners to North Gate",
                            "1 usher near parking east to redirect arrivals",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "north_gate",
                        "minutes_until_peak": 5,
                        "confidence": 0.78,
                    },
                },
            ],
        },
        "peak_rush": {
            "label": "Peak Rush",
            "description": "Kickoff pressure with sustained demand at ingress and concessions.",
            "risk_level": "high",
            "incident_type": "crowd_surge",
            "google_services": ["Google Maps Embed"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "critical",
                        "south_gate": "medium",
                        "concourse_a": "high",
                        "food_court": "high",
                        "parking_east": "medium",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 22,
                        "restrooms": 8,
                        "merch_stand": 14,
                        "security_check": 15,
                    },
                    "alerts": [
                        {
                            "type": "critical",
                            "msg": "Peak rush is active. North Gate saturation is above comfort thresholds.",
                            "reasoning": "Simulated arrival waves are compounding at ingress and central concourse routes.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 25,
                        "max_zone_density": 93,
                        "reroute_success_rate": 33,
                        "evacuation_response_seconds": 248,
                        "guest_satisfaction": 52,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 14,
                        "max_zone_density": 68,
                        "reroute_success_rate": 74,
                        "evacuation_response_seconds": 171,
                        "guest_satisfaction": 73,
                    },
                    "recommended_actions": [
                        "Deploy redirection staff to Gate C immediately.",
                        "Pause promo push notifications near the central food court.",
                    ],
                    "staffing_recommendation": {
                        "summary": "High urgency reallocation recommended.",
                        "moves": [
                            "3 ushers to Gate C corridor",
                            "2 queue marshals to North Gate",
                            "1 medic standby near Concourse A",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "north_gate",
                        "minutes_until_peak": 3,
                        "confidence": 0.9,
                    },
                },
                {
                    "heatmaps": {
                        "north_gate": "high",
                        "south_gate": "medium",
                        "concourse_a": "critical",
                        "food_court": "critical",
                        "parking_east": "medium",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 27,
                        "restrooms": 9,
                        "merch_stand": 15,
                        "security_check": 12,
                    },
                    "alerts": [
                        {
                            "type": "critical",
                            "msg": "Concourse A and central food court are entering red status.",
                            "reasoning": "Demand has shifted inward after heavy gate arrivals, compressing service corridors.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 28,
                        "max_zone_density": 96,
                        "reroute_success_rate": 30,
                        "evacuation_response_seconds": 255,
                        "guest_satisfaction": 49,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 16,
                        "max_zone_density": 70,
                        "reroute_success_rate": 76,
                        "evacuation_response_seconds": 174,
                        "guest_satisfaction": 72,
                    },
                    "recommended_actions": [
                        "Hold walk-up ordering near central concessions for three minutes.",
                        "Push rewards to east-side vendors and lower occupancy routes.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Concourse containment and food rerouting required.",
                        "moves": [
                            "2 staff to east concessions pickup",
                            "2 queue marshals inside Concourse A",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "food_court",
                        "minutes_until_peak": 4,
                        "confidence": 0.87,
                    },
                },
            ],
        },
    }
)

SCENARIO_LIBRARY.update(
    {
        "suspicious_object": {
            "label": "Suspicious Object",
            "description": "Security isolation scenario requiring controlled rerouting and evidence preservation.",
            "risk_level": "critical",
            "incident_type": "security_incident",
            "google_services": ["Gemini API"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "high",
                        "south_gate": "medium",
                        "concourse_a": "critical",
                        "food_court": "high",
                        "parking_east": "medium",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 16,
                        "restrooms": 7,
                        "merch_stand": 12,
                        "security_check": 14,
                    },
                    "alerts": [
                        {
                            "type": "critical",
                            "msg": "Suspicious object investigation underway. Soft exclusion perimeter established in Concourse A.",
                            "reasoning": "Security workflows require low-panic containment and immediate alternate routing.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 23,
                        "max_zone_density": 90,
                        "reroute_success_rate": 34,
                        "evacuation_response_seconds": 230,
                        "guest_satisfaction": 54,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 13,
                        "max_zone_density": 58,
                        "reroute_success_rate": 72,
                        "evacuation_response_seconds": 146,
                        "guest_satisfaction": 74,
                    },
                    "recommended_actions": [
                        "Redirect all nearby fans using silent wayfinding prompts.",
                        "Prevent crowd congregation around the exclusion perimeter.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Security containment with calm guest rerouting.",
                        "moves": [
                            "2 security officers at perimeter",
                            "2 ushers to alternate hall entry",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "concourse_a",
                        "minutes_until_peak": 2,
                        "confidence": 0.93,
                    },
                }
            ],
        },
        "network_outage": {
            "label": "Network Outage",
            "description": "Connectivity degradation forces SMS-style fallback and cached guidance.",
            "risk_level": "medium",
            "incident_type": "network_outage",
            "google_services": ["Google Maps Embed fallback"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "medium",
                        "south_gate": "low",
                        "concourse_a": "medium",
                        "food_court": "high",
                        "parking_east": "low",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 17,
                        "restrooms": 5,
                        "merch_stand": 8,
                        "security_check": 6,
                    },
                    "alerts": [
                        {
                            "type": "info",
                            "msg": "Network disruption detected. Cached routes and offline assistance are active.",
                            "reasoning": "The system is prioritizing resilient guidance while live telemetry partially degrades.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 19,
                        "max_zone_density": 77,
                        "reroute_success_rate": 39,
                        "evacuation_response_seconds": 219,
                        "guest_satisfaction": 57,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 12,
                        "max_zone_density": 54,
                        "reroute_success_rate": 69,
                        "evacuation_response_seconds": 155,
                        "guest_satisfaction": 75,
                    },
                    "recommended_actions": [
                        "Switch staff to offline scripts and cached zone maps.",
                        "Use dynamic signage and PA announcements for major reroutes.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Offline resilience protocol active.",
                        "moves": [
                            "1 IT lead in command center",
                            "2 ushers at signage chokepoints",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "food_court",
                        "minutes_until_peak": 9,
                        "confidence": 0.69,
                    },
                }
            ],
        },
    }
)

SCENARIO_LIBRARY.update(
    {
        "medical_emergency": {
            "label": "Medical Emergency",
            "description": "Localized medical incident near seating section 112 requiring calm rerouting.",
            "risk_level": "high",
            "incident_type": "medical_emergency",
            "google_services": ["Google Maps Embed", "Gemini API"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "medium",
                        "south_gate": "low",
                        "concourse_a": "high",
                        "food_court": "medium",
                        "parking_east": "low",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 10,
                        "restrooms": 4,
                        "merch_stand": 6,
                        "security_check": 5,
                    },
                    "alerts": [
                        {
                            "type": "critical",
                            "msg": "Medical team dispatched to Section 112. Soft cordon active nearby.",
                            "reasoning": "AI is preserving responder access while minimizing disruption to surrounding spectators.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 17,
                        "max_zone_density": 81,
                        "reroute_success_rate": 42,
                        "evacuation_response_seconds": 198,
                        "guest_satisfaction": 63,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 11,
                        "max_zone_density": 55,
                        "reroute_success_rate": 79,
                        "evacuation_response_seconds": 124,
                        "guest_satisfaction": 82,
                    },
                    "recommended_actions": [
                        "Reserve west aisle access for medics.",
                        "Inform nearby fans to use alternate restroom corridor.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Medical corridor protection required.",
                        "moves": [
                            "1 medic team at Section 112",
                            "2 ushers to west aisle cordon",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "concourse_a",
                        "minutes_until_peak": 6,
                        "confidence": 0.74,
                    },
                }
            ],
        },
        "gate_closure": {
            "label": "Gate Closure",
            "description": "One ingress route is closed, forcing rebalancing across alternate entries.",
            "risk_level": "high",
            "incident_type": "gate_closure",
            "google_services": ["Google Maps Embed"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "critical",
                        "south_gate": "high",
                        "concourse_a": "medium",
                        "food_court": "medium",
                        "parking_east": "high",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 13,
                        "restrooms": 5,
                        "merch_stand": 8,
                        "security_check": 18,
                    },
                    "alerts": [
                        {
                            "type": "critical",
                            "msg": "Gate A closed for inspection. Guests are being rerouted to Gate C and East Entry.",
                            "reasoning": "Ingress capacity dropped sharply, so the assistant is balancing arrivals across remaining checkpoints.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 24,
                        "max_zone_density": 91,
                        "reroute_success_rate": 29,
                        "evacuation_response_seconds": 236,
                        "guest_satisfaction": 55,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 13,
                        "max_zone_density": 61,
                        "reroute_success_rate": 77,
                        "evacuation_response_seconds": 162,
                        "guest_satisfaction": 76,
                    },
                    "recommended_actions": [
                        "Open temporary bag-check at East Entry.",
                        "Push wayfinding to parking and rideshare arrivals.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Ingress rebalancing needed immediately.",
                        "moves": [
                            "2 screeners to East Entry",
                            "2 ushers to Gate C",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "security_check",
                        "minutes_until_peak": 4,
                        "confidence": 0.84,
                    },
                }
            ],
        },
        "weather_delay": {
            "label": "Weather Delay",
            "description": "Rain and lightning delay increases indoor crowding and shelter demand.",
            "risk_level": "medium",
            "incident_type": "weather_delay",
            "google_services": ["Google Maps Embed", "Google Weather-compatible planning"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "medium",
                        "south_gate": "medium",
                        "concourse_a": "critical",
                        "food_court": "high",
                        "parking_east": "critical",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 19,
                        "restrooms": 11,
                        "merch_stand": 13,
                        "security_check": 7,
                    },
                    "alerts": [
                        {
                            "type": "critical",
                            "msg": "Weather shelter pattern active. Indoor circulation needs controlled pacing.",
                            "reasoning": "Fans are clustering under cover, causing concourse spillover and restroom demand.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 21,
                        "max_zone_density": 89,
                        "reroute_success_rate": 38,
                        "evacuation_response_seconds": 220,
                        "guest_satisfaction": 58,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 12,
                        "max_zone_density": 62,
                        "reroute_success_rate": 71,
                        "evacuation_response_seconds": 158,
                        "guest_satisfaction": 77,
                    },
                    "recommended_actions": [
                        "Open indoor overflow seating near East Hall.",
                        "Prioritize accessible sheltered routes in all fan messaging.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Shelter coordination and accessibility support required.",
                        "moves": [
                            "2 accessibility staff to East Hall",
                            "2 ushers to parking shelter route",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "parking_east",
                        "minutes_until_peak": 7,
                        "confidence": 0.8,
                    },
                }
            ],
        },
        "lost_child": {
            "label": "Lost Child",
            "description": "A child separation event where staff needs fast identification and calm route control.",
            "risk_level": "high",
            "incident_type": "lost_child",
            "google_services": ["Gemini API"],
            "frames": [
                {
                    "heatmaps": {
                        "north_gate": "medium",
                        "south_gate": "medium",
                        "concourse_a": "high",
                        "food_court": "medium",
                        "parking_east": "low",
                    },
                    "wait_times_minutes": {
                        "food_stalls": 12,
                        "restrooms": 5,
                        "merch_stand": 8,
                        "security_check": 6,
                    },
                    "alerts": [
                        {
                            "type": "critical",
                            "msg": "Lost child protocol active. Staff are narrowing search radius around family zone.",
                            "reasoning": "AI uses last-seen context and crowd density to prioritize search corridors.",
                        }
                    ],
                    "before_ai": {
                        "avg_wait_minutes": 18,
                        "max_zone_density": 79,
                        "reroute_success_rate": 41,
                        "evacuation_response_seconds": 208,
                        "guest_satisfaction": 61,
                    },
                    "after_ai": {
                        "avg_wait_minutes": 10,
                        "max_zone_density": 52,
                        "reroute_success_rate": 76,
                        "evacuation_response_seconds": 135,
                        "guest_satisfaction": 81,
                    },
                    "recommended_actions": [
                        "Quietly alert guest services and nearest ushers.",
                        "Route surrounding fans away from family reunification desk.",
                    ],
                    "staffing_recommendation": {
                        "summary": "Search radius reduction underway.",
                        "moves": [
                            "1 guest services lead at reunification desk",
                            "2 ushers to family zone perimeter",
                        ],
                    },
                    "predicted_bottleneck": {
                        "zone": "concourse_a",
                        "minutes_until_peak": 5,
                        "confidence": 0.71,
                    },
                }
            ],
        },
    }
)

GLOBAL_STATE: dict[str, Any] = {
    "panic_mode": False,
    "scenario": "normal",
    "tick": 0,
    "current_profile": {
        "name": "Guest 112",
        "seat_section": "112",
        "preferred_gate": "gate_c",
        "food_preference": "vegetarian",
        "accessibility_need": "none",
        "ticket_type": "vip",
    },
}


class ScenarioRequest(BaseModel):
    scenario: str


class AskAIRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    profile: FanProfile | None = None


class ProfileRequest(FanProfile):
    pass


def calculate_improvements(before_ai: dict[str, float], after_ai: dict[str, float]) -> dict[str, float]:
    return {
        "avg_wait_improvement_pct": round(
            ((before_ai["avg_wait_minutes"] - after_ai["avg_wait_minutes"]) / before_ai["avg_wait_minutes"]) * 100, 1
        ),
        "density_reduction_pct": round(
            ((before_ai["max_zone_density"] - after_ai["max_zone_density"]) / before_ai["max_zone_density"]) * 100, 1
        ),
        "reroute_gain_pct": round(after_ai["reroute_success_rate"] - before_ai["reroute_success_rate"], 1),
        "evacuation_time_saved_sec": round(
            before_ai["evacuation_response_seconds"] - after_ai["evacuation_response_seconds"], 1
        ),
        "guest_satisfaction_gain": round(after_ai["guest_satisfaction"] - before_ai["guest_satisfaction"], 1),
    }


def build_snapshot() -> dict[str, Any]:
    scenario_key = GLOBAL_STATE["scenario"]
    scenario = SCENARIO_LIBRARY[scenario_key]
    frames = scenario["frames"]
    frame = frames[GLOBAL_STATE["tick"] % len(frames)]
    GLOBAL_STATE["tick"] += 1

    before_ai = dict(frame["before_ai"])
    after_ai = dict(frame["after_ai"])
    improvements = calculate_improvements(before_ai, after_ai)
    profile = dict(GLOBAL_STATE["current_profile"])
    preferred_gate = profile.get("preferred_gate", "gate_c")
    accessibility = profile.get("accessibility_need", "none")
    bounty_points = 60 if scenario_key in {"peak_rush", "gate_closure"} else 35

    bounties = [
        {
            "target": preferred_gate,
            "points": f"+{bounty_points}",
            "reason": f"Use {preferred_gate.replace('_', ' ').title()} to reduce crowding and improve reroute success.",
        }
    ]
    if frame["wait_times_minutes"]["food_stalls"] <= 12:
        bounties.append(
            {
                "target": "food_stalls",
                "points": "+20",
                "reason": f"Grab {profile.get('food_preference', 'fan favorite')} concessions during the calm service window.",
            }
        )

    personalized_route = {
        "best_gate": preferred_gate,
        "seat_section": profile.get("seat_section", "112"),
        "accessibility_route": (
            "Use the low-slope East Hall corridor with elevator access."
            if accessibility != "none"
            else "Use the shortest marked path through Gate C toward Section 112."
        ),
        "food_pickup": "Suite lane pickup" if profile.get("ticket_type") == "vip" else "Express cart near Concourse A",
    }

    directives = {
        "predicted_bottleneck": dict(frame["predicted_bottleneck"]),
        "staffing_recommendation": dict(frame["staffing_recommendation"]),
        "recommended_actions": list(frame["recommended_actions"]),
        "confidence_score": round(frame["predicted_bottleneck"]["confidence"] * 100, 1),
        "escalation_priority": (
            "critical"
            if scenario["risk_level"] == "critical" or GLOBAL_STATE["panic_mode"]
            else "high"
            if scenario["risk_level"] == "high"
            else "medium"
        ),
    }

    alerts = list(frame["alerts"])
    if GLOBAL_STATE["panic_mode"]:
        alerts.append(
            {
                "type": "critical",
                "msg": "Manual evacuation override active. All fans must follow the nearest marked exits.",
                "reasoning": "Command center initiated emergency egress.",
            }
        )

    # Dynamic Match Simulation
    match_tick = GLOBAL_STATE["tick"]
    dynamic_time = 72 + (match_tick // 4)  # Slow clock progression
    score = "2 - 1"
    if dynamic_time > 85 and match_tick % 10 == 0: score = "3 - 1" # Goal simulation

    # Dynamic Weather Simulation
    weather_map = {
        "normal": {"temp": 24, "cond": "Clear Sky", "icon": "☀️"},
        "peak_rush": {"temp": 26, "cond": "Sunny", "icon": "☀️"},
        "suspicious_object": {"temp": 23, "cond": "Overcast", "icon": "☁️"},
        "network_outage": {"temp": 22, "cond": "Partly Cloudy", "icon": "⛅"},
        "medical_emergency": {"temp": 24, "cond": "Clear", "icon": "☀️"},
        "extreme_weather": {"temp": 18, "cond": "Thunderstorm", "icon": "⛈️"}
    }
    w = weather_map.get(scenario_key, {"temp": 24, "cond": "Clear Sky", "icon": "☀️"})
    # Add a slight variation based on tick
    varied_temp = w["temp"] + (1 if GLOBAL_STATE["tick"] % 15 > 7 else 0)

    return {
        "timestamp": int(time.time()),
        "scenario": scenario_key,
        "scenario_label": scenario["label"],
        "scenario_description": scenario["description"],
        "incident_type": scenario["incident_type"],
        "risk_level": scenario["risk_level"],
        "google_services": list(scenario["google_services"]),
        "emergency_override": GLOBAL_STATE["panic_mode"],
        "heatmaps": dict(frame["heatmaps"]),
        "wait_times_minutes": dict(frame["wait_times_minutes"]),
        "alerts": alerts,
        "gamification": {"bounties": bounties},
        "evaluation": {
            "before_ai": before_ai,
            "after_ai": after_ai,
            "improvements": improvements,
        },
        "directives": directives,
        "personalization": profile,
        "route_plan": personalized_route,
        "match_info": {"teams": "Lions vs Tigers", "score": score, "time": f"{dynamic_time}'"},
        "weather": {
            "temp": f"{varied_temp}°C",
            "condition": w["cond"],
            "icon": w["icon"]
        }
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB_PATH)
    storage.initialize()
    narrator = GeminiNarrator()
    decision_engine = DecisionEngine(narrator=narrator)
    app.state.storage = storage
    app.state.decision_engine = decision_engine
    yield


app = FastAPI(title="SmartStadium AI Premium", lifespan=lifespan)
templates = Jinja2Templates(directory="app/templates")


def get_storage() -> Storage:
    return app.state.storage


def get_decision_engine() -> DecisionEngine:
    return app.state.decision_engine


def persist_snapshot(snapshot: dict[str, Any]) -> None:
    get_storage().insert_snapshot(snapshot)
    for alert in snapshot["alerts"]:
        get_storage().log_event(
            event_type="alert",
            scenario=snapshot["scenario"],
            severity=alert["type"],
            summary=alert["msg"],
            details={"reasoning": alert["reasoning"], "risk_level": snapshot["risk_level"]},
        )


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@app.get("/old_landing", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/fan", response_class=HTMLResponse)
async def fan_app(request: Request):
    return templates.TemplateResponse(request=request, name="fan.html")


@app.get("/staff", response_class=HTMLResponse)
async def staff_app(request: Request):
    return templates.TemplateResponse(request=request, name="staff.html")


@app.get("/api/v1/config")
async def get_config():
    return {
        "status": "success",
        "scenarios": [
            {
                "key": key,
                "label": value["label"],
                "description": value["description"],
                "risk_level": value["risk_level"],
                "incident_type": value["incident_type"],
            }
            for key, value in SCENARIO_LIBRARY.items()
        ],
        "stadium_state": {
            "wait_times_minutes": build_snapshot()["wait_times_minutes"]
        },
        "profile": GLOBAL_STATE["current_profile"],
        "llm_enabled": bool(get_decision_engine().narrator and get_decision_engine().narrator.enabled),
        "match_info": {
            "teams": "Lions vs Tigers",
            "score": "2 - 1",
            "time": "72'",
            "venue": "SoFi Stadium",
            "attendance": "68,420"
        },
        "weather": {
            "temp": "24°C",
            "condition": "Clear Sky",
            "icon": "☀️"
        }
    }


@app.get("/api/v1/snapshot")
async def get_snapshot():
    snapshot = build_snapshot()
    persist_snapshot(snapshot)
    return {"status": "success", "snapshot": snapshot}


@app.post("/api/v1/panic")
async def toggle_panic():
    GLOBAL_STATE["panic_mode"] = not GLOBAL_STATE["panic_mode"]
    get_storage().log_operator_action(
        action="panic_toggle",
        scenario=GLOBAL_STATE["scenario"],
        actor="staff_operator",
        details={"panic_mode": GLOBAL_STATE["panic_mode"]},
    )
    return {"status": "success", "panic_mode": GLOBAL_STATE["panic_mode"]}


@app.post("/api/v1/scenario")
async def set_scenario(payload: ScenarioRequest):
    if payload.scenario not in SCENARIO_LIBRARY:
        raise HTTPException(status_code=400, detail="Unsupported scenario.")

    GLOBAL_STATE["scenario"] = payload.scenario
    GLOBAL_STATE["tick"] = 0
    get_storage().log_operator_action(
        action="scenario_change",
        scenario=GLOBAL_STATE["scenario"],
        actor="staff_operator",
        details={"scenario": payload.scenario},
    )
    return {"status": "success", "scenario": GLOBAL_STATE["scenario"]}


@app.post("/api/v1/profile")
async def update_profile(payload: ProfileRequest):
    GLOBAL_STATE["current_profile"] = payload.model_dump()
    get_storage().log_operator_action(
        action="profile_update",
        scenario=GLOBAL_STATE["scenario"],
        actor="fan_user",
        details=GLOBAL_STATE["current_profile"],
    )
    return {"status": "success", "profile": GLOBAL_STATE["current_profile"]}


@app.post("/api/v1/ask_ai")
async def ask_ai(payload: AskAIRequest):
    profile = payload.profile or FanProfile(**GLOBAL_STATE["current_profile"])
    snapshot = build_snapshot()
    response = await get_decision_engine().evaluate(snapshot, payload.prompt, profile)
    get_storage().insert_ai_query(
        scenario=snapshot["scenario"],
        prompt=payload.prompt,
        profile=profile.model_dump(),
        response=response,
    )
    persist_snapshot(snapshot)
    return {
        "status": "success",
        "scenario": snapshot["scenario"],
        "stadium_state": {
            "heatmaps": snapshot["heatmaps"],
            "wait_times_minutes": snapshot["wait_times_minutes"],
            "emergency_override": snapshot["emergency_override"],
            "route_plan": snapshot["route_plan"],
            "evaluation": snapshot["evaluation"],
        },
        "response": response,
    }


@app.get("/api/v1/analytics")
async def analytics():
    snapshot = build_snapshot()
    persist_snapshot(snapshot)
    return {
        "status": "success",
        "snapshot": snapshot,
        "analytics": get_storage().get_dashboard_analytics(snapshot["scenario"]),
    }


@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            snapshot = build_snapshot()
            persist_snapshot(snapshot)
            await websocket.send_text(json.dumps(snapshot))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
