"""
app.py — MCE MCP Client
───────────────────────
LLM brain: Google Gemini (via Gemini API)
MCP server: Salesforce Marketing Cloud Engagement hosted MCP

Run:  py app.py
Open: http://localhost:8080
"""

import os, json, threading, webbrowser
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp_worker import MCPWorker

load_dotenv()

APP_PORT            = int(os.getenv("APP_PORT", "8080"))
OAUTH_CALLBACK_PORT = int(os.getenv("OAUTH_CALLBACK_PORT", "3030"))
CONFIG_FILE         = Path("ui_config.json")

app = FastAPI(title="MCE MCP Client")
STATIC_DIR = Path(__file__).parent / "static"

# ── Security: block requests from external origins ─────────────────────────────
# The app binds to localhost only, but a malicious website you visit in another
# tab could still trigger requests to http://localhost:8080 in your browser.
# This middleware rejects any POST/PUT/DELETE request that has a Host header
# pointing to a non-localhost address. Browsers will set Origin/Referer for
# cross-site requests — we reject those for state-changing methods.

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

ALLOWED_HOSTS = {"localhost", "127.0.0.1"}

class LocalhostOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Reject if Host header isn't localhost
        host = (request.headers.get("host") or "").split(":")[0]
        if host not in ALLOWED_HOSTS:
            return Response("Forbidden — localhost only", status_code=403)

        # For state-changing methods, also require Origin/Referer to be localhost
        # (protects against CSRF from malicious websites)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin") or ""
            referer = request.headers.get("referer") or ""
            check = origin or referer
            if check and not any(h in check for h in ("localhost", "127.0.0.1")):
                return Response("Forbidden — cross-origin request blocked", status_code=403)

        return await call_next(request)

app.add_middleware(LocalhostOnlyMiddleware)

worker: MCPWorker | None = None
brain = None
_lock = threading.Lock()

