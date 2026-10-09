"""Helpers to build throwaway git repos in tests."""

import subprocess
from pathlib import Path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, text=True
    ).stdout.strip()


def commit_all(repo: Path, message: str = "change") -> None:
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


def init_repo(repo: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
    git(repo, "init", "-q")
    commit_all(repo, "init")
    return repo
