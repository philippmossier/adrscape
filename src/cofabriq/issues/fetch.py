"""Copy issues from an IssueSource into `<issues_dir>/<owner>/<name>/`."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from cofabriq.issues.model import Issue, IssueSource, check_repo

INDEX_FILE = "index.json"
FLUSH_EVERY = 100
_index_adapter = TypeAdapter(dict[int, datetime])


@dataclass(frozen=True)
class IssueFetchReport:
    written: int
    unchanged: int
    since: datetime | None


def fetch_issues(source: IssueSource, issues_dir: Path) -> IssueFetchReport:
    """Mirror the source's issues to `<number>.json` plus `index.json`.

    Incremental: asks only for issues updated at or after the newest
    `updated_at` in the index. Idempotent by (repo, number, `updated_at`):
    the boundary issues come back and are not rewritten. The index is flushed
    every FLUSH_EVERY issues, so an interrupted run (rate limit, Ctrl-C)
    resumes where it stopped instead of starting over.

    Not handled: deleted or transferred issues stay on disk (a full refetch
    would need a listing of all numbers; nothing needs it yet).
    """
    repo_dir = issue_dir(issues_dir, source.repo)
    index = load_index(repo_dir)
    since = max(index.values(), default=None)
    written = unchanged = 0

    for issue in source.issues(since):
        if index.get(issue.number) == issue.updated_at:
            unchanged += 1
            continue
        repo_dir.mkdir(parents=True, exist_ok=True)
        (repo_dir / f"{issue.number}.json").write_text(
            issue.model_dump_json(indent=2), encoding="utf-8"
        )
        index[issue.number] = issue.updated_at
        written += 1
        if written % FLUSH_EVERY == 0:
            _write_index(repo_dir, index)

    if written:
        _write_index(repo_dir, index)
    return IssueFetchReport(written=written, unchanged=unchanged, since=since)


def issue_dir(issues_dir: Path, repo: str) -> Path:
    owner, name = check_repo(repo).split("/")
    return issues_dir / owner / name


def load_index(repo_dir: Path) -> dict[int, datetime]:
    index = repo_dir / INDEX_FILE
    if not index.is_file():
        return {}
    return _index_adapter.validate_json(index.read_bytes())


def load_issues(issues_dir: Path, repo: str) -> list[Issue]:
    repo_dir = issue_dir(issues_dir, repo)
    return [
        Issue.model_validate_json((repo_dir / f"{n}.json").read_bytes())
        for n in sorted(load_index(repo_dir))
    ]


def load_issue(issues_dir: Path, repo: str, number: int) -> Issue | None:
    path = issue_dir(issues_dir, repo) / f"{number}.json"
    return Issue.model_validate_json(path.read_bytes()) if path.is_file() else None


def _write_index(repo_dir: Path, index: dict[int, datetime]) -> None:
    tmp = repo_dir / f"{INDEX_FILE}.tmp"
    tmp.write_bytes(_index_adapter.dump_json(dict(sorted(index.items())), indent=2))
    os.replace(tmp, repo_dir / INDEX_FILE)
