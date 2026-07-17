import asyncio
import json
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


def _touches_workflows(files: list[dict]) -> bool:
    return any(f["filename"].startswith(".github/workflows/") for f in files)


async def _actions_inventory(gh: GitHub) -> str:
    """What secrets/variables actually exist, so Claude can check workflow references against
    reality instead of just assuming a referenced name is valid."""
    try:
        secrets = await gh.list_secret_names()
        secret_line = ", ".join(sorted(secrets)) if secrets else "(none configured)"
    except Exception as exc:
        secret_line = f"(could not list — {exc})"
    try:
        variables = await gh.list_variables()
        variable_line = ", ".join(f"{v['name']}={v['value']}" for v in variables) if variables else "(none configured)"
    except Exception as exc:
        variable_line = f"(could not list — {exc})"
    return (
        "Repository-level Actions secrets currently configured (names only — GitHub never exposes "
        f"secret values): {secret_line}\n"
        f"Repository-level Actions variables currently configured (name=value): {variable_line}\n"
        "For every `${{ secrets.X }}` or `${{ vars.Y }}` reference in workflow files you touch, "
        "confirm X/Y is in the lists above, is a GitHub-provided default (e.g. "
        "`secrets.GITHUB_TOKEN`), or is an `env`/`github` context value defined elsewhere in the "
        "workflow. If a `jobs.<id>.environment:` is set, also check that environment's own "
        "secrets/variables with `gh api repos/<owner>/<repo>/environments/<env>/secrets` and "
        "`.../variables`. If a reference looks like a typo of a name that does exist, fix the "
        "reference. If a referenced secret or variable genuinely doesn't exist anywhere, do not "
        "invent a value for it — flag it clearly in your report as something a human needs to add "
        "in Settings -> Secrets and variables, without treating that alone as blocking other safe "
        "fixes in the same review."
    )


def _summarize_claude_event(event: dict) -> str | None:
    """Best-effort human-readable summary of one stream-json event for live logging. Returns
    None for events not worth their own log line (the raw line is logged instead in that case).
    The exact tool-call event shape isn't fully documented, so this is deliberately defensive —
    unrecognized shapes just fall through rather than raising."""
    etype = event.get("type")
    if etype == "system":
        subtype = event.get("subtype", "")
        if subtype == "init":
            return f"session started (model={event.get('model', '?')}, {len(event.get('tools', []))} tools available)"
        return f"system: {subtype}" if subtype else None
    if etype in ("assistant", "user"):
        content = event.get("message", {}).get("content", [])
        if isinstance(content, str):
            return content[:300].replace("\n", " ") or None
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "").strip()
                if text:
                    parts.append(text[:300].replace("\n", " "))
            elif btype == "tool_use":
                raw_input = json.dumps(block.get("input", {}))[:200]
                parts.append(f"-> tool_use {block.get('name')}({raw_input})")
            elif btype == "tool_result":
                result_content = block.get("content")
                if isinstance(result_content, list):
                    result_content = " ".join(b.get("text", "") for b in result_content if isinstance(b, dict))
                parts.append(f"<- tool_result: {str(result_content)[:300].replace(chr(10), ' ')}")
        return " | ".join(p for p in parts if p) or None
    if etype == "result":
        return f"final result: {event.get('subtype', 'unknown')}"
    return None


