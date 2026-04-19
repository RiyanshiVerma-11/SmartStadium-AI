from fastapi.testclient import TestClient

from app.main import app


def test_toggle_panic():
    with TestClient(app) as client:
        first = client.post("/api/v1/panic")
        second = client.post("/api/v1/panic")

    assert first.status_code == 200
    assert second.status_code == 200
    assert isinstance(first.json()["panic_mode"], bool)
    assert isinstance(second.json()["panic_mode"], bool)


def test_set_supported_scenario():
    with TestClient(app) as client:
        response = client.post("/api/v1/scenario", json={"scenario": "peak_rush"})

    assert response.status_code == 200
    assert response.json()["scenario"] == "peak_rush"


def test_reject_unsupported_scenario():
    with TestClient(app) as client:
        response = client.post("/api/v1/scenario", json={"scenario": "unknown"})

    assert response.status_code == 400


def test_ask_ai_returns_hybrid_payload():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/ask_ai",
            json={
                "prompt": "Where is the shortest line right now?",
                "profile": {
                    "name": "Riya",
                    "seat_section": "112",
                    "preferred_gate": "gate_c",
                    "food_preference": "vegetarian",
                    "accessibility_need": "none",
                    "ticket_type": "vip",
                },
            },
        )

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "success"
    assert "response" in body
    assert body["response"]["intent"] in {"shortest_line", "gate"}
    assert "provider" in body["response"]
    assert "evaluation" in body["stadium_state"]


def test_profile_update_persists_shape():
    payload = {
        "name": "Riya",
        "seat_section": "215",
        "preferred_gate": "gate_a",
        "food_preference": "vegan",
        "accessibility_need": "wheelchair",
        "ticket_type": "general",
        "preferred_language": "en",
    }
    with TestClient(app) as client:
        response = client.post("/api/v1/profile", json=payload)

    assert response.status_code == 200
    assert response.json()["profile"] == payload
