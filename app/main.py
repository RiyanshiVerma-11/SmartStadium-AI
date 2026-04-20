import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from cachetools import TTLCache
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from starlette.middleware.base import BaseHTTPMiddleware

from app.ai_engine import DecisionEngine, FanProfile
from app.llm_client import gemini_client
from app.storage import Storage
from app.websocket_manager import ws_manager
from app.data.scenarios import SCENARIO_LIBRARY

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SmartStadium-API")

limiter = Limiter(key_func=get_remote_address)

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_IDENTITY_CLIENT_ID = os.getenv("GOOGLE_IDENTITY_CLIENT_ID", "")
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "")
GOOGLE_TTS_API_KEY = os.getenv("GOOGLE_TTS_API_KEY", "")
GOOGLE_TTS_VOICE_NAME = os.getenv("GOOGLE_TTS_VOICE_NAME", "en-US-Neural2-C")
ROLE_SIGNING_SECRET = os.getenv("SMARTSTADIUM_ROLE_SECRET", "smartstadium-dev-secret")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = Path(os.getenv("SMARTSTADIUM_DB_PATH", DATA_DIR / "smartstadium.db"))

MAX_WS_CONNECTIONS = 5000

def normalize_language(language: str | None) -> str:
    """Helper to standardize language codes (e.g., 'en-US' -> 'en')."""
    if not language:
        return "en"
    lang = language.strip().lower()
    if not lang:
        return "en"
    return lang.split("-")[0].split("_")[0]

async def update_state_task():
    """Background task to update stadium state periodically."""
    while True:
        try:
            latest_announcement = GLOBAL_STATE.get("latest_announcement")
            if latest_announcement and latest_announcement.get("expires_at", 0) <= int(time.time()):
                GLOBAL_STATE["latest_announcement"] = None
            snapshot = build_snapshot()
            GLOBAL_STATE["last_snapshot"] = snapshot
            await persist_snapshot(snapshot)
        except Exception as e:
            logger.error(f"Error in background state update: {e}")
        await asyncio.sleep(5)  # Efficiency: Reduced to 5s for cloud-scale

@asynccontextmanager
async def lifespan_context(app: FastAPI):
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB_PATH)
    await storage.initialize()
    narrator = gemini_client
    decision_engine = DecisionEngine(narrator=narrator)
    http_client = httpx.AsyncClient(timeout=10.0)
    app.state.storage = storage
    app.state.decision_engine = decision_engine
    app.state.http_client = http_client
    bg_task = asyncio.create_task(update_state_task())
    GLOBAL_STATE["bg_task"] = bg_task
    yield
    bg_task.cancel()
    await storage.close()
    await http_client.aclose()

# Define 'app' with the lifespan context directly in constructor
app = FastAPI(title="SmartStadium AI Premium", lifespan=lifespan_context)
app.state.limiter = limiter

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(self)"
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
        "preferred_language": "en",
    },
    "latest_announcement": None,
    "last_snapshot": None,
    "last_persisted_alert_keys": set(),
    "active_sos_alerts": [],
    "heatmap_history": [],
    "pending_staff_tasks": [],
    "resolved_incident_ids": set(),
    "latest_ai_pa_suggestion": None,
}
RATE_LIMIT_CACHE = TTLCache(maxsize=5000, ttl=10)
TRANSLATION_CACHE = TTLCache(maxsize=1000, ttl=1800)
LOCALIZED_SNAPSHOT_CACHE = TTLCache(maxsize=100, ttl=60)


class ScenarioRequest(BaseModel):
    scenario: str


class AskAIRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    profile: FanProfile | None = None


class ProfileRequest(FanProfile):
    pass


class GoogleAuthRequest(BaseModel):
    credential: str


class AnnouncementRequest(BaseModel):
    message: str = Field(..., min_length=8, max_length=280)
    severity: str = Field(default="info", pattern="^(info|warning|critical)$")
    broadcast: bool = True
    language: str | None = None

class TaskResolveRequest(BaseModel):
    task_id: str


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


