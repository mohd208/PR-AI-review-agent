import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from .config import Settings
from .github import CloneFailed, GitHub, PushRejected

logger = logging.getLogger(__name__)

SYSTEM_RULES = """You are a careful repository repair agent working non-interactively. Work only
on the stated issue and make the smallest correct fix.

The codebase may be in any programming language or file format: application code in any language,
Dockerfiles, GitHub Actions workflows (.github/workflows/*.yml), Terraform (*.tf), Kubernetes
manifests (*.yaml/*.yml), or anything else. Use whatever validation fits the files you touched —
the project's own build/test/lint commands if present, plus format-specific tools where relevant
and available (e.g. `node --check`, `python -m py_compile`/pytest, `go build`/`go vet`,
`terraform validate`/`terraform fmt -check`, `kubeval`/`kubectl --dry-run=client`, `hadolint`,
`actionlint`, `yamllint`).

Hard rules:
- Never modify credentials, secrets, lockfiles (unless the task is literally a lockfile fix), CI
  permissions, or branch-protection configuration.
- Never disable, skip, or weaken tests or checks to make them pass.
- Never run destructive or "apply" commands: no `terraform apply`, `terraform destroy`,
  `kubectl apply`, `kubectl delete`, cloud CLI mutations, or anything that changes real
  infrastructure or deployed state. You may run read-only/plan/validate/lint commands.
- Never merge, close, approve, or otherwise change the state of any pull request, and never run
  `gh pr merge`, `gh pr close`, `gh pr review`, or any repository-administration command. Your only
  job is editing files in the working tree — a human always decides whether and when to merge.
- Only edit files in the working tree; never touch state files, kubeconfig, or cloud credentials.

The first line of your final report must be exactly one of:
`STATUS: OK - <one short sentence>` if the code is already correct and you made no changes, or
`STATUS: FIXED - <one short sentence describing the issue and the fix>` if you made changes.
Follow that line with a short summary of changed files and validation performed. If no safe fix
exists, do not edit any files and explain why."""

_STATUS_RE = re.compile(r"^STATUS:\s*(OK|FIXED)\s*-\s*(.+)$", re.MULTILINE)


def _format_report(pushed: bool, result: str) -> str:
    details = f"<details>\n<summary>Full report</summary>\n\n{result}\n</details>"
    match = _STATUS_RE.search(result)
    if not match:
        heading = "✅ Pushed a focused repair commit." if pushed else "⚠️ Could not complete the scan — see details below."
        return f"{heading}\n\n{details}"

    status, summary = match.group(1), match.group(2).strip()
    if status == "OK":
        heading = f"✅ Everything is good — {summary}"
    elif pushed:
        heading = f"🔧 Found an issue: {summary} Fixed and pushed."
    else:
        heading = f"⚠️ Found an issue: {summary} Could not push the fix — see details below."
    return f"{heading}\n\n{details}"


# Serializes repairs per branch so two overlapping poll cycles can't race each other's
# clone/commit/push or double-count a retry attempt.
_branch_locks: dict[str, asyncio.Lock] = {}

# Caps how many `claude` subprocesses run at once, since each is a real, resource-heavy process.
_repair_semaphore: asyncio.Semaphore | None = None


def lock_for(key: str) -> asyncio.Lock:
    return _branch_locks.setdefault(key, asyncio.Lock())


def _semaphore(config: Settings) -> asyncio.Semaphore:
    global _repair_semaphore
    if _repair_semaphore is None:
        _repair_semaphore = asyncio.Semaphore(config.max_concurrent_repairs)
    return _repair_semaphore


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "workflow"


def _attempt_number(pr: dict) -> int:
    for label in pr.get("labels", []):
        match = re.fullmatch(r"autofix-attempt-(\d+)", label["name"])
        if match:
            return int(match.group(1))
    return 0


def _labels_with(pr: dict, new_label: str) -> list[str]:
    kept = [
        label["name"] for label in pr.get("labels", [])
        if not re.fullmatch(r"autofix-attempt-\d+|autofix-exhausted", label["name"])
    ]
    return kept + [new_label]


async def invoke_claude(directory: Path, task: str, config: Settings) -> str:
    prompt = f"{SYSTEM_RULES}\n\nTask:\n{task}"
    logger.info("Invoking Claude Code in %s (timeout=%ds)", directory, config.claude_timeout_seconds)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        config.claude_command, "-p", prompt, "--dangerously-skip-permissions",
        cwd=directory, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=config.claude_timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.communicate()
        logger.warning("Claude Code timed out after %.0fs in %s", time.monotonic() - started, directory)
        return "Claude timed out before completing a fix."
    logger.info("Claude Code finished in %.0fs (exit=%s) in %s", time.monotonic() - started, process.returncode, directory)
    return output.decode(errors="replace")[-12000:]


