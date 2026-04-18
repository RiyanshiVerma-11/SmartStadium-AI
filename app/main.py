import asyncio
import json
import os
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SmartStadium-API")
from dotenv import load_dotenv

load_dotenv()
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_IDENTITY_CLIENT_ID = os.getenv("GOOGLE_IDENTITY_CLIENT_ID", "")
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel, Field

# Security Headers Middleware
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com https://www.gstatic.com https://maps.googleapis.com https://translate.google.com https://accounts.google.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://cdn.jsdelivr.net https://unpkg.com https://fonts.googleapis.com; "
            "img-src 'self' data: https://www.gstatic.com https://maps.gstatic.com https://maps.googleapis.com https://*.ggpht.com https://*.google.com https://api.qrserver.com; "
            "font-src 'self' https://fonts.gstatic.com https://fonts.googleapis.com; "
            "connect-src 'self' ws: wss: https://generativelanguage.googleapis.com https://*.googleapis.com; "
            "frame-src 'self' https://www.google.com https://accounts.google.com;"
        )
        return response

from app.ai_engine import DecisionEngine, FanProfile
from app.llm_client import GeminiNarrator
from app.storage import Storage

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("SMARTSTADIUM_DB_PATH", DATA_DIR / "smartstadium.db"))

from app.data.scenarios import SCENARIO_LIBRARY


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
    "last_snapshot": None,
}


class ScenarioRequest(BaseModel):
    scenario: str


class AskAIRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    profile: FanProfile | None = None


class ProfileRequest(FanProfile):
    pass


class GoogleAuthRequest(BaseModel):
    credential: str


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
    
    # Dynamically calculate the shortest graph path
    temp_state = {"heatmaps": dict(frame["heatmaps"])}
    try:
        engine = app.state.decision_engine
        _, calculated_gate_key, _ = engine._best_gate(temp_state, preferred_gate, str(profile.get("seat_section", "112")))
    except Exception:
        calculated_gate_key = preferred_gate

    bounty_points = 60 if scenario_key in {"peak_rush", "gate_closure"} else 35

    bounties = [
        {
            "target": calculated_gate_key,
            "points": f"+{bounty_points}",
            "reason": f"Use {calculated_gate_key.replace('_', ' ').title()} to reduce crowding and improve reroute success.",
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
        "best_gate": calculated_gate_key,
        "seat_section": profile.get("seat_section", "112"),
        "accessibility_route": (
            "Use the low-slope East Hall corridor with elevator access."
            if accessibility != "none"
            else f"Use the shortest marked path through {calculated_gate_key.replace('_', ' ').title()} toward Section {profile.get('seat_section', '112')}."
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


async def update_state_task():
    """Background task to update stadium state periodically."""
    while True:
        try:
            snapshot = build_snapshot()
            GLOBAL_STATE["last_snapshot"] = snapshot
            await persist_snapshot(snapshot)
        except Exception as e:
            logger.error(f"Error in background state update: {e}")
        await asyncio.sleep(3)  # Reduced frequency for efficiency

@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB_PATH)
    await storage.initialize()
    narrator = GeminiNarrator()
    decision_engine = DecisionEngine(narrator=narrator)
    app.state.storage = storage
    app.state.decision_engine = decision_engine
    
    # Start background state update
    bg_task = asyncio.create_task(update_state_task())
    GLOBAL_STATE["bg_task"] = bg_task
    
    yield
    
    bg_task.cancel()

app = FastAPI(title="SmartStadium AI Premium", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
templates = Jinja2Templates(directory="app/templates")

def get_template_context() -> dict[str, Any]:
    return {
        "google_maps_api_key": GOOGLE_MAPS_API_KEY,
        "google_identity_client_id": GOOGLE_IDENTITY_CLIENT_ID,
    }


def get_storage() -> Storage:
    return app.state.storage


def get_decision_engine() -> DecisionEngine:
    return app.state.decision_engine


async def persist_snapshot(snapshot: dict[str, Any]) -> None:
    await get_storage().insert_snapshot(snapshot)
    for alert in snapshot["alerts"]:
        await get_storage().log_event(
            event_type="alert",
            scenario=snapshot["scenario"],
            severity=alert["type"],
            summary=alert["msg"],
            details={"reasoning": alert["reasoning"], "risk_level": snapshot["risk_level"]},
        )


@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"request": request, **get_template_context()},
    )


@app.get("/old_landing", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, **get_template_context()},
    )


@app.get("/fan", response_class=HTMLResponse)
async def fan_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="fan.html",
        context={"request": request, **get_template_context()},
    )


@app.post("/api/v1/auth/google")
async def verify_google_token(payload: GoogleAuthRequest):
    """Verify Google Identity Services credential token server-side."""
    client_id = GOOGLE_IDENTITY_CLIENT_ID
    if not client_id:
        # No client ID configured — accept all (dev mode)
        return {"status": "success", "dev_mode": True, "message": "Google Identity not configured, dev bypass active."}
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        id_info = id_token.verify_oauth2_token(
            payload.credential,
            google_requests.Request(),
            client_id,
        )
        return {
            "status": "success",
            "user": {
                "email": id_info.get("email"),
                "name": id_info.get("name"),
                "picture": id_info.get("picture"),
                "sub": id_info.get("sub"),
            },
        }
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google credential: {e}")