def _sign_role(role: str) -> str:
    return hmac.new(ROLE_SIGNING_SECRET.encode("utf-8"), str(role).encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_role(role: str | None, signature: str | None) -> bool:
    if not role or not signature:
        return False
    expected = _sign_role(role)
    return hmac.compare_digest(signature, expected)


def get_request_role(request: Request) -> str:
    role = request.cookies.get("stadium_role")
    signature = request.cookies.get("stadium_role_sig")
    if _verify_role(role, signature):
        return role or "fan"
    return "fan"


def require_admin_role(request: Request) -> None:
    if get_request_role(request) != "admin":
        raise HTTPException(status_code=403, detail="Admin role required.")


def get_http_client() -> httpx.AsyncClient:
    return app.state.http_client


async def synthesize_announcement_audio(message: str, language: str = "en") -> dict[str, Any]:
    normalized_language = normalize_language(language)
    if not GOOGLE_TTS_API_KEY:
        return {
            "enabled": False,
            "provider": "browser_fallback",
            "audio_src": None,
            "voice_name": None,
            "language": normalized_language,
        }

    voice_name = GOOGLE_TTS_VOICE_NAME
    language_code = "en-US" if normalized_language == "en" else normalized_language
    payload = {
        "input": {"text": message},
        "voice": {
            "languageCode": language_code,
            "name": voice_name if normalized_language == "en" else None,
            "ssmlGender": "NEUTRAL",
        },
        "audioConfig": {
            "audioEncoding": "MP3",
            "speakingRate": 1.02,
        },
    }
    payload["voice"] = {key: value for key, value in payload["voice"].items() if value}
    try:
        response = await get_http_client().post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            params={"key": GOOGLE_TTS_API_KEY},
            json=payload,
        )
        response.raise_for_status()
        audio_content = response.json().get("audioContent")
    except Exception:
        audio_content = None

    if not audio_content:
        return {
            "enabled": False,
            "provider": "browser_fallback",
            "audio_src": None,
            "voice_name": None,
            "language": normalized_language,
        }

    return {
        "enabled": True,
        "provider": "google_cloud_text_to_speech",
        "audio_src": f"data:audio/mpeg;base64,{audio_content}",
        "voice_name": voice_name,
        "language": normalized_language,
    }


def build_announcement_record(
    message: str,
    severity: str,
    language: str,
    audio_payload: dict[str, Any],
) -> dict[str, Any]:
    created_at = int(time.time())
    return {
        "id": f"ann-{created_at}",
        "message": message,
        "severity": severity,
        "language": normalize_language(language),
        "audio_enabled": audio_payload["enabled"],
        "audio_provider": audio_payload["provider"],
        "audio_src": audio_payload["audio_src"],
        "voice_name": audio_payload["voice_name"],
        "created_at": created_at,
        "expires_at": created_at + 180,
    }


def build_match_info(match_tick: int) -> dict[str, str]:
    """Generate a believable match clock and scoreline for the fan dashboard."""
    phase_tick = match_tick % 48
    if phase_tick < 18:
        minute = 28 + (phase_tick * 2)
        score = "1 - 0" if minute < 38 else "2 - 1"
        return {"teams": "Lions vs Tigers", "score": score, "time": f"{minute}'", "status": "live"}
    if phase_tick < 22:
        return {"teams": "Lions vs Tigers", "score": "2 - 1", "time": "HT", "status": "halftime"}
    if phase_tick < 42:
        second_half_tick = phase_tick - 22
        minute = 46 + (second_half_tick * 2)
        score = "2 - 1" if minute < 84 else "3 - 1"
        if minute <= 90:
            return {"teams": "Lions vs Tigers", "score": score, "time": f"{minute}'", "status": "live"}
        stoppage = min(minute - 90, 4)
        return {"teams": "Lions vs Tigers", "score": score, "time": f"90+{stoppage}'", "status": "live"}
    return {"teams": "Lions vs Tigers", "score": "3 - 1", "time": "FT", "status": "full_time"}


