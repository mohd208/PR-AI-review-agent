import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class State:
    """Tracks what's already been scanned/reacted to, so polling doesn't redo work every cycle.

    Persisted to a JSON file so a restart doesn't cause every open PR to be rescanned at once.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                logger.warning("Could not read state file %s, starting fresh", self.path)
        return {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.data))

    def _repo(self, repo: str) -> dict:
        return self.data.setdefault(repo, {"prs": {}, "ci_runs": {}, "notified_passing": [], "watched_branches": []})

    def get_pr_sha(self, repo: str, number: int) -> str | None:
        return self._repo(repo)["prs"].get(str(number))

    def set_pr_sha(self, repo: str, number: int, sha: str) -> None:
        self._repo(repo)["prs"][str(number)] = sha
        self._save()

    def get_ci_run(self, repo: str, branch: str) -> int | None:
        return self._repo(repo)["ci_runs"].get(branch)

    def set_ci_run(self, repo: str, branch: str, run_id: int) -> None:
        self._repo(repo)["ci_runs"][branch] = run_id
        self._save()

    def is_notified_passing(self, repo: str, branch: str) -> bool:
        return branch in self._repo(repo)["notified_passing"]

    def set_notified_passing(self, repo: str, branch: str) -> None:
        notified = self._repo(repo)["notified_passing"]
        if branch not in notified:
            notified.append(branch)
        self._save()

    def clear_notified_passing(self, repo: str, branch: str) -> None:
        notified = self._repo(repo)["notified_passing"]
        if branch in notified:
            notified.remove(branch)
            self._save()

    def watched_branches(self, repo: str, default_branch: str) -> set[str]:
        """Branches to check post-merge pipeline runs on: the repo's configured default branch,
        plus any branch that's ever been observed as a PR's base (e.g. teams that merge into
        "dev" rather than the GitHub-configured default get watched too, automatically)."""
        branches = self._repo(repo).setdefault("watched_branches", [])
        if default_branch not in branches:
            branches.append(default_branch)
            self._save()
        return set(branches)

    def watch_branch(self, repo: str, branch: str) -> bool:
        """Adds a branch to the watch list. Returns True if it was newly added."""
        branches = self._repo(repo).setdefault("watched_branches", [])
        if branch in branches:
            return False
        branches.append(branch)
        self._save()
        return True
