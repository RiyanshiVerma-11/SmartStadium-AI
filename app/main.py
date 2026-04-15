from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import random
import time
import asyncio
import json

app = FastAPI(title="SmartStadium AI Premium")
templates = Jinja2Templates(directory="app/templates")

# Global state for prototype
GLOBAL_STATE = {
    "panic_mode": False
}

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

@app.websocket("/ws/data")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            # Generate highly dynamic data for that "living app" feel
            food_wait = random.randint(2, 25)
            restroom_wait = random.randint(1, 8)
            merch_wait = random.randint(5, 15)
            
            # Map zones to crowd statuses
            heatmaps = {
                "north_gate": random.choices(["low", "medium", "high", "critical"], weights=[10, 40, 40, 10])[0],
                "south_gate": random.choices(["low", "medium", "high"], weights=[60, 30, 10])[0],
                "concourse_a": random.choices(["low", "medium", "high"], weights=[20, 50, 30])[0],
                "food_court": random.choices(["medium", "high", "critical"], weights=[20, 50, 30])[0]
            }
            
            data = {
                "timestamp": int(time.time()),
                "emergency_override": GLOBAL_STATE["panic_mode"],
                "heatmaps": heatmaps,
                "wait_times_minutes": {
                    "food_stalls": food_wait,
                    "restrooms": restroom_wait,
                    "merch_stand": merch_wait
                },
                "gamification": {
                    "bounties": []
                },
                "alerts": [
                    {
                        "type": "info", 
                        "msg": "AI balancing load: Adjusting thermal controls in Concourse A.",
                        "reasoning": "Sensors detect sustained heat mass above threshold (24°C). Lowering ambient temperature to disperse gathering."
                    }
                ]
            }

            # Gamification logic
            if heatmaps["south_gate"] == "low":
                 data["gamification"]["bounties"].append({"target": "south_gate", "points": "+50", "reason": "Use the South Gate to balance load."})
                 
            if food_wait < 10:
                 data["gamification"]["bounties"].append({"target": "food_stalls", "points": "+15", "reason": "Off-peak dining bonus active."})

            # Explanations and alerts
            if heatmaps["north_gate"] == "critical":
                data["alerts"].append({
                    "type": "critical", 
                    "msg": "🚨 STAMPEDE RISK DETECTED: Deploying Rapid Response to North Gate.",
                    "reasoning": "Optical flow density surpassed 4.2 persons/sq-meter. Algorithm assigns 98% risk."
                })
            
            if GLOBAL_STATE["panic_mode"]:
                data["alerts"].append({
                    "type": "critical",
                    "msg": "🚨 EVACUATION ORDERED: Security team, execute lockdown protocols.",
                    "reasoning": "Manual operator override activated via Command Center."
                })
                
            await websocket.send_text(json.dumps(data))
            await asyncio.sleep(2) # Stream updates every 2 seconds
    except WebSocketDisconnect:
        pass