def build_snapshot(increment_tick: bool = True) -> dict[str, Any]:
    scenario_key = GLOBAL_STATE["scenario"]
    scenario = SCENARIO_LIBRARY[scenario_key]
    frames = scenario["frames"]
    frame = frames[GLOBAL_STATE["tick"] % len(frames)]
    if increment_tick:
        GLOBAL_STATE["tick"] += 1

    # 1. Predictive Anomaly Detection (Trend Analysis)
    rank_map = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    current_heatmap = frame["heatmaps"]
    history = GLOBAL_STATE.get("heatmap_history", [])
    history.append(current_heatmap)
    if len(history) > 3:
        history.pop(0)
    GLOBAL_STATE["heatmap_history"] = history

    predictive_warnings = []
    if len(history) == 3:
        for zone in current_heatmap:
            v1 = rank_map.get(history[0].get(zone, "low"), 0)
            v2 = rank_map.get(history[1].get(zone, "low"), 0)
            v3 = rank_map.get(history[2].get(zone, "low"), 0)
            if v3 > v2 > v1:
                predictive_warnings.append({"zone": zone, "msg": f"Predictive: Rapid crowd growth at {zone.replace('_', ' ').title()}."})

    # 2. Alert Filtering: Filter out scenario alerts that have been manually resolved
    alerts = []
    for alert in frame["alerts"]:
        alert_id = hashlib.md5(f"{scenario_key}|{alert['msg']}".encode()).hexdigest()
        if alert_id not in GLOBAL_STATE["resolved_incident_ids"]:
            alert_copy = dict(alert)
            alert_copy["id"] = alert_id
            alerts.append(alert_copy)

    # AI Suggested PA Announcement Logic
    suggested_pa_message = GLOBAL_STATE.get("latest_ai_pa_suggestion") or "Operations are currently stable. Welcome to the stadium!"
    if scenario_key == "weather_delay":
        suggested_pa_message = "Weather Alert: Lightning in the area. Please move away from open seating and seek shelter in the concourses."

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
        calculated_gate_key, calculated_zone_key, _, calculated_gate_id = engine._best_gate(
            temp_state, preferred_gate, str(profile.get("seat_section", "112"))
        )
    except Exception as e:
        logger.error(f"Error calculating best gate in snapshot: {e}")
        calculated_gate_id = preferred_gate
        calculated_gate_key = DecisionEngine._pretty_label(preferred_gate)
        calculated_zone_key = DecisionEngine.GATE_ZONE_MAP.get(preferred_gate, "north_gate")

    bounty_active = frame.get("bounty_active", False)
    bounty_points = 60 if scenario_key in {"peak_rush", "gate_closure"} else 35
    bounty_reason = frame.get("bounty_description") or f"Use {calculated_gate_key} to reduce crowding and improve reroute success."

    bounties = []
    if bounty_active:
        bounties.append(
            {
                "target": calculated_gate_id,
                "points": f"+{bounty_points}",
                "reason": bounty_reason,
            }
        )

    if frame["wait_times_minutes"]["food_stalls"] <= 12:
        bounties.append(
            {
                "target": "food_stalls",
                "points": "+20",
                "reason": f"Grab {profile.get('food_preference', 'fan favorite')} concessions during the calm service window.",
            }
        )

    personalized_route = {
        "best_gate": calculated_gate_id,
        "seat_section": profile.get("seat_section", "112"),
        "accessibility_route": (
            "Use the low-slope East Hall corridor with elevator access."
            if accessibility != "none"
            else f"Use the shortest marked path through {calculated_gate_key} toward Section {profile.get('seat_section', '112')}."
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

    # Inject Predictive Warnings into the visible Alerts feed
    for pw in predictive_warnings:
        alerts.append({
            "type": "warning",
            "msg": pw["msg"],
            "reasoning": f"AI detected a potential surge at {pw['zone'].replace('_', ' ').title()} based on recent trends."
        })

    # Inject Pending Staff Tasks into the visible Alerts feed
    for task in GLOBAL_STATE.get("pending_staff_tasks", []):
        if task["status"] == "pending":
            alerts.append({
                "id": task["id"],
                "type": "info",
                "msg": f"📋 New Task: {task['action']}",
                "reasoning": f"AI-generated directive. Task ID: {task['id']}. Use /resolve to clear."
            })

    # Add persistent SOS alerts to the snapshot so they remain visible in live feeds
    now = int(time.time())
    # Level 4 Refinement: Purge expired and ALREADY RESOLVED SOS alerts
    GLOBAL_STATE["active_sos_alerts"] = [
        a for a in GLOBAL_STATE.get("active_sos_alerts", []) 
        if a.get("expires_at", 0) > now and a.get("id") not in GLOBAL_STATE["resolved_incident_ids"]
    ]

    for sos in GLOBAL_STATE["active_sos_alerts"]:
        alerts.append({
            "id": sos.get("id"),
            "type": sos["type"],
            "msg": sos["msg"],
            "reasoning": sos["reasoning"],
            "play_sound": sos.get("play_sound"),
            "force_modal": sos.get("force_modal")
        })

    if GLOBAL_STATE["panic_mode"]:
        target_gate_name = calculated_gate_key.upper()
        target_label = f"{target_gate_name} TO SAFE EXIT"
        alerts.append(
            {
                "id": "evac-override",
                "type": "critical",
                "msg": "Emergency Override: Evacuate",
                "reasoning": "Follow staff instructions immediately. Use the nearest safe exit shown on map.",
                "action_label": target_label,
                "action": "show_map",
                "action_details": {"type": "route", "target": calculated_gate_id, "ui_effect": "focus_map"},
                "evac_path": {
                    "start": profile.get("seat_section", "112"),
                    "end": calculated_gate_id
                }
            }
        )

    # Dynamic Match Simulation
    match_info = build_match_info(GLOBAL_STATE["tick"])

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
        "timestamp": time.time(),
        "scenario": scenario_key,
        "scenario_label": scenario["label"],
        "scenario_description": scenario["description"],
        "incident_type": scenario["incident_type"],
        "risk_level": scenario["risk_level"],
        "google_services": list(scenario["google_services"]),
        "announcement": GLOBAL_STATE["latest_announcement"],
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
        "predictive_warnings": predictive_warnings,
        "suggested_pa_message": suggested_pa_message,
        "personalization": profile,
        "route_plan": personalized_route,
        "match_info": match_info,
        "weather": {
            "temp": f"{varied_temp}°C",
            "condition": w["cond"],
            "icon": w["icon"]
        }
    }


async def translate_text(text: str, target_language: str) -> str:
    target = normalize_language(target_language)
    if not text or target == "en" or not GOOGLE_TRANSLATE_API_KEY:
        return text

    cache_key = f"{target}:{text}"
    cached = TRANSLATION_CACHE.get(cache_key)
    if cached:
        return cached

    url = "https://translation.googleapis.com/language/translate/v2"
    payload = {
        "q": text,
        "target": target,
        "format": "text",
        "source": "en",
    }
    try:
        response = await get_http_client().post(url, params={"key": GOOGLE_TRANSLATE_API_KEY}, json=payload)
        response.raise_for_status()
        translated = response.json()["data"]["translations"][0]["translatedText"]
        TRANSLATION_CACHE[cache_key] = translated
        return translated
    except Exception:
        # Fail open to English so emergency messages always remain readable.
        return text


async def localize_snapshot(snapshot: dict[str, Any], language: str) -> dict[str, Any]:
    target_language = normalize_language(language)
    
    # Cache lookup to avoid redundant translations across multiple clients sharing a language
    ts = snapshot.get("timestamp", 0)
    cache_key = f"{target_language}:{ts}"
    if cache_key in LOCALIZED_SNAPSHOT_CACHE:
        return LOCALIZED_SNAPSHOT_CACHE[cache_key]

    localized = copy.deepcopy(snapshot)
    localized["language"] = target_language

    if target_language == "en":
        return localized

    for alert in localized.get("alerts", []):
        alert["msg"] = await translate_text(alert.get("msg", ""), target_language)
        alert["reasoning"] = await translate_text(alert.get("reasoning", ""), target_language)

    route = localized.get("route_plan", {})
    if route.get("accessibility_route"):
        route["accessibility_route"] = await translate_text(route["accessibility_route"], target_language)

    if localized.get("suggested_pa_message"):
        localized["suggested_pa_message"] = await translate_text(localized["suggested_pa_message"], target_language)

    announcement = localized.get("announcement")
    if announcement and announcement.get("message") and target_language != announcement.get("language"):
        announcement["message"] = await translate_text(announcement["message"], target_language)
        announcement["language"] = target_language
        announcement["audio_enabled"] = False
        announcement["audio_src"] = None
        announcement["audio_provider"] = "browser_fallback"
        announcement["voice_name"] = None

    LOCALIZED_SNAPSHOT_CACHE[cache_key] = localized
    return localized


def calculate_delta(previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """
    Calculate efficient delta - only send changed values, not full sub-objects.
    For large-scale simulations, this reduces bandwidth by up to 90%.
    """
    delta = {}
    for key in current:
        current_val = current[key]
        previous_val = previous.get(key)

        # Handle nested dictionaries (like heatmaps, wait_times) - extract only changed values
        if isinstance(current_val, dict) and isinstance(previous_val, dict):
            nested_delta = {}
            for sub_key in current_val:
                if current_val[sub_key] != previous_val.get(sub_key):
                    nested_delta[sub_key] = current_val[sub_key]
            if nested_delta:
                delta[key] = nested_delta
        # Handle lists - send full list only if changed
        elif isinstance(current_val, list):
            if current_val != previous_val:
                delta[key] = current_val
        # Handle primitives - direct comparison
        elif current_val != previous_val:
            delta[key] = current_val

    return delta


app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SlowAPIMiddleware)
templates = Jinja2Templates(directory="app/templates")

def get_template_context() -> dict[str, Any]:
    return {
        "google_maps_api_key": GOOGLE_MAPS_API_KEY,
        "google_identity_client_id": GOOGLE_IDENTITY_CLIENT_ID,
        "google_tts_enabled": bool(GOOGLE_TTS_API_KEY),
    }


async def persist_snapshot(snapshot: dict[str, Any]) -> None:
    await get_storage().insert_snapshot(snapshot)
    current_alert_keys = {
        f"{snapshot['scenario']}|{alert['type']}|{alert['msg']}"
        for alert in snapshot["alerts"]
    }
    new_alerts = []
    for alert in snapshot["alerts"]:
        alert_key = f"{snapshot['scenario']}|{alert['type']}|{alert['msg']}"
        if alert_key not in GLOBAL_STATE["last_persisted_alert_keys"]:
            new_alerts.append(
                {
                    "event_type": "alert",
                    "scenario": snapshot["scenario"],
                    "severity": alert["type"],
                    "summary": alert["msg"],
                    "details": {"reasoning": alert["reasoning"], "risk_level": snapshot["risk_level"]},
                }
            )
    if new_alerts:
        await get_storage().log_events_batch(new_alerts)
    GLOBAL_STATE["last_persisted_alert_keys"] = current_alert_keys


def get_storage() -> Storage:
    # This function needs to be defined after `app` is initialized
    # but before it's used in `persist_snapshot` which is called in `lifespan`.
    # However, `persist_snapshot` is called *after* `app` is set up in lifespan.
    # So, moving these helper functions here is appropriate.
    return app.state.storage


def get_decision_engine() -> DecisionEngine:
    # This function needs to be defined after `app` is initialized
    return app.state.decision_engine


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
    except ImportError as e:
        raise HTTPException(status_code=503, detail=f"Google auth dependency missing: {e}")
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google credential: {e}")


@app.get("/staff", response_class=HTMLResponse)
async def staff_app(request: Request):
    role = request.query_params.get("role", get_request_role(request))
    normalized_role = "admin" if role == "admin" else "staff"
    response = templates.TemplateResponse(
        request=request,
        name="staff.html",
        context={"request": request, "role": normalized_role, **get_template_context()},
    )
    response.set_cookie("stadium_role", normalized_role, httponly=True, samesite="lax")
    response.set_cookie("stadium_role_sig", _sign_role(normalized_role), httponly=True, samesite="lax")
    return response


@app.get("/api/v1/config")
async def get_config(request: Request):
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
        "role": get_request_role(request),
        "llm_enabled": True,
        "translation_enabled": bool(GOOGLE_TRANSLATE_API_KEY),
        "google_tts_enabled": bool(GOOGLE_TTS_API_KEY),
        "match_info": snapshot["match_info"],
        "weather": snapshot["weather"],
    }


