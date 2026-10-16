"""Typed settings loaded from environment variables and `.env`."""

from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    postgres_dsn: str = "postgresql://cofabriq:cofabriq@localhost:5433/cofabriq"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("cofabriq-dev")
    raw_dir: Path = Path("data/raw")
    extracted_path: Path = Path("data/extracted.json")
    openai_api_key: SecretStr | None = None
    issues_dir: Path = Path("data/issues")
    verdicts_dir: Path = Path("data/verdicts")
    readiness_dir: Path = Path("data/readiness")
    targets_dir: Path = Path("targets")
    target_repo: str | None = None
    github_token: SecretStr | None = None
    issue_comments: int = 10
    anthropic_api_key: SecretStr | None = None  # the coding agent (0.2)
    agent_model: str = "claude-opus-5-5"
    agent_effort: str = "medium"
    clones_dir: Path = Path("data/clones")
    refine_dir: Path = Path("data/refine")


@lru_cache
def get_settings() -> Settings:
    return Settings()
