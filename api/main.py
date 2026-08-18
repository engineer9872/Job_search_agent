import os
import sys
import logging

# Ensure project root directory is in sys.path for top-level package imports (DB, Scheduler, agent, etc.)
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Configure root logging BEFORE any app modules are imported below, so every
# module-level `logger = logging.getLogger(__name__)` call across the app
# (pipeline, connectors, quota tracker, etc.) actually emits at the level
# set by LOG_LEVEL in .env. Without this, the root logger defaults to
# WARNING with no handler and every logger.info(...) call is silently
# dropped -- only .warning()/.error() get through.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from DB import init_db
from api.routes import jobs_router
from Scheduler import start_scheduler, stop_scheduler, get_scheduler_status

# Lifespan event handler for starting and stopping background scheduler
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize DB & Background APScheduler
    try:
        init_db()
    except Exception as e:
        print(f"Warning: Database initialization deferred or error: {e}")

    start_scheduler(interval_hours=6)
    yield
    # Shutdown: Stop scheduler cleanly
    stop_scheduler()


app = FastAPI(
    title="AI Job Search Agent API",
    description="Multi-Source Job Aggregation & Search API",
    version="1.0.0",
    lifespan=lifespan,
)

# Enable CORS for local dev / frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API Routers
app.include_router(jobs_router)
# AI assistant route unregistered: the drawer it served was unreachable in the
# UI (Sidebar declared onOpenAiChat but never rendered a trigger), so the
# feature never actually ran. The agent/ package is left in place rather than
# deleted, in case a script or future surface uses it.

# Scheduler Status Endpoint
@app.get("/api/scheduler/status")
def read_scheduler_status():
    return get_scheduler_status()

# Mount static frontend build directory (Vite output or fallback)
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
dist_dir = os.path.join(frontend_dir, "dist")
assets_dir = os.path.join(dist_dir, "assets")

if os.path.exists(assets_dir):
    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def read_root():
    dist_index = os.path.join(dist_dir, "index.html")
    index_file = dist_index if os.path.exists(dist_index) else os.path.join(frontend_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(
            index_file,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return {"message": "AI Job Search Agent API is running. Visit /docs for OpenAPI documentation."}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