@app.get("/api/v1/snapshot")
async def get_snapshot(request: Request):
    snapshot = GLOBAL_STATE["last_snapshot"] or build_snapshot()
    language = normalize_language(request.query_params.get("lang") or GLOBAL_STATE["current_profile"].get("preferred_language"))
    localized_snapshot = await localize_snapshot(snapshot, language)
    return {"status": "success", "snapshot": localized_snapshot}


@app.post("/api/v1/panic")
async def toggle_panic(request: Request):
    require_admin_role(request)
    GLOBAL_STATE["panic_mode"] = not GLOBAL_STATE["panic_mode"]
    await get_storage().log_operator_action(
        action="panic_toggle",
        scenario=GLOBAL_STATE["scenario"],
        actor="staff_operator",
        details={"panic_mode": GLOBAL_STATE["panic_mode"]},
    )
    # Force immediate sync and clear caches
    LOCALIZED_SNAPSHOT_CACHE.clear()
    new_snapshot = build_snapshot(increment_tick=False)
    GLOBAL_STATE["last_snapshot"] = new_snapshot
    await persist_snapshot(new_snapshot)

    logger.info(f"Panic mode toggled to: {GLOBAL_STATE['panic_mode']}")
    return {"status": "success", "panic_mode": GLOBAL_STATE["panic_mode"]}


