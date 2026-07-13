import hashlib
import hmac
import json
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
    files = await GitHub(settings().github_token, repo).pr_files(number)
    await repair_pr(repo, number, branch, title, files, settings(), head_repo)


@app.post("/webhooks/github", status_code=202)
async def webhook(request: Request, background_tasks: BackgroundTasks, x_github_event: str = Header(""), x_hub_signature_256: str | None = Header(None)):
    body = await request.body()
    if not verified(body, x_hub_signature_256):
        raise HTTPException(401, "Invalid webhook signature")
    payload = json.loads(body)
    repo = payload.get("repository", {}).get("full_name", "").lower()
    if repo not in settings().allowed_repos:
        return {"accepted": False, "reason": "Repository is not allowlisted"}

    if x_github_event == "pull_request" and payload.get("action") in {"opened", "synchronize", "reopened"}:
        pr = payload["pull_request"]
        is_bot_pr = pr.get("user", {}).get("login") == settings().bot_login
        # Review the bot's own autofix PRs too, but only once on creation — otherwise our own
        # fix commit fires "synchronize" and we'd re-review (and re-push) ourselves forever.
        if not is_bot_pr or payload["action"] == "opened":
            head_repo = pr.get("head", {}).get("repo", {}).get("full_name", "")
            background_tasks.add_task(review_pr, repo, pr["number"], pr["head"]["ref"], pr["title"], head_repo)
    elif x_github_event == "workflow_run" and payload.get("action") == "completed":
        run = payload["workflow_run"]
        if run.get("conclusion") == "failure":
            background_tasks.add_task(handle_workflow_failure, repo, run, settings())
    return {"accepted": True}
