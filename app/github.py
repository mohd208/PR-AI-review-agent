import logging
import os
import subprocess
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Without this, a bad/expired token can make git fall back to an interactive username/password
# prompt on stdin. With no TTY attached, that hangs silently until the subprocess timeout — instead
# force it to fail immediately with a clear stderr message.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}


class PushRejected(Exception):
    """Raised when a computed fix could not be pushed (e.g. no write access to a fork branch)."""


class CloneFailed(Exception):
    """Raised when cloning a branch fails (bad token/permissions, missing branch, etc.)."""


class GitHub:
    def __init__(self, token: str, repo: str):
        self.repo = repo
        self.headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def request(self, method: str, path: str, **kwargs):
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.request(method, f"https://api.github.com{path}", headers=self.headers, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else None

    async def get_repo(self) -> dict:
        return await self.request("GET", f"/repos/{self.repo}")

    async def list_open_prs(self) -> list[dict]:
        return await self.request("GET", f"/repos/{self.repo}/pulls", params={"state": "open", "per_page": 100})

    async def pr_files(self, number: int) -> list[dict]:
        return await self.request("GET", f"/repos/{self.repo}/pulls/{number}/files", params={"per_page": 100})

    async def latest_run(self, branch: str) -> dict | None:
        results = await self.request(
            "GET", f"/repos/{self.repo}/actions/runs",
            params={"branch": branch, "status": "completed", "per_page": 1},
        )
        runs = results.get("workflow_runs", [])
        return runs[0] if runs else None

    async def list_secret_names(self, environment: str | None = None) -> list[str]:
        # GitHub never exposes secret values via the API — names only, by design.
        path = (
            f"/repos/{self.repo}/environments/{environment}/secrets" if environment
            else f"/repos/{self.repo}/actions/secrets"
        )
        result = await self.request("GET", path, params={"per_page": 100})
        return [s["name"] for s in result.get("secrets", [])]

    async def list_variables(self, environment: str | None = None) -> list[dict]:
        path = (
            f"/repos/{self.repo}/environments/{environment}/variables" if environment
            else f"/repos/{self.repo}/actions/variables"
        )
        result = await self.request("GET", path, params={"per_page": 100})
        return result.get("variables", [])

    async def find_pr_by_branch(self, branch: str) -> dict | None:
        owner = self.repo.split("/")[0]
        results = await self.request(
            "GET", f"/repos/{self.repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}
        )
        return results[0] if results else None

    async def set_labels(self, number: int, labels: list[str]) -> None:
        await self.request("PUT", f"/repos/{self.repo}/issues/{number}/labels", json={"labels": labels})

    async def comment(self, number: int, body: str) -> None:
        await self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})

    async def workflow_logs(self, run_id: int) -> str:
        logger.info("Fetching failed-job logs for run %s (%s)", run_id, self.repo)
        # GitHub redirects this endpoint to a ZIP archive. gh handles the download reliably.
        result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--repo", self.repo, "--log-failed"],
            capture_output=True, text=True, timeout=120, check=False,
            env={**_GIT_ENV, "GH_TOKEN": self.headers["Authorization"].split(" ", 1)[1]},
        )
        if result.returncode != 0:
            logger.warning("gh run view failed for run %s: %s", run_id, result.stderr.strip()[-500:])
        return (result.stdout + "\n" + result.stderr)[-50000:]

    def clone_branch(self, branch: str, destination: Path, repo: str | None = None) -> None:
        token = self.headers["Authorization"].split(" ", 1)[1]
        url = f"https://x-access-token:{token}@github.com/{repo or self.repo}.git"
        clone = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, url, str(destination)],
            capture_output=True, text=True, timeout=180, env=_GIT_ENV,
        )
        if clone.returncode != 0:
            raise CloneFailed(clone.stderr.strip()[-2000:])
        subprocess.run(["git", "config", "user.name", "pr-autofix-agent[bot]"], cwd=destination, check=True)
        subprocess.run(["git", "config", "user.email", "pr-autofix-agent[bot]@users.noreply.github.com"], cwd=destination, check=True)
        logger.info("Clone complete: %s@%s -> %s", repo or self.repo, branch, destination)

    def commit_and_push(self, directory: Path, branch: str, message: str) -> bool:
        subprocess.run(["git", "add", "-A"], cwd=directory, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=directory, check=False).returncode != 0
        if not changed:
            logger.info("No working-tree changes in %s; nothing to commit", directory)
            return False
        subprocess.run(["git", "commit", "-m", message], cwd=directory, check=True, timeout=120)
        logger.info("Committed changes in %s: %s", directory, message)
        push = subprocess.run(
            ["git", "push", "origin", f"HEAD:{branch}"], cwd=directory,
            capture_output=True, text=True, timeout=180, env=_GIT_ENV,
        )
        if push.returncode != 0:
            raise PushRejected(push.stderr.strip()[-2000:])
        logger.info("Pushed to %s", branch)
        return True

    async def create_pr(self, head: str, base: str, title: str, body: str) -> dict:
        return await self.request("POST", f"/repos/{self.repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})