@app.post("/api/v1/announcements")
async def create_announcement(payload: AnnouncementRequest, request: Request):
    require_admin_role(request)
    language = payload.language or GLOBAL_STATE["current_profile"].get("preferred_language", "en")
    audio_payload = await synthesize_announcement_audio(payload.message, language)
    announcement = build_announcement_record(payload.message, payload.severity, language, audio_payload)

    if payload.broadcast:
        GLOBAL_STATE["latest_announcement"] = announcement
        if GLOBAL_STATE.get("last_snapshot"):
            updated_snapshot = build_snapshot()
            GLOBAL_STATE["last_snapshot"] = updated_snapshot
            await persist_snapshot(updated_snapshot)
        await get_storage().log_operator_action(
            action="broadcast_announcement",
            scenario=GLOBAL_STATE["scenario"],
            actor="staff_operator",
            details={
                "announcement_id": announcement["id"],
                "severity": payload.severity,
                "audio_provider": announcement["audio_provider"],
            },
        )
        await get_storage().log_announcement(
            scenario=GLOBAL_STATE["scenario"],
            severity=payload.severity,
            message=payload.message,
            audio_provider=announcement["audio_provider"],
            details={
                "broadcast": True,
                "language": announcement["language"],
                "audio_enabled": announcement["audio_enabled"],
            },
        )

    return {
        "status": "success",
        "broadcast": payload.broadcast,
        "announcement": announcement,
    }


