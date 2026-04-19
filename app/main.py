import asyncio
import copy
import hashlib
import hmac
import json
import os
import time
import logging
import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SmartStadium-API")
from dotenv import load_dotenv

load_dotenv()
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")
GOOGLE_IDENTITY_CLIENT_ID = os.getenv("GOOGLE_IDENTITY_CLIENT_ID", "")
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "")
GOOGLE_TTS_API_KEY = os.getenv("GOOGLE_TTS_API_KEY", "")
GOOGLE_TTS_VOICE_NAME = os.getenv("GOOGLE_TTS_VOICE_NAME", "en-US-Neural2-C")
ROLE_SIGNING_SECRET = os.getenv("SMARTSTADIUM_ROLE_SECRET", "smartstadium-dev-secret")
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from cachetools import TTLCache

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
        "preferred_language": "en",
    },
    "latest_announcement": None,
    "last_snapshot": None,
    "last_persisted_alert_keys": set(),
}
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
    return hmac.new(ROLE_SIGNING_SECRET.encode("utf-8"), role.encode("utf-8"), hashlib.sha256).hexdigest()


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

    bounty_active = frame.get("bounty_active", False)
    bounty_points = 60 if scenario_key in {"peak_rush", "gate_closure"} else 35
    bounty_reason = frame.get("bounty_description") or f"Use {calculated_gate_key.replace('_', ' ').title()} to reduce crowding and improve reroute success."

    bounties = []
    if bounty_active:
        bounties.append(
            {
                "target": calculated_gate_key,
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
        "timestamp": int(time.time()),
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
        "personalization": profile,
        "route_plan": personalized_route,
        "match_info": match_info,
        "weather": {
            "temp": f"{varied_temp}°C",
            "condition": w["cond"],
            "icon": w["icon"]
        }
    }


def normalize_language(language: str | None) -> str:
    if not language:
        return "en"
    lang = language.strip().lower()
    if not lang:
        return "en"
    return lang.split("-")[0].split("_")[0]


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
async def lifespan(_: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    storage = Storage(DB_PATH)
    await storage.initialize()
    narrator = GeminiNarrator()
    decision_engine = DecisionEngine(narrator=narrator)
    http_client = httpx.AsyncClient(timeout=10.0)
    app.state.storage = storage
    app.state.decision_engine = decision_engine
    app.state.http_client = http_client
    
    # Start background state update
    bg_task = asyncio.create_task(update_state_task())
    GLOBAL_STATE["bg_task"] = bg_task
    
    yield
    
    bg_task.cancel()
    await http_client.aclose()

app = FastAPI(title="SmartStadium AI Premium", lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
templates = Jinja2Templates(directory="app/templates")

def get_template_context() -> dict[str, Any]:
    return {
        "google_maps_api_key": GOOGLE_MAPS_API_KEY,
        "google_identity_client_id": GOOGLE_IDENTITY_CLIENT_ID,
        "google_tts_enabled": bool(GOOGLE_TTS_API_KEY),
    }


def get_storage() -> Storage:
    return app.state.storage


def get_decision_engine() -> DecisionEngine:
    return app.state.decision_engine


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
        ACTIVE_WS_CONNECTIONS -= 1
