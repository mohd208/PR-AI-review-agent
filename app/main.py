import hashlib
import hmac
import json
import logging
import os
import sys

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .agent import handle_workflow_failure, repair_pr
from .config import settings
from .github import GitHub

if hasattr(os, "geteuid") and os.geteuid() == 0:
    sys.exit(
        "Refusing to start as root: Claude Code rejects --dangerously-skip-permissions "
        "when run as root/sudo, so every autofix would silently fail. Run this service "
        "as a non-root user instead."
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="PR AutoFix Agent")


def verified(body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(settings().github_webhook_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature.removeprefix("sha256="), expected)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


async def review_pr(repo: str, number: int, branch: str, title: str, head_repo: str) -> None:
    logger.info("PR review start: %s#%d branch=%s", repo, number, branch)
    files = await GitHub(settings().github_token, repo).pr_files(number)
    logger.info("PR review: %s#%d has %d changed file(s)", repo, number, len(files))
    await repair_pr(repo, number, branch, title, files, settings(), head_repo)
    logger.info("PR review done: %s#%d", repo, number)


@app.post("/webhooks/github", status_code=202)
async def webhook(request: Request, background_tasks: BackgroundTasks, x_github_event: str = Header(""), x_hub_signature_256: str | None = Header(None)):
    body = await request.body()
    if not verified(body, x_hub_signature_256):
        raise HTTPException(401, "Invalid webhook signature")
    payload = json.loads(body)
    repo = payload.get("repository", {}).get("full_name", "").lower()
    action = payload.get("action")
    logger.info("Webhook received: event=%s action=%s repo=%s", x_github_event, action, repo)

    if repo not in settings().allowed_repos:
        logger.info("Ignoring %s: repository not allowlisted", repo)
        return {"accepted": False, "reason": "Repository is not allowlisted"}

    if x_github_event == "pull_request" and action in {"opened", "synchronize", "reopened"}:
        pr = payload["pull_request"]
        sender = payload.get("sender", {}).get("login")
        sender_is_bot = sender == settings().bot_login
        # A "synchronize" caused by our own fix commit has the bot as the event's sender,
        # regardless of who authored the PR. Skip those, or we'd review -> push -> synchronize
        # -> review forever. Still allow the one "opened" event when the bot creates its own
        # autofix PR (goal 2), since sender == bot there too but it's a one-time review.
        if not sender_is_bot or action == "opened":
            head_repo = pr.get("head", {}).get("repo", {}).get("full_name", "")
            logger.info(
                "Queuing PR review: %s#%d (%s) branch=%s sender=%s",
                repo, pr["number"], action, pr["head"]["ref"], sender,
            )
            background_tasks.add_task(review_pr, repo, pr["number"], pr["head"]["ref"], pr["title"], head_repo)
        else:
            logger.info("Skipping %s#%d: event was caused by the bot's own push", repo, pr["number"])

    elif x_github_event == "workflow_run" and action == "completed":
        run = payload["workflow_run"]
        logger.info(
            "Workflow run completed: %s run=%s name=%s conclusion=%s event=%s branch=%s",
            repo, run["id"], run["name"], run.get("conclusion"), run.get("event"), run.get("head_branch"),
        )
        if run.get("conclusion") == "failure":
            logger.info("Queuing workflow-failure repair: %s run=%s", repo, run["id"])
            background_tasks.add_task(handle_workflow_failure, repo, run, settings())

    return {"accepted": True}