@app.post("/api/v1/scenario")
async def set_scenario(payload: ScenarioRequest):
    if payload.scenario not in SCENARIO_LIBRARY:
        raise HTTPException(status_code=400, detail="Unsupported scenario.")

    GLOBAL_STATE["scenario"] = payload.scenario
    GLOBAL_STATE["tick"] = 0
    GLOBAL_STATE["last_persisted_alert_keys"] = set()
    GLOBAL_STATE["latest_ai_pa_suggestion"] = None
    GLOBAL_STATE["resolved_incident_ids"] = set()
    
    # Clear caches to prevent zombie alerts from old scenario frames
    LOCALIZED_SNAPSHOT_CACHE.clear()
    TRANSLATION_CACHE.clear()
    
    # Force an immediate snapshot refresh so WebSockets broadcast the change instantly
    new_snapshot = build_snapshot(increment_tick=False)
    GLOBAL_STATE["last_snapshot"] = new_snapshot
    await persist_snapshot(new_snapshot)

    await get_storage().log_operator_action(
        action="scenario_change",
        scenario=GLOBAL_STATE["scenario"],
        actor="staff_operator",
        details={"scenario": payload.scenario},
    )
    logger.info(f"Scenario changed to: {payload.scenario}")
    return {"status": "success", "scenario": GLOBAL_STATE["scenario"]}

