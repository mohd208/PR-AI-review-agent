import asyncio
import logging

from .agent import create_autofix_pr, lock_for, repair_pr, retry_autofix, slug
from .config import Settings
from .github import GitHub
from .state import State

logger = logging.getLogger(__name__)

AUTOFIX_PREFIX = "autofix/ci-"


async def poll_forever(config: Settings, state: State) -> None:
    logger.info("Polling started: every %ds across %d repo(s)", config.poll_interval_seconds, len(config.allowed_repos))
    while True:
        for repo in config.allowed_repos:
            try:
                await poll_repo(repo, config, state)
            except Exception:
                logger.exception("Poll cycle failed for %s", repo)
        await asyncio.sleep(config.poll_interval_seconds)


async def poll_repo(repo: str, config: Settings, state: State) -> None:
    gh = GitHub(config.github_token, repo)
    prs = await gh.list_open_prs()
    logger.info("Poll %s: %d open PR(s)", repo, len(prs))

    tasks = []
    autofix_prs = []
    for pr in prs:
        if pr["head"]["ref"].startswith(AUTOFIX_PREFIX):
            # These are handled by the pipeline-failure flow below, not the generic review —
            # otherwise a single fix commit would trigger two independent Claude passes on it.
            autofix_prs.append(pr)
            continue
        if state.get_pr_sha(repo, pr["number"]) != pr["head"]["sha"]:
            tasks.append(_scan_pr(gh, repo, pr, config, state))

    repo_info = await gh.get_repo()
    tasks.append(_check_default_branch(gh, repo, repo_info["default_branch"], config, state))
    for pr in autofix_prs:
        tasks.append(_check_autofix_branch(gh, repo, pr, config, state))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error("Poll task for %s raised", repo, exc_info=result)


async def _scan_pr(gh: GitHub, repo: str, pr: dict, config: Settings, state: State) -> None:
    number = pr["number"]
    branch = pr["head"]["ref"]
    head_repo = pr.get("head", {}).get("repo", {}).get("full_name", "")
    async with lock_for(f"{repo}:{branch}"):
        files = await gh.pr_files(number)
        await repair_pr(repo, number, branch, pr["title"], files, config, head_repo)
        updated = await gh.request("GET", f"/repos/{repo}/pulls/{number}")
        state.set_pr_sha(repo, number, updated["head"]["sha"])


async def _check_default_branch(gh: GitHub, repo: str, branch: str, config: Settings, state: State) -> None:
    run = await gh.latest_run(branch)
    if not run or run.get("conclusion") != "failure":
        return
    if state.get_ci_run(repo, branch) == run["id"]:
        return

    branch_name = f"{AUTOFIX_PREFIX}{slug(run['name'])}"
    async with lock_for(f"{repo}:{branch_name}"):
        existing_pr = await gh.find_pr_by_branch(branch_name)
        if not existing_pr:
            await create_autofix_pr(gh, branch_name, run, config)
    state.set_ci_run(repo, branch, run["id"])


async def _check_autofix_branch(gh: GitHub, repo: str, pr: dict, config: Settings, state: State) -> None:
    branch = pr["head"]["ref"]
    run = await gh.latest_run(branch)
    if not run:
        return
    if state.get_ci_run(repo, branch) == run["id"]:
        return

    if run.get("conclusion") == "failure":
        state.clear_notified_passing(repo, branch)
        async with lock_for(f"{repo}:{branch}"):
            await retry_autofix(gh, pr, branch, run, config)
    elif run.get("conclusion") == "success" and not state.is_notified_passing(repo, branch):
        await gh.comment(pr["number"], "✅ **PR AutoFix Agent**\n\nThe pipeline is now passing. Ready for review/merge.")
        state.set_notified_passing(repo, branch)

    state.set_ci_run(repo, branch, run["id"])
