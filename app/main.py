import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from .config import settings
from .poller import poll_forever
from .state import State

if hasattr(os, "geteuid") and os.geteuid() == 0:
    sys.exit(
        "Refusing to start as root: Claude Code rejects --dangerously-skip-permissions "
        "when run as root/sudo, so every autofix would silently fail. Run this service "
        "as a non-root user instead."
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = settings()
    state = State(Path(config.state_file))
    poll_task = asyncio.create_task(poll_forever(config, state))
    logger.info("PR AutoFix Agent started, polling %d repo(s)", len(config.allowed_repos))
    yield
    poll_task.cancel()


app = FastAPI(title="PR AutoFix Agent", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