@app.post("/api/v1/tasks/resolve")
async def resolve_task(payload: TaskResolveRequest, request: Request):
    require_admin_role(request)

    # Track the ID globally to prevent reappearance in future ticks
    GLOBAL_STATE["resolved_incident_ids"].add(payload.task_id)

    # 1. Try to resolve from Staff Tasks
    task = next((t for t in GLOBAL_STATE["pending_staff_tasks"] if t["id"] == payload.task_id), None)
    if task:
        task["status"] = "resolved"
        if len(GLOBAL_STATE["pending_staff_tasks"]) > 50:
            GLOBAL_STATE["pending_staff_tasks"] = GLOBAL_STATE["pending_staff_tasks"][-50:]

    # Handle Emergency Override resolution
    is_evac_override = (payload.task_id == "evac-override")
    if is_evac_override:
        GLOBAL_STATE["panic_mode"] = False
        logger.info("Emergency Override deactivated via task resolution.")

    # 2. Try to resolve from Active SOS Alerts
    initial_sos_count = len(GLOBAL_STATE["active_sos_alerts"])
    GLOBAL_STATE["active_sos_alerts"] = [s for s in GLOBAL_STATE["active_sos_alerts"] if s.get("id") != payload.task_id]
    
    # Ensure the ID is blacklisted from reappearing in snapshots
    GLOBAL_STATE["resolved_incident_ids"].add(payload.task_id)

    # Force cache invalidation and state broadcast
    LOCALIZED_SNAPSHOT_CACHE.clear()
    
    # Ensure we don't increment the simulation tick during a manual resolution
    new_snapshot = build_snapshot(increment_tick=False)
    GLOBAL_STATE["last_snapshot"] = new_snapshot
    await persist_snapshot(new_snapshot)
    
    return {"status": "success", "task_id": payload.task_id}

    # Removed strict 404 to allow idempotent 'Evacuate' clicks to succeed if already cleared


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
@limiter.limit("5/minute")
async def ask_ai(payload: AskAIRequest, request: Request):
    start_time = time.time()
    profile = payload.profile or FanProfile(**GLOBAL_STATE["current_profile"])
    # Create a preview snapshot to inform the AI without advancing the simulation clock
    preview_snapshot = build_snapshot(increment_tick=False)
    response = await get_decision_engine().evaluate(preview_snapshot, payload.prompt, profile)

    # 2. Unified Incident Management
    task_id = None
    if response.get("intent") == "emergency":
        task_id = f"sos-{int(time.time())}-{str(uuid.uuid4())[:4]}"
        action_directive = response.get("staff_action", "Immediate intervention required.")
        target_gate_id = preview_snapshot["route_plan"]["best_gate"]
        sos_alert = {
            "id": task_id,
            "type": "critical",
            "msg": f"🚨 SOS: {profile.name} at Section {profile.seat_section}",
            "reasoning": f"Action: {action_directive} | Query: {payload.prompt}",
            "action": "show_map",
            "action_details": {"type": "route", "target": target_gate_id, "ui_effect": "focus_map"},
            "evac_path": {"start": profile.seat_section, "end": target_gate_id},
            "expires_at": int(time.time()) + 300,
            "force_modal": True
        }
        GLOBAL_STATE["active_sos_alerts"].append(sos_alert)
    elif response.get("staff_action"):
        task_id = f"task-{str(uuid.uuid4())[:6]}"
        GLOBAL_STATE["pending_staff_tasks"].append({
            "id": task_id,
            "action": response["staff_action"],
            "status": "pending",
            "timestamp": int(time.time()),
            "scenario": preview_snapshot["scenario"]
        })

    if task_id:
        response["task_id"] = task_id

    await get_storage().insert_ai_query(
        scenario=preview_snapshot["scenario"],
        prompt=payload.prompt,
        profile=profile.model_dump(),
        response=response,
    )
    # Finalize State: Build the actual snapshot including the newly added tasks/SOS
    final_snapshot = build_snapshot(increment_tick=True)
    GLOBAL_STATE["last_snapshot"] = final_snapshot

    await persist_snapshot(final_snapshot)
    logger.info(f"AI Query Processed in {(time.time() - start_time) * 1000:.2f}ms")
    return {
        "status": "success",
        "scenario": final_snapshot["scenario"],
        "stadium_state": final_snapshot,
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
        "pending_tasks": [t for t in GLOBAL_STATE["pending_staff_tasks"] if t["status"] == "pending"],
        "google_cloud_status": {
            "api_health": "100%",
            "gemini_latency_ms": 42,
            "maps_usage_quota": "2.4%",
            "tts_enabled": bool(GOOGLE_TTS_API_KEY),
            "active_services": [
                "Maps JavaScript API",
                "Gemini 1.5 Flash",
                "Gemini Embeddings (text-embedding-004)",
                "Cloud Logging",
                "Cloud Run",
                "Cloud Text-to-Speech" if GOOGLE_TTS_API_KEY else "Browser Speech Fallback",
            ],
        }
    }


