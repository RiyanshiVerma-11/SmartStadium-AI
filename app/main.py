import asyncio
import json
import time

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.ai_engine import DecisionEngine

app = FastAPI(title="SmartStadium AI Premium")
templates = Jinja2Templates(directory="app/templates")
decision_engine = DecisionEngine()

SCENARIO_LIBRARY = {
    "normal": [
        {
            "heatmaps": {
                "north_gate": "medium",
                "south_gate": "low",
                "concourse_a": "medium",
                "food_court": "medium",
            },
            "wait_times_minutes": {
                "food_stalls": 7,
                "restrooms": 3,
                "merch_stand": 6,
            },
            "alerts": [
                {
                    "type": "info",
                    "msg": "Operations stable. Crowd balancing remains within target range.",
                    "reasoning": "Normal operations profile is active, with smooth entry flow and moderate concourse density.",
                }
            ],
        },
        {
            "heatmaps": {
                "north_gate": "high",
                "south_gate": "low",
                "concourse_a": "medium",
                "food_court": "medium",
            },
            "wait_times_minutes": {
                "food_stalls": 9,
                "restrooms": 4,
                "merch_stand": 7,
            },
            "alerts": [
                {
                    "type": "info",
                    "msg": "Arrival wave detected at the North Gate. South Gate remains the preferred route.",
                    "reasoning": "Traffic is trending upward at the north entry, but alternate routes still have spare capacity.",
                }
            ],
        },
        {
            "heatmaps": {
                "north_gate": "medium",
                "south_gate": "low",
                "concourse_a": "medium",
                "food_court": "high",
            },
            "wait_times_minutes": {
                "food_stalls": 11,
                "restrooms": 4,
                "merch_stand": 8,
            },
            "alerts": [
                {
                    "type": "info",
                    "msg": "Concessions traffic is building before kickoff. Routing fans toward faster service lanes.",
                    "reasoning": "Food court density increased while restroom and gate systems remain healthy.",
                }
            ],
        },
    ],
    "peak_rush": [
        {
            "heatmaps": {
                "north_gate": "critical",
                "south_gate": "medium",
                "concourse_a": "high",
                "food_court": "high",
            },
            "wait_times_minutes": {
                "food_stalls": 22,
                "restrooms": 8,
                "merch_stand": 14,
            },
            "alerts": [
                {
                    "type": "critical",
                    "msg": "Peak rush active. North Gate saturation is above safe operating comfort levels.",
                    "reasoning": "Scenario controller raised entry density, increasing North Gate risk and pushing fans to alternate paths.",
                }
            ],
        },
        {
            "heatmaps": {
                "north_gate": "critical",
                "south_gate": "medium",
                "concourse_a": "high",
                "food_court": "critical",
            },
            "wait_times_minutes": {
                "food_stalls": 27,
                "restrooms": 10,
                "merch_stand": 16,
            },
            "alerts": [
                {
                    "type": "critical",
                    "msg": "Concession surge detected. Staff should redirect guests away from the central food court.",
                    "reasoning": "Peak-rush demand is propagating from entry lines into concessions and nearby concourse corridors.",
                }
            ],
        },
        {
            "heatmaps": {
                "north_gate": "high",
                "south_gate": "medium",
                "concourse_a": "critical",
                "food_court": "high",
            },
            "wait_times_minutes": {
                "food_stalls": 25,
                "restrooms": 9,
                "merch_stand": 15,
            },
            "alerts": [
                {
                    "type": "critical",
                    "msg": "Interior concourse congestion rising. Maintain redirection toward the South Gate corridor.",
                    "reasoning": "The scenario has shifted the primary bottleneck inward after heavy gate arrivals.",
                }
            ],
        },
    ],
}

GLOBAL_STATE = {
    "panic_mode": False,
    "scenario": "normal",
    "tick": 0,
}


class ScenarioRequest(BaseModel):
    scenario: str


class AskAIRequest(BaseModel):
    prompt: str


def get_current_snapshot(advance: bool = True) -> dict:
    scenario = GLOBAL_STATE["scenario"]
    frames = SCENARIO_LIBRARY.get(scenario, SCENARIO_LIBRARY["normal"])
    frame = frames[GLOBAL_STATE["tick"] % len(frames)]
    if advance:
        GLOBAL_STATE["tick"] += 1

    snapshot = {
        "timestamp": int(time.time()),
        "scenario": scenario,
        "emergency_override": GLOBAL_STATE["panic_mode"],
        "heatmaps": dict(frame["heatmaps"]),
        "wait_times_minutes": dict(frame["wait_times_minutes"]),
        "gamification": {"bounties": []},
        "alerts": list(frame["alerts"]),
    }

    if snapshot["heatmaps"]["south_gate"] in {"low", "medium"}:
        snapshot["gamification"]["bounties"].append(
            {"target": "south_gate", "points": "+50", "reason": "Use the South Gate to help balance entry traffic."}
        )

    if snapshot["wait_times_minutes"]["food_stalls"] <= 10:
        snapshot["gamification"]["bounties"].append(
            {"target": "food_stalls", "points": "+15", "reason": "Off-peak dining bonus active right now."}
        )

    if snapshot["heatmaps"]["north_gate"] == "critical":
        snapshot["alerts"].append(
            {
                "type": "critical",
                "msg": "North Gate is in critical flow. Redirect guests toward Gate C immediately.",
                "reasoning": "Density thresholds crossed the critical band, so alternate gate routing is now the safer option.",
            }
        )

    if GLOBAL_STATE["panic_mode"]:
        snapshot["alerts"].append(
            {
                "type": "critical",
                "msg": "Evacuation ordered. Follow the nearest marked exit and staff instructions.",
                "reasoning": "Manual evacuation override was triggered from the Command Center.",
            }
        )

    return snapshot


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/fan", response_class=HTMLResponse)
async def fan_app(request: Request):
    return templates.TemplateResponse(request=request, name="fan.html")


@app.get("/staff", response_class=HTMLResponse)
async def staff_app(request: Request):
    return templates.TemplateResponse(request=request, name="staff.html")


@app.post("/api/v1/panic")
async def toggle_panic():
    GLOBAL_STATE["panic_mode"] = not GLOBAL_STATE["panic_mode"]
    return {"status": "success", "panic_mode": GLOBAL_STATE["panic_mode"]}


@app.post("/api/v1/scenario")
async def set_scenario(payload: ScenarioRequest):
    if payload.scenario not in SCENARIO_LIBRARY:
        return {"status": "error", "message": "Unsupported scenario."}

    GLOBAL_STATE["scenario"] = payload.scenario
    GLOBAL_STATE["tick"] = 0
    return {"status": "success", "scenario": GLOBAL_STATE["scenario"]}


@app.post("/api/v1/ask_ai")
async def ask_ai(payload: AskAIRequest):
    snapshot = get_current_snapshot(advance=False)
    response = decision_engine.evaluate(snapshot, payload.prompt)
    return {
        "status": "success",
        "scenario": snapshot["scenario"],
        "stadium_state": {
            "heatmaps": snapshot["heatmaps"],
            "wait_times_minutes": snapshot["wait_times_minutes"],
            "emergency_override": snapshot["emergency_override"],
        },
        "response": response,
    }


@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            data = get_current_snapshot()
            await websocket.send_text(json.dumps(data))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
