"""Issues as cofabriq stores them: the `IssueSource` seam and its data shape."""

import re
from collections.abc import Iterator
from datetime import date, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

type Closer = Literal["pull_request", "commit"]

REPO_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*)/[A-Za-z0-9._-]+$")


def check_repo(repo: str) -> str:
    """Lowercased `owner/name` (GitHub names are case-insensitive) or ValueError.

    The result becomes a path under `data/`, so `..` and slashes are refused.
    """
    if not REPO_NAME.match(repo) or repo.split("/")[1] in {".", ".."}:
        raise ValueError(f"not an owner/name repo: {repo!r}")
    return repo.lower()


class Comment(BaseModel):
    model_config = ConfigDict(frozen=True)

    author: str | None  # None: deleted account ("ghost")
    bot: bool
    body: str
    created_at: datetime


class IssueRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    repo: str
    number: int


class Issue(BaseModel):
    """One issue with its first comments. Title, body and comments are untrusted.

    `comments` holds the first N only; `comment_count` is the full number.
    `duplicate_of` / `duplicates` are GitHub's own duplicate marks (net of
    unmark events) seen from this issue's timeline, one list per direction.
    `Duplicate of #N` comments are not parsed here: that is the eval set's job.
    `closed_by` is what closed it last: a merged pull request or a commit that
    references it, None for a person or an open issue (and for stores fetched
    before the field existed).
    """

    model_config = ConfigDict(frozen=True)

    repo: str
    number: int
    title: str
    body: str
    state: str  # open | closed
    state_reason: str | None  # completed | not_planned | duplicate | reopened
    author: str | None
    labels: list[str]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    closed_by: Closer | None = None
    comment_count: int
    comments: list[Comment]
    duplicate_of: list[IssueRef]  # this issue is a duplicate of these
    duplicates: list[IssueRef]  # these were marked as duplicates of this one


class IssueSource(Protocol):
    """Yields a repo's issues updated at or after `since`, oldest update first,
    or one issue by number.

    Ascending order makes every prefix of the stream a valid resume point: the
    last `updated_at` seen is the next `since`.
    """

    @property
    def repo(self) -> str: ...

    def issues(self, since: datetime | None = None) -> Iterator[Issue]: ...

    def issue(self, number: int) -> Issue | None: ...

    def created(self, start: date, end: date) -> Iterator[Issue]:
        """Issues created from `start` to `end` (both included), oldest first."""
        ...
