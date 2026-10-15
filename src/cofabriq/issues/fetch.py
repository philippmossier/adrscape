"""Copy issues from an IssueSource into `<issues_dir>/<owner>/<name>/`."""

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from cofabriq.issues.model import Issue, IssueSource, check_repo

INDEX_FILE = "index.json"
CURSOR_FILE = "cursor.json"
FLUSH_EVERY = 100
_index_adapter = TypeAdapter(dict[int, datetime])
_cursor_adapter = TypeAdapter[datetime | None](datetime | None)


@dataclass(frozen=True)
class IssueFetchReport:
    written: int
    unchanged: int
    since: datetime | None


def fetch_issues(source: IssueSource, issues_dir: Path) -> IssueFetchReport:
    """Mirror the source's issues to `<number>.json` plus `index.json`.

    Incremental: asks only for issues updated at or after the cursor, the
    newest `updated_at` this stream has seen. Idempotent by (repo, number,
    `updated_at`): the boundary issues come back and are not rewritten. Index
    and cursor are flushed every FLUSH_EVERY issues, so an interrupted run
    (rate limit, Ctrl-C) resumes where it stopped instead of starting over.

    Not handled: deleted or transferred issues stay on disk (a full refetch
    would need a listing of all numbers; nothing needs it yet).
    """
    repo_dir = issue_dir(issues_dir, source.repo)
    index = load_index(repo_dir)
    since = _since(repo_dir, index)
    latest = since
    written = unchanged = 0

    for issue in source.issues(since):
        latest = issue.updated_at if latest is None else max(latest, issue.updated_at)
        if index.get(issue.number) == issue.updated_at:
            unchanged += 1
            continue
        _write_issue(repo_dir, issue)
        index[issue.number] = issue.updated_at
        written += 1
        if written % FLUSH_EVERY == 0:
            _write_index(repo_dir, index)
            _write_cursor(repo_dir, latest)

    if written:
        _write_index(repo_dir, index)
    if latest is not None:
        _write_cursor(repo_dir, latest)
    return IssueFetchReport(written=written, unchanged=unchanged, since=since)


def fetch_one(source: IssueSource, issues_dir: Path, number: int) -> Issue | None:
    """Fetch one issue and store it if it is new or changed; None if the
    source doesn't have it.

    Leaves the cursor alone: a single issue updated today must not make the
    next `fetch_issues` skip everything updated between the last full fetch
    and today.
    """
    issue = source.issue(number)
    if issue is None:
        return None
    repo_dir = issue_dir(issues_dir, source.repo)
    index = load_index(repo_dir)
    if index.get(number) != issue.updated_at:
        if not (repo_dir / CURSOR_FILE).is_file():
            _write_cursor(repo_dir, max(index.values(), default=None))
        _write_issue(repo_dir, issue)
        index[number] = issue.updated_at
        _write_index(repo_dir, index)
    return issue


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


def _since(repo_dir: Path, index: dict[int, datetime]) -> datetime | None:
    """The cursor; stores from before it existed fall back to the newest
    `updated_at` in the index, which was the cursor then."""
    cursor = repo_dir / CURSOR_FILE
    if cursor.is_file():
        return _cursor_adapter.validate_json(cursor.read_bytes())
    return max(index.values(), default=None)


def _write_issue(repo_dir: Path, issue: Issue) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    (repo_dir / f"{issue.number}.json").write_text(
        issue.model_dump_json(indent=2), encoding="utf-8"
    )


def _write_cursor(repo_dir: Path, since: datetime | None) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    tmp = repo_dir / f"{CURSOR_FILE}.tmp"
    tmp.write_bytes(_cursor_adapter.dump_json(since))
    os.replace(tmp, repo_dir / CURSOR_FILE)


def _write_index(repo_dir: Path, index: dict[int, datetime]) -> None:
    tmp = repo_dir / f"{INDEX_FILE}.tmp"
    tmp.write_bytes(_index_adapter.dump_json(dict(sorted(index.items())), indent=2))
    os.replace(tmp, repo_dir / INDEX_FILE)