# ── Available Gemini models ────────────────────────────────────────────────────
GEMINI_MODELS = [
    {"id": "gemini-2.5-flash",      "label": "Gemini 2.5 Flash",      "note": "Recommended — best free tier limits"},
    {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash Lite", "note": "Fastest, lowest cost"},
    {"id": "gemini-2.5-pro",        "label": "Gemini 2.5 Pro",        "note": "Most capable, lower free tier quota"},
]

# ── Config helpers ─────────────────────────────────────────────────────────────

def load_ui_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_ui_config(data: dict):
    CONFIG_FILE.write_text(json.dumps(data, indent=2))

def get_cfg(key: str, fallback: str = "") -> str:
    return load_ui_config().get(key) or os.getenv(key, fallback)

# ── Brain factory ──────────────────────────────────────────────────────────────

def build_brain(cfg: dict):
    from gemini_brain import GeminiBrain
    return GeminiBrain(
        api_key=cfg["gemini_api_key"],
        model=cfg.get("model", "gemini-2.5-flash"),
    )

# ── Routes: static ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")

# ── Routes: config ─────────────────────────────────────────────────────────────

@app.get("/api/config/load")
def config_load():
    cfg = load_ui_config()
    gkey = cfg.get("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
    return {
        "mce_mcp_url":    cfg.get("mce_mcp_url") or os.getenv("MCE_MCP_URL", ""),
        "gemini_api_key": "••••••" if gkey else "",
        "model":          cfg.get("model") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        "configured": bool(
            (cfg.get("mce_mcp_url") or os.getenv("MCE_MCP_URL")) and gkey
        ),
    }

@app.post("/api/config/save")
async def config_save(request: Request):
    global brain
    body = await request.json()
    existing = load_ui_config()

    existing["mce_mcp_url"] = body.get("mce_mcp_url", existing.get("mce_mcp_url", ""))
    existing["model"]       = body.get("model", existing.get("model", "gemini-2.5-flash"))

    # Only overwrite key if a real (non-masked) value was sent
    new_key = body.get("gemini_api_key", "")
    if new_key and new_key != "••••••":
        existing["gemini_api_key"] = new_key

    save_ui_config(existing)
    brain = None  # force rebuild with new config
    return {"ok": True}

# ── Routes: models ─────────────────────────────────────────────────────────────

@app.get("/api/models")
def models_list():
    return {"models": GEMINI_MODELS}

@app.post("/api/model/set")
async def model_set(request: Request):
    global brain
    body = await request.json()
    cfg = load_ui_config()
    cfg["model"] = body.get("model", cfg.get("model", "gemini-2.5-flash"))
    save_ui_config(cfg)
    brain = None
    return {"ok": True, "model": cfg["model"]}

# ── Routes: status / connect ───────────────────────────────────────────────────

@app.get("/api/status")
def status():
    cfg  = load_ui_config()
    gkey = cfg.get("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
    model = cfg.get("model") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    mcp_url = cfg.get("mce_mcp_url") or os.getenv("MCE_MCP_URL", "")
    return {
        "mcp_url_configured":    bool(mcp_url) and "YOUR_TENANT_ID" not in mcp_url,
        "gemini_key_configured": bool(gkey) and gkey != "your_gemini_api_key_here",
        "model":         model,
        "mcp_status":    worker.status if worker else "idle",
        "mcp_error":     worker.error  if worker else None,
        "token_expired": bool(worker and getattr(worker, "_token_expired", False)),
        "tool_count":    len(worker.tools_cache) if worker else 0,
    }

@app.post("/api/connect")
def connect():
    global worker
    with _lock:
        if worker and worker.status in ("connecting", "awaiting_auth", "ready"):
            return {"ok": True, "status": worker.status}
        mcp_url = get_cfg("mce_mcp_url") or get_cfg("MCE_MCP_URL")
        if not mcp_url or "YOUR_TENANT_ID" in mcp_url:
            return JSONResponse(
                {"ok": False, "error": "MCE MCP URL not configured. Open Settings first."},
                status_code=400,
            )
        worker = MCPWorker(mcp_url, callback_port=OAUTH_CALLBACK_PORT)
        worker.start()
    return {"ok": True, "status": "connecting"}

@app.post("/api/reconnect")
def reconnect():
    """Clear expired token and restart the MCP worker for a fresh browser login."""
    global worker
    from mcp_worker import _clear_tokens
    import time

    # Fully stop and abandon the old worker
    old_worker = worker
    worker = None
    if old_worker:
        try:
            old_worker.stop()
        except Exception:
            pass
        # Give the old worker a moment to release its callback port (3030)
        time.sleep(1.5)

    # Clear the expired token so the SDK forces a fresh browser login
    _clear_tokens()

    # Start fresh
    with _lock:
        mcp_url = get_cfg("mce_mcp_url") or get_cfg("MCE_MCP_URL")
        if not mcp_url or "YOUR_TENANT_ID" in mcp_url:
            return JSONResponse({"ok": False, "error": "MCE MCP URL not configured."}, status_code=400)
        worker = MCPWorker(mcp_url, callback_port=OAUTH_CALLBACK_PORT)
        worker.start()
    return {"ok": True, "status": "connecting"}

@app.get("/api/tools")
def tools():
    if not worker or worker.status != "ready":
        return {"tools": [], "status": worker.status if worker else "idle"}
    return {"tools": worker.tools_cache, "status": "ready"}

# ── Routes: chat ───────────────────────────────────────────────────────────────

def get_brain():
    global brain
    if brain:
        return brain
    cfg  = load_ui_config()
    gkey = cfg.get("gemini_api_key") or os.getenv("GEMINI_API_KEY", "")
    if not gkey or gkey == "your_gemini_api_key_here":
        raise RuntimeError("Gemini API key not set. Open Settings ⚙ to add it.")
    cfg["gemini_api_key"] = gkey
    cfg["model"] = cfg.get("model") or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    brain = build_brain(cfg)
    return brain

@app.post("/api/chat")
async def chat(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid request body"}, status_code=400)
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)
    if not worker or worker.status != "ready":
        return JSONResponse({"error": "MCP not connected. Click Connect first."}, status_code=400)
    try:
        b = get_brain()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    events: list[dict] = []
    def run_chat():
        return b.chat(
            message, worker.tools_cache,
            lambda n, a: worker.call_tool(n, a),
            lambda kind, payload: events.append({"kind": kind, **payload}),
        )
    try:
        import anyio
        reply = await anyio.to_thread.run_sync(run_chat)
        return JSONResponse({"reply": reply, "events": events})
    except Exception as e:
        # Log only the exception type and message — not the full traceback or
        # request body which may contain sensitive tool arguments
        print(f"\n[chat error] {type(e).__name__}: {e}\n")
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

@app.post("/api/reset")
def reset():
    if brain: brain.reset()
    return {"ok": True}

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    url = f"http://localhost:{APP_PORT}"
    print(f"\n  MCE MCP Client → {url}\n  Ctrl+C to stop\n")
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="localhost", port=APP_PORT, log_level="warning")
