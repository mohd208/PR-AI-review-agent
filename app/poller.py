import asyncio
import logging

from .agent import create_autofix_pr, lock_for, repair_pr, repair_pr_ci_failure, retry_autofix, slug
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

    review_prs = []
    autofix_prs = []
    for pr in prs:
        if pr["head"]["ref"].startswith(AUTOFIX_PREFIX):
            # These are handled by the pipeline-failure flow below, not the generic review —
            # otherwise a single fix commit would trigger two independent Claude passes on it.
            autofix_prs.append(pr)
        else:
            review_prs.append(pr)

    tasks = [_scan_pr(gh, repo, pr, config, state) for pr in review_prs]

    repo_info = await gh.get_repo()
    default_branch = repo_info["default_branch"]
    tasks.append(_check_default_branch(gh, repo, default_branch, config, state))
    for pr in autofix_prs:
        tasks.append(_check_autofix_branch(gh, repo, pr, config, state))

    review_desc = ", ".join(f"#{pr['number']} ({pr['head']['ref']})" for pr in review_prs) or "none"
    autofix_desc = ", ".join(f"#{pr['number']} ({pr['head']['ref']})" for pr in autofix_prs) or "none"
    logger.info(
        "==== %s: checking PR(s) [%s] | default branch '%s' | autofix PR(s) [%s] ====",
        repo, review_desc, default_branch, autofix_desc,
    )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            logger.error("Poll task for %s raised", repo, exc_info=result)


async def _latest_failed_run_for_sha(gh: GitHub, sha: str) -> dict | None:
    runs = await gh.runs_for_sha(sha)
    return next((r for r in runs if r.get("conclusion") == "failure"), None)


async def _scan_pr(gh: GitHub, repo: str, pr: dict, config: Settings, state: State) -> None:
    # repair_pr/repair_pr_ci_failure acquire this same branch's lock themselves around the actual
    # clone/fix/push — don't also hold it here, or the (non-reentrant) second acquire deadlocks
    # the task forever.
    number = pr["number"]
    branch = pr["head"]["ref"]
    sha = pr["head"]["sha"]
    head_repo = pr.get("head", {}).get("repo", {}).get("full_name", "")

    sha_changed = state.get_pr_sha(repo, number) != sha
    # Checked every cycle regardless of sha_changed, since a PR's CI often finishes well after
    # the commit that triggered it was already reviewed.
    ci_failure = await _latest_failed_run_for_sha(gh, sha)
    ci_is_new = ci_failure is not None and state.get_ci_run(repo, branch) != ci_failure["id"]
    logger.info(
        "PR %s#%d (branch=%s sha=%s): sha_changed=%s ci_failure=%s ci_is_new=%s",
        repo, number, branch, sha[:8], sha_changed,
        f"{ci_failure['name']}#{ci_failure['id']}" if ci_failure else None, ci_is_new,
    )

    if not sha_changed and not ci_is_new:
        return

    files = await gh.pr_files(number)
    if ci_is_new:
        await repair_pr_ci_failure(repo, pr, files, config, head_repo, ci_failure)
        state.set_ci_run(repo, branch, ci_failure["id"])
    else:
        await repair_pr(repo, number, branch, pr["title"], files, config, head_repo)

    updated = await gh.request("GET", f"/repos/{repo}/pulls/{number}")
    state.set_pr_sha(repo, number, updated["head"]["sha"])


async def _check_default_branch(gh: GitHub, repo: str, branch: str, config: Settings, state: State) -> None:
    run = await gh.latest_run(branch)
    if not run:
        logger.info("Default branch %s@%s: no completed workflow run found yet", repo, branch)
        return

    last_seen = state.get_ci_run(repo, branch)
    logger.info(
        "Default branch %s@%s: latest run=%s (%s) conclusion=%s, last_seen=%s",
        repo, branch, run["id"], run.get("name"), run.get("conclusion"), last_seen,
    )
    if last_seen is None:
        # First time watching this branch — record a baseline without reacting. Otherwise, on
        # first startup (or a newly allowlisted repo) we'd "fix" whatever failure happened to
        # already be sitting there, rather than only reacting to failures from this point on.
        state.set_ci_run(repo, branch, run["id"])
        logger.info(
            "Baseline recorded for %s@%s: run=%s conclusion=%s (not reacting to pre-existing runs — "
            "the *next* new failed run after this one is what will trigger an autofix PR)",
            repo, branch, run["id"], run.get("conclusion"),
        )
        return
    if run["id"] == last_seen:
        logger.info("Default branch %s@%s: run=%s already handled, nothing new", repo, branch, run["id"])
        return

    if run.get("conclusion") == "failure":
        branch_name = f"{AUTOFIX_PREFIX}{slug(run['name'])}"
        async with lock_for(f"{repo}:{branch_name}"):
            existing_pr = await gh.find_pr_by_branch(branch_name)
            if existing_pr:
                logger.info(
                    "Default branch %s@%s failed (run=%s) but %s#%d is already open for that "
                    "workflow — not creating another PR. If that PR is stuck/exhausted, close or "
                    "merge it so a fresh failure can open a new one.",
                    repo, branch, run["id"], repo, existing_pr["number"],
                )
            else:
                logger.info("Default branch %s@%s: new failure (run=%s), creating autofix PR", repo, branch, run["id"])
                await create_autofix_pr(gh, branch_name, run, config)
    else:
        logger.info("Default branch %s@%s: run=%s concluded '%s', nothing to fix", repo, branch, run["id"], run.get("conclusion"))
    state.set_ci_run(repo, branch, run["id"])


async def _check_autofix_branch(gh: GitHub, repo: str, pr: dict, config: Settings, state: State) -> None:
    branch = pr["head"]["ref"]
    run = await gh.latest_run(branch)
    if not run:
        logger.info("Autofix PR %s#%d (branch=%s): no completed run found yet", repo, pr["number"], branch)
        return
    if state.get_ci_run(repo, branch) == run["id"]:
        return
    logger.info(
        "Autofix PR %s#%d (branch=%s): latest run=%s conclusion=%s",
        repo, pr["number"], branch, run["id"], run.get("conclusion"),
    )

    if run.get("conclusion") == "failure":
        state.clear_notified_passing(repo, branch)
        async with lock_for(f"{repo}:{branch}"):
            await retry_autofix(gh, pr, branch, run, config)
    elif run.get("conclusion") == "success" and not state.is_notified_passing(repo, branch):
        await gh.comment(pr["number"], "✅ **PR AutoFix Agent**\n\nThe pipeline is now passing. Ready for review/merge.")
        state.set_notified_passing(repo, branch)

    state.set_ci_run(repo, branch, run["id"])
