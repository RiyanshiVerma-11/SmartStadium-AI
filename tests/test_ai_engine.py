import pytest
from app.ai_engine import DecisionEngine, FanProfile

@pytest.fixture
def base_state():
    return {
        "scenario_label": "Normal Operation",
        "scenario": "normal",
        "heatmaps": {
            "north_gate": "medium",
            "concourse_a": "low",
            "south_gate": "critical",
            "concourse_b": "medium",
            "parking_east": "low"
        },
        "wait_times_minutes": {
            "restrooms": 3,
            "food_stalls": 5
        },
        "route_plan": {
            "food_pickup": "Express Lane 2",
            "accessibility_route": "Use Elevator B"
        },
        "evaluation": {
            "improvements": {"avg_wait_improvement_pct": 12}
        },
        "directives": {
            "escalation_priority": "low",
            "staffing_recommendation": {"summary": "All good"}
        }
    }

@pytest.mark.asyncio
async def test_detect_intent():
    engine = DecisionEngine()
    assert engine._detect_intent("Where is the nearest restroom?") == "restroom"
    assert engine._detect_intent("I am hungry, I want food") == "food"
    assert engine._detect_intent("Help there is a fire!") == "emergency"
    assert engine._detect_intent("Which gate has the shortest line") == "shortest_line"
    assert engine._detect_intent("Where is the wheelchair ramp") == "accessibility"
    assert engine._detect_intent("Just saying hello") == "general"

@pytest.mark.asyncio
async def test_handle_emergency(base_state):
    engine = DecisionEngine()
    profile = FanProfile(name="John")
    result = await engine.evaluate(base_state, "Help injured fan", profile)
    
    assert result["intent"] == "emergency"
    assert result["severity"] == "critical"
    assert "John" in result["message"]
    assert result["action"]["type"] == "alert_staff"

@pytest.mark.asyncio
async def test_best_gate(base_state):
    engine = DecisionEngine()
    # Gate A is closer than Gate B from section 112 in the graph
    profile = FanProfile(preferred_gate="gate_b")
    gate_name, zone, status, gate_id = engine._best_gate(base_state, profile.preferred_gate)
    assert gate_name == "Gate A"
    assert zone == "north_gate"
    assert status == "medium"

@pytest.mark.asyncio
async def test_handle_food(base_state):
    engine = DecisionEngine()
    profile = FanProfile(food_preference="vegan")
    result = await engine.evaluate(base_state, "Where can I get a hot dog", profile)
    
    assert result["intent"] == "food"
    assert "vegan" in result["message"]
    assert result["action"]["target"] == "food_stalls"

@pytest.mark.asyncio
async def test_accessibility(base_state):
    engine = DecisionEngine()
    profile = FanProfile()
    result = await engine.evaluate(base_state, "Need wheelchair access", profile)
    
    assert result["intent"] == "accessibility"
    assert result["message"] == "Use Elevator B"
    assert result["action"]["target"] == "accessible_path"