async def invoke_claude(directory: Path, task: str, config: Settings) -> str:
    prompt = f"{SYSTEM_RULES}\n\nTask:\n{task}"
    logger.info("Invoking Claude Code in %s (timeout=%ds)", directory, config.claude_timeout_seconds)
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        config.claude_command, "-p", prompt, "--dangerously-skip-permissions",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        cwd=directory, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    lines: list[str] = []
    final_text: list[str] = []

    async def _pump_stdout() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode(errors="replace").rstrip("\n")
            if not line:
                continue
            lines.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.info("[claude] %s", line[:500])
                continue
            if event.get("type") == "stream_event":
                continue  # token-level deltas — too noisy; the assembled turn events cover this
            summary = _summarize_claude_event(event)
            if summary:
                logger.info("[claude] %s", summary)
            if event.get("type") == "result":
                final_text.append(event.get("result") or event.get("text") or "")

    async def _pump_stderr() -> None:
        assert process.stderr is not None
        async for raw in process.stderr:
            text = raw.decode(errors="replace").rstrip("\n")
            if text:
                logger.warning("[claude:stderr] %s", text[:500])

    try:
        await asyncio.wait_for(
            asyncio.gather(_pump_stdout(), _pump_stderr(), process.wait()),
            timeout=config.claude_timeout_seconds,
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        logger.warning("Claude Code timed out after %.0fs in %s", time.monotonic() - started, directory)
        return "Claude timed out before completing a fix."

    logger.info("Claude Code finished in %.0fs (exit=%s) in %s", time.monotonic() - started, process.returncode, directory)
    if final_text and final_text[-1]:
        return final_text[-1][-12000:]
    # Fallback in case the "result" event wasn't found/parsed as expected (e.g. a Claude Code
    # version whose stream-json shape differs from what's assumed above) — never return nothing.
    return "\n".join(lines)[-12000:]


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
    if _touches_workflows(files):
        task += f"\n\n{await _actions_inventory(gh)}"
    await gh.comment(number, "🔍 **PR AutoFix Agent**\n\nScanning this PR for issues...")
    async with lock_for(f"{repo}:{branch}"):
        pushed, result = await _apply_fix(
            gh, branch, task, f"fix: address automated review for PR #{number}", config, clone_repo=head_repo
        )
    await gh.comment(number, f"🤖 **PR AutoFix Agent**\n\n{_format_report(pushed, result)}")


async def repair_pr_ci_failure(
    repo: str, pr: dict, files: list[dict], config: Settings, head_repo: str | None, ci_failure: dict,
) -> None:
    """Like repair_pr, but for a PR whose own CI check (triggered by opening/updating it) just
    failed — feeds Claude the actual failure logs and caps retries the same way the post-merge
    pipeline-autofix flow does, so a PR that keeps failing CI doesn't get fixed at forever."""
    gh = GitHub(config.github_token, repo)
    number = pr["number"]
    branch = pr["head"]["ref"]

    attempt = _attempt_number(pr)
    if attempt >= config.max_autofix_attempts:
        logger.info("PR CI-fix %s#%d: attempt cap (%d) reached, not retrying", repo, number, config.max_autofix_attempts)
        if not any(label["name"] == "autofix-exhausted" for label in pr.get("labels", [])):
            await gh.set_labels(number, _labels_with(pr, "autofix-exhausted"))
            diagnosis = await _diagnose_exhausted(gh, branch, ci_failure, config, clone_repo=head_repo)
            await gh.comment(
                number,
                f"🤖 **PR AutoFix Agent**\n\n🛑 This PR's pipeline is still failing after "
                f"{attempt}/{config.max_autofix_attempts} automatic fix attempts. Stopping here — "
                f"this needs a human to take a look.\n\n<details>\n<summary>Diagnosis and "
                f"suggestions</summary>\n\n{diagnosis}\n</details>",
            )
        return

    next_attempt = attempt + 1
    logger.info("PR CI-fix %s#%d: starting attempt %d/%d", repo, number, next_attempt, config.max_autofix_attempts)
    changed = "\n".join(f"- {item['filename']} ({item['status']})" for item in files[:100])
    logs = await gh.workflow_logs(ci_failure["id"])
    task = (
        f"Review pull request #{number}: {pr['title']}\nChanged files:\n{changed}\n\n"
        f"This PR's own CI check '{ci_failure['name']}' is failing (run {ci_failure['id']}, "
        f"attempt {next_attempt} of {config.max_autofix_attempts}).\nFailed logs:\n{logs}"
    )
    if _touches_workflows(files):
        task += f"\n\n{await _actions_inventory(gh)}"
    task += "\n\nFind and safely fix this failure, plus any other actual defects in this PR."

    await gh.comment(
        number,
        "🔍 **PR AutoFix Agent**\n\nThis PR's own pipeline failed — scanning logs and code for issues...",
    )
    async with lock_for(f"{repo}:{branch}"):
        pushed, result = await _apply_fix(
            gh, branch, task, f"fix: address CI failure for PR #{number}", config, clone_repo=head_repo
        )
    await gh.set_labels(number, _labels_with(pr, f"autofix-attempt-{next_attempt}"))
    await gh.comment(
        number,
        f"🤖 **PR AutoFix Agent** (attempt {next_attempt}/{config.max_autofix_attempts})\n\n{_format_report(pushed, result)}",
    )


async def create_autofix_pr(gh: GitHub, branch_name: str, run: dict, config: Settings) -> None:
    base_branch = run["head_branch"]
    logger.info("Creating autofix PR for %s on %s (branch=%s)", run["name"], gh.repo, branch_name)
    logs = await gh.workflow_logs(run["id"])
    inventory = await _actions_inventory(gh)
    task = (
        f"A post-merge workflow failed: {run['name']} (run {run['id']}).\nFailed logs:\n{logs}\n\n"
        f"{inventory}\n\nDiagnose and safely fix the root cause."
    )
    pushed, result = await _apply_fix(
        gh, base_branch, task, f"fix(ci): repair failed workflow {run['name']}", config, push_branch=branch_name
    )
    if not pushed:
        # No code fix exists (e.g. an external/infra cause like an AWS permission or quota issue) —
        # surface Claude's diagnosis as a GitHub issue instead of only logging it server-side,
        # since there's no PR to comment on here. Dedupe by a stable title (no run ID in it) so a
        # recurring external failure updates the same issue instead of opening a new one each time.
        title = f"🤖 Pipeline failure needs human attention: {run['name']}"
        report = _format_report(pushed, result)
        run_link = f"\n\n[View run {run['id']}]({run['html_url']})" if run.get("html_url") else ""
        existing = await gh.find_issue_by_title(title, "autofix-needs-human")
        if existing:
            await gh.comment(existing["number"], f"🤖 Still happening (run {run['id']}):\n\n{report}{run_link}")
            logger.info("Autofix for %s: no safe fix, updated existing issue #%d", run["name"], existing["number"])
        else:
            issue = await gh.create_issue(
                title,
                f"Automated diagnosis for a failing pipeline on `{base_branch}` found no safe code "
                f"fix — this needs a human to look at.\n\n{report}{run_link}",
                labels=["autofix-needs-human"],
            )
            logger.info("Autofix for %s: no safe fix, opened issue #%d", run["name"], issue["number"])
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


async def _diagnose_exhausted(
    gh: GitHub, branch_name: str, run: dict, config: Settings, clone_repo: str | None = None,
) -> str:
    """Read-only final pass once the retry cap is hit: no edits, just a diagnosis a human can act on."""
    async with _semaphore(config):
        directory = Path(tempfile.mkdtemp(prefix="pr-autofix-diag-"))
        try:
            await asyncio.to_thread(gh.clone_branch, branch_name, directory, clone_repo)
            logs = await gh.workflow_logs(run["id"])
            inventory = await _actions_inventory(gh)
            task = (
                f"The workflow {run['name']} is still failing after {config.max_autofix_attempts} automated fix "
                "attempts on this branch, and no further automatic attempts will be made. Do NOT make any changes. "
                "Instead, give a clear root-cause diagnosis and concrete, actionable suggestions for a human "
                f"engineer to resolve it.\nFailed logs:\n{logs}\n\n{inventory}"
            )
            return await invoke_claude(directory, task, config)
        except CloneFailed as exc:
            return f"Could not clone branch `{branch_name}` for a final diagnosis: {exc}"
        finally:
            shutil.rmtree(directory, ignore_errors=True)


async def retry_autofix(gh: GitHub, pr: dict, branch_name: str, run: dict, config: Settings) -> None:
    attempt = _attempt_number(pr)
    if attempt >= config.max_autofix_attempts:
        logger.info("Autofix %s#%d: attempt cap (%d) reached, not retrying", gh.repo, pr["number"], config.max_autofix_attempts)
        if not any(label["name"] == "autofix-exhausted" for label in pr.get("labels", [])):
            await gh.set_labels(pr["number"], _labels_with(pr, "autofix-exhausted"))
            diagnosis = await _diagnose_exhausted(gh, branch_name, run, config)
            await gh.comment(
                pr["number"],
                f"🤖 **PR AutoFix Agent**\n\n🛑 Still failing after {attempt}/{config.max_autofix_attempts} "
                "automatic fix attempts. Stopping here — this needs a human to take a look.\n\n"
                f"<details>\n<summary>Diagnosis and suggestions</summary>\n\n{diagnosis}\n</details>",
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
    inventory = await _actions_inventory(gh)
    task = (
        f"The workflow {run['name']} (run {run['id']}) is still failing after a previous automated fix "
        f"attempt on this branch (attempt {next_attempt} of {config.max_autofix_attempts}).\n"
        f"Failed logs:\n{logs}\n\n{inventory}\n\nDiagnose and safely fix the root cause."
    )
    pushed, result = await _apply_fix(gh, branch_name, task, f"fix(ci): retry {next_attempt} for {run['name']}", config)
    await gh.set_labels(pr["number"], _labels_with(pr, f"autofix-attempt-{next_attempt}"))
    await gh.comment(
        pr["number"],
        f"🤖 **PR AutoFix Agent** (attempt {next_attempt}/{config.max_autofix_attempts})\n\n{_format_report(pushed, result)}",
    )