async def _apply_fix(
    gh: GitHub, source_branch: str, task: str, commit_message: str, config: Settings,
    push_branch: str | None = None, clone_repo: str | None = None,
) -> tuple[bool, str]:
    async with _semaphore(config):
        directory = Path(tempfile.mkdtemp(prefix="pr-autofix-"))
        target_branch = push_branch or source_branch
        try:
            logger.info("Cloning %s (repo=%s) into %s", source_branch, clone_repo or gh.repo, directory)
            try:
                # Runs in a worker thread — it's a blocking subprocess call, and letting it block
                # the asyncio event loop would freeze the whole server (all repos, /healthz, etc.)
                # for as long as git takes, including if it hangs waiting on a bad-token failure.
                await asyncio.to_thread(gh.clone_branch, source_branch, directory, clone_repo)
            except CloneFailed as exc:
                logger.warning("Clone of %s failed: %s", source_branch, exc)
                return False, f"Could not clone branch `{source_branch}`: {exc}"
            if push_branch:
                subprocess.run(["git", "checkout", "-b", push_branch], cwd=directory, check=True)
            result = await invoke_claude(directory, task, config)
            try:
                pushed = await asyncio.to_thread(gh.commit_and_push, directory, target_branch, commit_message)
                logger.info("Push to %s: %s", target_branch, "changes pushed" if pushed else "no changes to push")
            except PushRejected as exc:
                pushed = False
                logger.warning("Push to %s rejected: %s", target_branch, exc)
                result += (
                    f"\n\n(A fix was computed but could not be pushed: {exc}\n"
                    "This usually means the branch is on a fork without \"Allow edits from maintainers\" enabled.)"
                )
            return pushed, result
        finally:
            shutil.rmtree(directory, ignore_errors=True)


async def repair_pr(
    repo: str, number: int, branch: str, title: str, files: list[dict], config: Settings,
    head_repo: str | None = None,
) -> None:
    gh = GitHub(config.github_token, repo)
    changed = "\n".join(f"- {item['filename']} ({item['status']})" for item in files[:100])
    task = f"Review pull request #{number}: {title}\nChanged files:\n{changed}\nFind and safely fix actual defects in this PR."
    await gh.comment(number, "🔍 **PR AutoFix Agent**\n\nScanning this PR for issues...")
    async with lock_for(f"{repo}:{branch}"):
        pushed, result = await _apply_fix(
            gh, branch, task, f"fix: address automated review for PR #{number}", config, clone_repo=head_repo
        )
    await gh.comment(number, f"🤖 **PR AutoFix Agent**\n\n{_format_report(pushed, result)}")


async def create_autofix_pr(gh: GitHub, branch_name: str, run: dict, config: Settings) -> None:
    base_branch = run["head_branch"]
    logger.info("Creating autofix PR for %s on %s (branch=%s)", run["name"], gh.repo, branch_name)
    logs = await gh.workflow_logs(run["id"])
    task = f"A post-merge workflow failed: {run['name']} (run {run['id']}).\nFailed logs:\n{logs}\nDiagnose and safely fix the root cause."
    pushed, result = await _apply_fix(
        gh, base_branch, task, f"fix(ci): repair failed workflow {run['name']}", config, push_branch=branch_name
    )
    if not pushed:
        logger.info("Autofix for %s: no safe fix found, not creating a PR", run["name"])
        return
    pr = await gh.create_pr(
        branch_name, base_branch, f"fix(ci): repair {run['name']}",
        f"Automated repair for failed workflow.\n\n{_format_report(pushed, result)}",
    )
    await gh.set_labels(pr["number"], ["autofix-attempt-1"])
    await gh.comment(
        pr["number"],
        f"🤖 This PR was opened automatically after a post-merge CI failure (attempt 1/{config.max_autofix_attempts}).",
    )
    logger.info("Autofix PR opened for %s: %s#%d", run["name"], gh.repo, pr["number"])


async def retry_autofix(gh: GitHub, pr: dict, branch_name: str, run: dict, config: Settings) -> None:
    attempt = _attempt_number(pr)
    if attempt >= config.max_autofix_attempts:
        logger.info("Autofix %s#%d: attempt cap (%d) reached, not retrying", gh.repo, pr["number"], config.max_autofix_attempts)
        if not any(label["name"] == "autofix-exhausted" for label in pr.get("labels", [])):
            await gh.set_labels(pr["number"], _labels_with(pr, "autofix-exhausted"))
            await gh.comment(
                pr["number"],
                f"🤖 **PR AutoFix Agent**\n\nStill failing after {attempt}/{config.max_autofix_attempts} "
                "automatic fix attempts. Stopping here — this needs a human to take a look.",
            )
        return

    next_attempt = attempt + 1
    logger.info("Autofix %s#%d: starting attempt %d/%d", gh.repo, pr["number"], next_attempt, config.max_autofix_attempts)
    await gh.comment(
        pr["number"],
        f"🔍 **PR AutoFix Agent**\n\nPipeline still failing — scanning logs for attempt "
        f"{next_attempt}/{config.max_autofix_attempts}...",
    )
    logs = await gh.workflow_logs(run["id"])
    task = (
        f"The workflow {run['name']} (run {run['id']}) is still failing after a previous automated fix "
        f"attempt on this branch (attempt {next_attempt} of {config.max_autofix_attempts}).\n"
        f"Failed logs:\n{logs}\nDiagnose and safely fix the root cause."
    )
    pushed, result = await _apply_fix(gh, branch_name, task, f"fix(ci): retry {next_attempt} for {run['name']}", config)
    await gh.set_labels(pr["number"], _labels_with(pr, f"autofix-attempt-{next_attempt}"))
    await gh.comment(
        pr["number"],
        f"🤖 **PR AutoFix Agent** (attempt {next_attempt}/{config.max_autofix_attempts})\n\n{_format_report(pushed, result)}",
    )
