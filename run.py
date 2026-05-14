"""
Single entry point - runs Adam (agent loop) and Dashboard (web) concurrently.
Railway runs this as the web service on $PORT.
"""
import asyncio
import logging
import os
import threading

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def start_dashboard():
    """Run FastAPI dashboard in a background thread."""
    import dashboard  # noqa: F401 - registers routes
    from dashboard import app
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")


async def start_adam():
    """Run Adam agent loop."""
    from adam import main
    await main()


if __name__ == "__main__":
    # Dashboard runs in a background thread (uvicorn is synchronous)
    t = threading.Thread(target=start_dashboard, daemon=True)
    t.start()

    # Adam runs in the main asyncio loop
    asyncio.run(start_adam())
