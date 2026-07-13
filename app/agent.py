import asyncio
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import Settings
from .github import GitHub, PushRejected

SYSTEM_RULES = """You are a careful repository repair agent working non-interactively. Work only
on the stated issue and make the smallest correct fix.

Hard rules:
- Never modify credentials, secrets, lockfiles (unless the task is literally a lockfile fix), CI
  permissions, or branch-protection configuration.
- Never disable, skip, or weaken tests or checks to make them pass.
- Never run destructive or "apply" commands: no `terraform apply`, `terraform destroy`,
  `kubectl apply`, `kubectl delete`, cloud CLI mutations, or anything that changes real
  infrastructure or deployed state. You may run read-only/plan/validate/lint commands
  (`terraform validate`, `terraform fmt -check`, `terraform plan`, `kubeval`,
  `kubectl --dry-run=client`, `yamllint`, `actionlint`, etc.).
- Only edit files in the working tree; never touch state files, kubeconfig, or cloud credentials.
- Run the most relevant available validation for the files you changed.

When finished, return a short summary of changed files and validation performed. If no safe fix
exists, do not edit any files and explain why."""

# Serializes repairs per branch so two webhook events for the same branch can't race each
# other's clone/commit/push or double-count a retry attempt.
_branch_locks: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    return _branch_locks.setdefault(key, asyncio.Lock())


def _slug(text: str) -> str:
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
    process = await asyncio.create_subprocess_exec(
        config.claude_command, "-p", prompt, "--dangerously-skip-permissions",
        cwd=directory, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=config.claude_timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.communicate()
        return "Claude timed out before completing a fix."
    return output.decode(errors="replace")[-12000:]


async def _apply_fix(
    gh: GitHub, source_branch: str, task: str, commit_message: str, config: Settings,
    push_branch: str | None = None, clone_repo: str | None = None,
) -> tuple[bool, str]:
    directory = Path(tempfile.mkdtemp(prefix="pr-autofix-"))
    target_branch = push_branch or source_branch
    try:
        gh.clone_branch(source_branch, directory, repo=clone_repo)
        if push_branch:
            subprocess.run(["git", "checkout", "-b", push_branch], cwd=directory, check=True)
        result = await invoke_claude(directory, task, config)
        try:
            pushed = gh.commit_and_push(directory, target_branch, commit_message)
        except PushRejected as exc:
            pushed = False
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
    async with _lock_for(f"{repo}:{branch}"):
        pushed, result = await _apply_fix(
            gh, branch, task, f"fix: address automated review for PR #{number}", config, clone_repo=head_repo
        )
    status = "I pushed a focused repair commit." if pushed else "I found no safe code change to push."
    await gh.comment(number, f"🤖 **PR AutoFix Agent**\n\n{status}\n\nClaude report:\n```text\n{result}\n```")


async def handle_workflow_failure(repo: str, run: dict, config: Settings) -> None:
    gh = GitHub(config.github_token, repo)
    branch_name = f"autofix/ci-{_slug(run['name'])}"
    async with _lock_for(f"{repo}:{branch_name}"):
        existing_pr = await gh.find_pr_by_branch(branch_name)
        if existing_pr:
            await _retry_autofix(gh, existing_pr, branch_name, run, config)
        elif run.get("event") == "push":
            await _create_autofix_pr(gh, branch_name, run, config)


async def _create_autofix_pr(gh: GitHub, branch_name: str, run: dict, config: Settings) -> None:
    base_branch = run["head_branch"]
    logs = await gh.workflow_logs(run["id"])
    task = f"A post-merge workflow failed: {run['name']} (run {run['id']}).\nFailed logs:\n{logs}\nDiagnose and safely fix the root cause."
    pushed, result = await _apply_fix(
        gh, base_branch, task, f"fix(ci): repair failed workflow {run['name']}", config, push_branch=branch_name
    )
    if not pushed:
        return
    pr = await gh.create_pr(
        branch_name, base_branch, f"fix(ci): repair {run['name']}",
        f"Automated repair for failed workflow.\n\nClaude report:\n```text\n{result}\n```",
    )
    await gh.set_labels(pr["number"], ["autofix-attempt-1"])
    await gh.comment(
        pr["number"],
        f"🤖 This PR was opened automatically after a post-merge CI failure (attempt 1/{config.max_autofix_attempts}).",
    )


async def _retry_autofix(gh: GitHub, pr: dict, branch_name: str, run: dict, config: Settings) -> None:
    attempt = _attempt_number(pr)
    if attempt >= config.max_autofix_attempts:
        if not any(label["name"] == "autofix-exhausted" for label in pr.get("labels", [])):
            await gh.set_labels(pr["number"], _labels_with(pr, "autofix-exhausted"))
            await gh.comment(
                pr["number"],
                f"🤖 **PR AutoFix Agent**\n\nStill failing after {attempt}/{config.max_autofix_attempts} "
                "automatic fix attempts. Stopping here — this needs a human to take a look.",
            )
        return

    next_attempt = attempt + 1
    logs = await gh.workflow_logs(run["id"])
    task = (
        f"The workflow {run['name']} (run {run['id']}) is still failing after a previous automated fix "
        f"attempt on this branch (attempt {next_attempt} of {config.max_autofix_attempts}).\n"
        f"Failed logs:\n{logs}\nDiagnose and safely fix the root cause."
    )
    pushed, result = await _apply_fix(gh, branch_name, task, f"fix(ci): retry {next_attempt} for {run['name']}", config)
    await gh.set_labels(pr["number"], _labels_with(pr, f"autofix-attempt-{next_attempt}"))
    status = (
        f"Pushed fix attempt {next_attempt}/{config.max_autofix_attempts}." if pushed
        else f"Found no safe change to push (attempt {next_attempt}/{config.max_autofix_attempts})."
    )
    await gh.comment(pr["number"], f"🤖 **PR AutoFix Agent**\n\n{status}\n\nClaude report:\n```text\n{result}\n```")
