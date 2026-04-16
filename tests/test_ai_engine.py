import asyncio

from app.ai_engine import DecisionEngine, FanProfile


def test_emergency_intent_is_critical():
    engine = DecisionEngine()
    state = {
        "scenario": "medical_emergency",
        "heatmaps": {"north_gate": "medium", "south_gate": "low", "concourse_a": "high"},
        "wait_times_minutes": {"restrooms": 4, "food_stalls": 9},
        "directives": {
            "escalation_priority": "high",
            "staffing_recommendation": {"summary": "Medical corridor protection required."},
        },
        "evaluation": {"improvements": {"avg_wait_improvement_pct": 32}},
        "route_plan": {
            "food_pickup": "Suite lane pickup",
            "accessibility_route": "Use the low-slope East Hall corridor with elevator access.",
        },
        "emergency_override": False,
    }

    result = asyncio.run(engine.evaluate(state, "Help there is an injured fan", FanProfile()))
    assert result["intent"] == "emergency"
    assert result["severity"] == "critical"
    assert result["action"]["type"] == "alert_staff"


def test_shortest_line_prefers_profile_gate_when_safe():
    engine = DecisionEngine()
    state = {
        "scenario": "normal",
        "heatmaps": {"north_gate": "low", "south_gate": "low", "concourse_a": "medium"},
        "wait_times_minutes": {"restrooms": 3, "food_stalls": 7},
        "directives": {
            "escalation_priority": "medium",
            "staffing_recommendation": {"summary": "Balanced staffing is sufficient."},
        },
        "evaluation": {"improvements": {"avg_wait_improvement_pct": 28}},
        "route_plan": {
            "food_pickup": "Suite lane pickup",
            "accessibility_route": "Use the shortest marked path through Gate C toward Section 112.",
        },
        "emergency_override": False,
    }

    result = asyncio.run(engine.evaluate(state, "What is the fastest line?", FanProfile(preferred_gate="gate_c")))
    assert result["intent"] == "shortest_line"
    assert "Gate" in result["message"]
    assert result["provider"] == "rules"


def test_accessibility_intent_returns_route():
    engine = DecisionEngine()
    state = {
        "scenario": "weather_delay",
        "heatmaps": {"north_gate": "medium", "south_gate": "medium", "concourse_a": "critical"},
        "wait_times_minutes": {"restrooms": 11, "food_stalls": 19},
        "directives": {
            "escalation_priority": "medium",
            "staffing_recommendation": {"summary": "Shelter coordination required."},
        },
        "evaluation": {"improvements": {"avg_wait_improvement_pct": 40}},
        "route_plan": {
            "food_pickup": "Express cart near Concourse A",
            "accessibility_route": "Use the low-slope East Hall corridor with elevator access.",
        },
        "emergency_override": False,
    }

    result = asyncio.run(
        engine.evaluate(
            state,
            "I need wheelchair friendly routing.",
            FanProfile(accessibility_need="wheelchair", ticket_type="general"),
        )
    )
    assert result["intent"] == "accessibility"
    assert "elevator" in result["message"].lower()
