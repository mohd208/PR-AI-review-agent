from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    github_token: str
    github_webhook_secret: str
    allowed_repositories: str
    claude_command: str = "claude"
    claude_timeout_seconds: int = 900
    max_autofix_attempts: int = 3
    bot_login: str = "pr-autofix-agent[bot]"

    @property
    def allowed_repos(self) -> set[str]:
        return {repo.strip().lower() for repo in self.allowed_repositories.split(",") if repo.strip()}


@lru_cache
def settings() -> Settings:
    return Settings()

