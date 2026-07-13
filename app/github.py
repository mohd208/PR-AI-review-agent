import subprocess
from pathlib import Path

import httpx


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

    async def pr_files(self, number: int) -> list[dict]:
        return await self.request("GET", f"/repos/{self.repo}/pulls/{number}/files", params={"per_page": 100})

    async def comment(self, number: int, body: str) -> None:
        await self.request("POST", f"/repos/{self.repo}/issues/{number}/comments", json={"body": body})

    async def workflow_logs(self, run_id: int) -> str:
        # GitHub redirects this endpoint to a ZIP archive. gh handles the download reliably.
        result = subprocess.run(
            ["gh", "run", "view", str(run_id), "--repo", self.repo, "--log-failed"],
            capture_output=True, text=True, timeout=120, check=False,
            env={**__import__("os").environ, "GH_TOKEN": self.headers["Authorization"].split(" ", 1)[1]},
        )
        return (result.stdout + "\n" + result.stderr)[-50000:]

    def clone_branch(self, branch: str, destination: Path) -> None:
        token = self.headers["Authorization"].split(" ", 1)[1]
        url = f"https://x-access-token:{token}@github.com/{self.repo}.git"
        subprocess.run(["git", "clone", "--depth", "1", "--branch", branch, url, str(destination)], check=True, timeout=180)
        subprocess.run(["git", "config", "user.name", "pr-autofix-agent[bot]"], cwd=destination, check=True)
        subprocess.run(["git", "config", "user.email", "pr-autofix-agent[bot]@users.noreply.github.com"], cwd=destination, check=True)

    def commit_and_push(self, directory: Path, branch: str, message: str) -> bool:
        subprocess.run(["git", "add", "-A"], cwd=directory, check=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=directory, check=False).returncode != 0
        if not changed:
            return False
        subprocess.run(["git", "commit", "-m", message], cwd=directory, check=True, timeout=120)
        subprocess.run(["git", "push", "origin", f"HEAD:{branch}"], cwd=directory, check=True, timeout=180)
        return True

    async def create_pr(self, head: str, base: str, title: str, body: str) -> dict:
        return await self.request("POST", f"/repos/{self.repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})

