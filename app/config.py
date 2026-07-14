from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: str
    allowed_repositories: str
    claude_command: str = "claude"
    claude_timeout_seconds: int = 900
    max_autofix_attempts: int = 3
    max_concurrent_repairs: int = 3
    poll_interval_seconds: int = 60
    state_file: str = "autofix_state.json"

    @property
    def allowed_repos(self) -> set[str]:
        return {repo.strip().lower() for repo in self.allowed_repositories.split(",") if repo.strip()}


@lru_cache
def settings() -> Settings:
    return Settings()
