<div align="center">
  <h1>🏟️ SmartStadium AI Premium</h1>
  <p><em>Real-Time Event Intelligence and Crowd Flow Optimization Platform</em></p>

  [![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)](https://fastapi.tiangolo.com)
  [![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?style=for-the-badge&logo=docker&logoColor=white)](https://www.docker.com/)
  [![WebSockets](https://img.shields.io/badge/WebSockets-black?style=for-the-badge&logo=socket.io)](https://developer.mozilla.org/en-US/docs/Web/API/WebSockets_API)
</div>

---

## 📖 Overview

**SmartStadium AI** is an advanced, edge-computed prototype designed to solve the largest friction points at massive sporting events (50,000+ attendees). By leveraging simulated real-time data processing and a multi-persona architecture, this platform eliminates waiting bottlenecks, prioritizes fan safety, and offers unprecedented insights into human crowd dynamics.

This dual-interface application provides simultaneous access for two distinct user groups:
1. **The Staff Command Center (`/staff`)**: A high-density, cyberpunk-inspired operational dashboard for stadium security.
2. **The Fan Companion WebApp (`/fan`)**: A beautiful, mobile-first, interactive ticket/map portfolio for attendees.

---

## ✨ Cutting-Edge Features

Built specifically to conquer edge cases, we have implemented several highly advanced "hackathon winning" features:

### 1. 🚨 Emergency Intelligence (Override Protocol)
Safety cannot wait. The Command Center features a **Manual Evacuation Override**. When a staff member presses this, the backend instantly broadcasts a WebSocket takeover. Every Fan Companion App in the stadium immediately locks its UI and forces an un-dismissable RED strobe effect with optimal evacuation paths.

### 2. 🏆 AI Gamification (Flow Bounties)
Why let crowds build up when you can incentivize them to disperse? The AI engine constantly watches wait times. If Concourse B is crowded but South Gate is empty, the system pushes **Flow Bounties** to the Fan App. Fans can dynamically earn `PTR` points (redeemable for merch/food) by actively choosing to balance the stadium's load!

### 3. 🤔 "Glass Box" AI Explanations
Users distrust black-box algorithms. Our AI Chatbot actively proves its value by exposing a `🧠 AI Reasoning` layer. When the system directs you away from a bathroom, it exposes the raw telemetry driving the decision (e.g., *"Optical flow density surpassed risk threshold"*). 

### 4. 📴 Offline / Edge-Mode Fallback
Real stadiums have terrible cell reception. This app is built to handle it. Native `WebSocket.onclose` event interception gracefully visually degrades the UI with a "Low Signal: SMS Fallback" banner, ensuring users know the app hasn't "crashed", but is operating on cached edge memory.

### 5. 🗺️ Fully Interactive Interactive Micro-Apps
The Fan App isn't just static text:
- **Interactive 3D Radar**: Plugs directly into real navigable Google Maps embeds (e.g., SoFi Stadium) with pulsing CSS heatmaps overlaid.
- **Express Concessions**: Slide-up vendor carts letting you "mock-order" stadium dogs directly to your seat (SEC 112).
- **VIP Ticket Access**: Modal NFC QR ticket stubs fetched dynamically.

---

## 🏗️ Architecture

- **Backend Framework**: Python FastAPI.
- **Real-Time Data**: Raw `asyncio` WebSockets capable of streaming telemetry updates every 2 seconds without HTTP polling overhead.
- **Frontend**: Vanilla HTML/JS with Tailwind CSS `glassmorphism` aesthetics for zero-build-step rapid prototyping.
- **Infrastructure**: Fully Dockerized with live Hot-Reloading volume mounts.

---

## 🚀 How to Run Locally

You only need Docker installed on your machine to run the entire stack.

**1. Clone and Navigate:**
```bash
cd "23 ai stadium"
```

**2. Spin up the Container:**
```bash
docker compose up --build
```
> *Note: We use hot-reloading volumes, meaning if you edit the HTML files while Docker is running, the changes will reflect instantly without a rebuild!*

**3. Test the Multi-Screen Experience:**
Open two side-by-side browser windows:
- 👨‍✈️ **Staff Interface**: [http://localhost:8000/staff](http://localhost:8000/staff)
- 🤳 **Fan Interface**: [http://localhost:8000/fan](http://localhost:8000/fan)

**4. Trigger the Magic:**
Try clicking the red `🚨 Manual Evacuation Override` button in the Staff view, and watch the Fan window instantly react to the broadcast!

---

<p align="center">
  <em>Built for Prompt Wars. 🚀</em>
</p>
