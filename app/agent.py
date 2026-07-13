import asyncio
import json
import shutil
import tempfile
from pathlib import Path

from .config import Settings
from .github import GitHub

SYSTEM_RULES = """You are a careful repository repair agent. Work only on the stated issue.
Inspect the repository and make the smallest correct fix. Never modify credentials, lockfiles
unless necessary, CI permissions, branch-protection configuration, or disable tests/checks to
make them pass. Do not run destructive commands. Run the most relevant available validation.
When finished, return a short summary of changed files and validation. If no safe fix exists,
do not edit files and explain why."""


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


async def repair_pr(repo: str, number: int, branch: str, title: str, files: list[dict], config: Settings) -> None:
    gh = GitHub(config.github_token, repo)
    changed = "\n".join(f"- {item['filename']} ({item['status']})" for item in files[:100])
    task = f"Review pull request #{number}: {title}\nChanged files:\n{changed}\nFind and safely fix actual defects in this PR."
    await _run_repair(gh, number, branch, task, f"fix: address automated review for PR #{number}", config)


async def repair_failed_run(repo: str, run_id: int, branch: str, base: str, name: str, config: Settings) -> None:
    gh = GitHub(config.github_token, repo)
    logs = await gh.workflow_logs(run_id)
    task = f"A post-merge workflow failed: {name} (run {run_id}).\nFailed logs:\n{logs}\nDiagnose and safely fix the root cause."
    repair_branch = f"autofix/ci-run-{run_id}"
    await _run_repair(gh, None, branch, task, f"fix(ci): repair failed workflow run {run_id}", config, repair_branch, base)


async def _run_repair(gh: GitHub, pr_number: int | None, source_branch: str, task: str, commit_message: str, config: Settings, push_branch: str | None = None, base: str | None = None) -> None:
    directory = Path(tempfile.mkdtemp(prefix="pr-autofix-"))
    target_branch = push_branch or source_branch
    try:
        gh.clone_branch(source_branch, directory)
        if push_branch:
            subprocess = __import__("subprocess")
            subprocess.run(["git", "checkout", "-b", push_branch], cwd=directory, check=True)
        result = await invoke_claude(directory, task, config)
        pushed = gh.commit_and_push(directory, target_branch, commit_message)
        if pr_number:
            status = "I pushed a focused repair commit." if pushed else "I found no safe code change to push."
            await gh.comment(pr_number, f"🤖 **PR AutoFix Agent**\n\n{status}\n\nClaude report:\n```text\n{result}\n```")
        elif pushed:
            pr = await gh.create_pr(target_branch, base or "main", f"fix(ci): repair failed workflow {target_branch.rsplit('-', 1)[-1]}", f"Automated repair for failed workflow.\n\nClaude report:\n```text\n{result}\n```")
            await gh.comment(pr["number"], "🤖 This PR was opened automatically after a post-merge CI failure.")
    finally:
        shutil.rmtree(directory, ignore_errors=True)

