"""
Dashboard - FastAPI web app showing Adam's bets and stats.
"""
import os, sys
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

_ROOT = Path(__file__).resolve().parent
os.chdir(_ROOT)
sys.path.insert(0, str(_ROOT))
import state

DB = os.getenv("DB_PATH", "adam.db")
state.init(DB)

app = FastAPI(title="Adam Dashboard")
templates = Jinja2Templates(directory=str(_ROOT / "templates"))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/stats")
async def get_stats():
    today = state.today()
    return JSONResponse({
        "today":        state.get_daily(today, DB),
        "alltime":      state.all_time(DB),
        "daily_budget": float(os.getenv("DAILY_BUDGET","10")),
        "date":         today,
    })


@app.get("/api/bets")
async def get_bets(limit: int = 100):
    return JSONResponse({"bets": state.all_bets(limit, DB)})


@app.get("/api/log")
async def get_log(limit: int = 60):
    return JSONResponse({"log": state.get_log(limit, DB)})


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