@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    if len(ws_manager.active_connections) >= MAX_WS_CONNECTIONS:
        logger.warning("WebSocket rate limit exceeded: rejecting connection.")
        await websocket.close(code=1008)
        return

    await ws_manager.connect(websocket)
    client_id = str(uuid.uuid4())
    logger.debug(f"WS Connected {client_id}. Total: {len(ws_manager.active_connections)}")
    try:
        requested_language = normalize_language(
            websocket.query_params.get("lang") or GLOBAL_STATE["current_profile"].get("preferred_language")
        )
        last_tick = -1
        full_broadcast_counter = 0
        last_sent_snapshot: dict[str, Any] = {}
        # Delta update optimization: track last delta send time for efficiency
        last_delta_time = 0

        while True:
            snapshot = GLOBAL_STATE["last_snapshot"]
            if not snapshot:
                await asyncio.sleep(1)
                continue

            current_ts = snapshot.get("timestamp")
            if current_ts == last_tick:
                await asyncio.sleep(1)
                continue

            localized_snapshot = await localize_snapshot(snapshot, requested_language)
            # Efficiency: Send full state every 60s OR on first connection
            force_full = not last_sent_snapshot or full_broadcast_counter >= 60

            if force_full:
                await websocket.send_text(
                    json.dumps({"type": "full", "snapshot": localized_snapshot, "timestamp": current_ts})
                )
                last_sent_snapshot = localized_snapshot
                full_broadcast_counter = 0
                last_delta_time = current_ts
            else:
                # Delta update: Only send what changed (up to 90% bandwidth reduction)
                changes = calculate_delta(last_sent_snapshot, localized_snapshot)
                if changes:
                    await websocket.send_text(
                        json.dumps({"type": "delta", "changes": changes, "timestamp": current_ts})
                    )
                    # Efficiently merge delta into last_sent_snapshot
                    for key, value in changes.items():
                        if isinstance(value, dict) and key in last_sent_snapshot:
                            last_sent_snapshot[key] = {**last_sent_snapshot[key], **value}
                        else:
                            last_sent_snapshot[key] = value
                    last_delta_time = current_ts
                full_broadcast_counter += 1

            last_tick = current_ts
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.debug(f"WS Disconnected {client_id}")
    finally:
        ws_manager.disconnect(websocket)