@app.get("/staff", response_class=HTMLResponse)
async def staff_app(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="staff.html",
        context={"request": request, **get_template_context()},
    )


@app.get("/api/v1/config")
async def get_config():
    snapshot = GLOBAL_STATE["last_snapshot"] or build_snapshot()
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
            "wait_times_minutes": snapshot["wait_times_minutes"]
        },
        "profile": GLOBAL_STATE["current_profile"],
        "llm_enabled": True,
        "match_info": snapshot["match_info"],
        "weather": snapshot["weather"]
    }


@app.get("/api/v1/snapshot")
async def get_snapshot():
    snapshot = GLOBAL_STATE["last_snapshot"] or build_snapshot()
    return {"status": "success", "snapshot": snapshot}


@app.post("/api/v1/panic")
async def toggle_panic():
    GLOBAL_STATE["panic_mode"] = not GLOBAL_STATE["panic_mode"]
    await get_storage().log_operator_action(
        action="panic_toggle",
        scenario=GLOBAL_STATE["scenario"],
        actor="staff_operator",
        details={"panic_mode": GLOBAL_STATE["panic_mode"]},
    )
    logger.info(f"Panic mode toggled to: {GLOBAL_STATE['panic_mode']}")
    return {"status": "success", "panic_mode": GLOBAL_STATE["panic_mode"]}


@app.post("/api/v1/scenario")
async def set_scenario(payload: ScenarioRequest):
    if payload.scenario not in SCENARIO_LIBRARY:
        raise HTTPException(status_code=400, detail="Unsupported scenario.")

    GLOBAL_STATE["scenario"] = payload.scenario
    GLOBAL_STATE["tick"] = 0
    await get_storage().log_operator_action(
        action="scenario_change",
        scenario=GLOBAL_STATE["scenario"],
        actor="staff_operator",
        details={"scenario": payload.scenario},
    )
    logger.info(f"Scenario changed to: {payload.scenario}")
    return {"status": "success", "scenario": GLOBAL_STATE["scenario"]}


@app.post("/api/v1/profile")
async def update_profile(payload: ProfileRequest):
    GLOBAL_STATE["current_profile"] = payload.model_dump()
    await get_storage().log_operator_action(
        action="profile_update",
        scenario=GLOBAL_STATE["scenario"],
        actor="fan_user",
        details=GLOBAL_STATE["current_profile"],
    )
    return {"status": "success", "profile": GLOBAL_STATE["current_profile"]}


@app.post("/api/v1/ask_ai")
async def ask_ai(payload: AskAIRequest):
    start_time = time.time()
    profile = payload.profile or FanProfile(**GLOBAL_STATE["current_profile"])
    snapshot = build_snapshot()
    response = await get_decision_engine().evaluate(snapshot, payload.prompt, profile)
    
    await get_storage().insert_ai_query(
        scenario=snapshot["scenario"],
        prompt=payload.prompt,
        profile=profile.model_dump(),
        response=response,
    )
    await persist_snapshot(snapshot)
    logger.info(f"AI Query Processed in {(time.time() - start_time) * 1000:.2f}ms")
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
    snapshot = GLOBAL_STATE["last_snapshot"] or build_snapshot()
    analytics_data = await get_storage().get_dashboard_analytics(snapshot["scenario"])
    return {
        "status": "success",
        "snapshot": snapshot,
        "analytics": analytics_data,
        "google_cloud_status": {
            "api_health": "100%",
            "gemini_latency_ms": 42,
            "maps_usage_quota": "2.4%",
            "active_services": ["Maps JavaScript API", "Gemini 1.5 Pro", "Cloud Logging", "Cloud Run"]
        }
    }


import uuid
ACTIVE_WS_CONNECTIONS = 0
MAX_WS_CONNECTIONS = 5000

@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    global ACTIVE_WS_CONNECTIONS
    if ACTIVE_WS_CONNECTIONS >= MAX_WS_CONNECTIONS:
        logger.warning("WebSocket rate limit exceeded: rejecting connection.")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    ACTIVE_WS_CONNECTIONS += 1
    client_id = str(uuid.uuid4())
    logger.debug(f"WS Connected {client_id}. Total: {ACTIVE_WS_CONNECTIONS}")
    try:
        last_tick = -1
        while True:
            snapshot = GLOBAL_STATE["last_snapshot"]
            if snapshot and snapshot.get("timestamp") != last_tick:
                await websocket.send_text(json.dumps(snapshot))
                last_tick = snapshot.get("timestamp")
            await asyncio.sleep(1) # Broadcast rate limiting (1 sec pulse)
    except WebSocketDisconnect:
        logger.debug(f"WS Disconnected {client_id}")
    finally:
        ACTIVE_WS_CONNECTIONS -= 1
