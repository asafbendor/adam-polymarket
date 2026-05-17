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
import memory

DB = os.getenv("DB_PATH", "adam.db")
state.init(DB)
memory.init()  # ensure all tables exist

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


@app.get("/api/agents")
async def get_agents():
    try:
        return JSONResponse({
            "scout":  {
                "name": "Scout Agent",
                "role": "Finds opportunities via Claude Haiku + Binance",
                "learnings": memory.recall_all("scout"),
                "log": memory.get_log_entries("scout", 20),
            },
            "trader": {
                "name": "Trader Agent",
                "role": "Places orders, self-heals on errors",
                "fixes": memory.recall_all("trader"),
                "log": memory.get_log_entries("trader", 20),
            },
            "outcomes": memory.get_outcomes(10),
        })
    except Exception as e:
        return JSONResponse({"error": str(e), "scout": {"learnings":{}, "log":[]},
                             "trader": {"fixes":{}, "log":[]}, "outcomes": []})


@app.get("/api/status")
async def get_status():
    """Returns system status - useful to verify Adam is running."""
    import sqlite3
    try:
        with sqlite3.connect(DB) as c:
            bets    = c.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
            pending = c.execute("SELECT COUNT(*) FROM bets WHERE status='pending'").fetchone()[0]
            last_log = c.execute("SELECT ts,message FROM log ORDER BY id DESC LIMIT 1").fetchone()
            last_agent = c.execute("SELECT agent,ts,message FROM agent_log ORDER BY id DESC LIMIT 1").fetchone()
        return JSONResponse({
            "status":       "running",
            "total_bets":   bets,
            "pending_bets": pending,
            "last_log":     {"ts": last_log[0], "msg": last_log[1]} if last_log else None,
            "last_agent":   {"agent": last_agent[0], "ts": last_agent[1], "msg": last_agent[2]} if last_agent else None,
            "db":           DB,
        })
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
