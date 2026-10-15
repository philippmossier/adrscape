"""GitHubIssueSource on recorded responses, and the issue store.

`fixtures/issues_page{1,2}.json` are two real pages of `QUERY` on
open-webui/open-webui (since 2026-08-31T09:25:00Z, pageSize 2, comments 2),
recorded 2026-10-13 with `gh api graphql`. Edited: bodies cut to 300
characters, `hasNextPage` on page 2 set to false.
"""

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cofabriq.issues.fetch import fetch_issues, fetch_one, load_index, load_issues
from cofabriq.issues.github import GitHubError, GitHubIssueSource
from cofabriq.issues.model import Issue, IssueRef, check_repo

FIXTURES = Path(__file__).parent / "fixtures"
REPO = "open-webui/open-webui"
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)

type Handler = Callable[[httpx.Request], httpx.Response]


def page(n: int) -> dict[str, Any]:
    return json.loads((FIXTURES / f"issues_page{n}.json").read_text())


def replay(*responses: httpx.Response) -> tuple[Handler, list[dict[str, Any]]]:
    """A handler answering with `responses` in order; records request bodies."""
    queue = list(responses)
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return queue.pop(0)

    return handle, sent


def source(handler: Handler, sleeps: list[float]) -> GitHubIssueSource:
    return GitHubIssueSource(
        REPO,
        "token",
        comments=2,
        page_size=2,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        now=lambda: NOW,
    )


def test_pages_through_and_parses_issues() -> None:
    handler, sent = replay(
        httpx.Response(200, json=page(1)), httpx.Response(200, json=page(2))
    )

    issues = list(source(handler, []).issues())

    assert [i.number for i in issues] == [29290, 29292, 29291, 21315]
    assert sent[0]["variables"]["after"] is None
    assert (
        sent[1]["variables"]["after"]
        == page(1)["data"]["repository"]["issues"]["pageInfo"]["endCursor"]
    )
    first = issues[0]
    assert (first.state, first.state_reason) == ("closed", "duplicate")
    assert first.comment_count == 2
    assert [(c.author, c.bot) for c in first.comments] == [
        ("owui-terminator", True),
        ("Classic298", False),
    ]


def test_duplicate_mark_is_kept_in_both_directions() -> None:
    handler, _ = replay(
        httpx.Response(200, json=page(1)), httpx.Response(200, json=page(2))
    )

    issues = {i.number: i for i in source(handler, []).issues()}

    # the event sits on the canonical's timeline only (29290)
    assert issues[29290].duplicates == [IssueRef(repo=REPO, number=29291)]
    assert issues[29290].duplicate_of == []
    assert issues[29291].duplicate_of == []


def test_unmark_cancels_a_mark() -> None:
    body = page(2)
    node = body["data"]["repository"]["issues"]["nodes"][0]
    # mixed case on purpose: GitHub names are case-insensitive
    target = {"number": 29290, "repository": {"nameWithOwner": "Open-WebUI/open-webui"}}
    me = {"number": node["number"], "repository": {"nameWithOwner": REPO}}
    event = {"canonical": target, "duplicate": me}
    node["timelineItems"]["nodes"] = [
        {"__typename": "MarkedAsDuplicateEvent", **event},
        {"__typename": "UnmarkedAsDuplicateEvent", **event},
        {"__typename": "MarkedAsDuplicateEvent", **event},
    ]
    handler, _ = replay(httpx.Response(200, json=body))

    [issue, _] = source(handler, []).issues()

    assert issue.duplicate_of == [IssueRef(repo=REPO, number=29290)]


def test_waits_for_reset_when_budget_is_spent() -> None:
    first = page(1)
    first["data"]["rateLimit"] = {
        "cost": 1,
        "remaining": 0,
        "resetAt": "2026-10-13T10:05:00Z",
    }
    handler, _ = replay(
        httpx.Response(200, json=first), httpx.Response(200, json=page(2))
    )
    sleeps: list[float] = []

    list(source(handler, sleeps).issues())

    assert sleeps == [301]


def test_retries_secondary_rate_limit_and_gateway_errors() -> None:
    handler, sent = replay(
        httpx.Response(403, headers={"retry-after": "60"}),
        httpx.Response(502),
        httpx.Response(200, json=page(2)),
    )
    sleeps: list[float] = []

    issues = list(source(handler, sleeps).issues())

    assert len(issues) == 2
    assert sleeps == [60, 10]
    assert len(sent) == 3


def test_plain_403_and_graphql_errors_raise() -> None:
    handler, _ = replay(httpx.Response(403, json={"message": "Bad credentials"}))
    with pytest.raises(httpx.HTTPStatusError):
        list(source(handler, []).issues())

    errors = {"errors": [{"type": "NOT_FOUND", "message": "no such repo"}]}
    handler, _ = replay(httpx.Response(200, json=errors))
    with pytest.raises(GitHubError, match="no such repo"):
        list(source(handler, []).issues())


@pytest.mark.parametrize("bad", ["../x", "owner", "a/b/c", "owner/..", "-x/y"])
def test_repo_name_is_checked(bad: str) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        check_repo(bad)


class ListSource:
    """IssueSource over a fixed list; records the `since` it was asked for."""

    def __init__(self, issues: list[Issue]) -> None:
        self.repo = REPO
        self._issues = issues
        self.asked: list[datetime | None] = []

    def issues(self, since: datetime | None = None) -> Iterator[Issue]:
        self.asked.append(since)
        return iter(i for i in self._issues if since is None or i.updated_at >= since)

    def issue(self, number: int) -> Issue | None:
        return next((i for i in self._issues if i.number == number), None)


def recorded_issues() -> list[Issue]:
    handler, _ = replay(
        httpx.Response(200, json=page(1)), httpx.Response(200, json=page(2))
    )
    return list(source(handler, []).issues())


def test_fetch_writes_issues_and_index(tmp_path: Path) -> None:
    issues = recorded_issues()

    report = fetch_issues(ListSource(issues), tmp_path)

    assert (report.written, report.unchanged, report.since) == (4, 0, None)
    repo_dir = tmp_path / "open-webui" / "open-webui"
    assert sorted(load_index(repo_dir)) == [21315, 29290, 29291, 29292]
    assert load_issues(tmp_path, REPO) == sorted(issues, key=lambda i: i.number)


def test_second_fetch_asks_since_newest_and_skips_unchanged(tmp_path: Path) -> None:
    issues = recorded_issues()
    fetch_issues(ListSource(issues), tmp_path)
    newest = max(i.updated_at for i in issues)
    edited = issues[0].model_copy(
        update={"title": "edited", "updated_at": datetime(2026, 9, 1, tzinfo=UTC)}
    )

    again = ListSource([*issues[1:], edited])
    report = fetch_issues(again, tmp_path)

    assert again.asked == [newest]
    assert (report.written, report.unchanged) == (1, 1)
    [stored] = [i for i in load_issues(tmp_path, REPO) if i.number == edited.number]
    assert stored.title == "edited"


def one(node: dict[str, Any] | None) -> httpx.Response:
    """A recorded page node as the answer to `ISSUE_QUERY`."""
    data = {"rateLimit": page(1)["data"]["rateLimit"], "repository": {"issue": node}}
    return httpx.Response(200, json={"data": data})


def test_fetches_one_issue_by_number() -> None:
    node = page(1)["data"]["repository"]["issues"]["nodes"][0]
    handler, sent = replay(one(node), one(None))
    src = source(handler, [])

    found = src.issue(node["number"])
    missing = src.issue(1)  # a pull request or a hidden issue: null

    assert found is not None and found.number == node["number"]
    assert found == recorded_issues()[0]
    assert missing is None
    assert sent[0]["variables"]["number"] == node["number"]
    assert "issue(number: $number)" in sent[0]["query"]


def test_unknown_issue_number_is_none() -> None:
    message = "Could not resolve to an Issue with the number of 99999."
    error = {"type": "NOT_FOUND", "message": message}
    handler, _ = replay(httpx.Response(200, json={"data": None, "errors": [error]}))

    assert source(handler, []).issue(99999) is None


def test_fetch_one_stores_a_new_issue_and_keeps_the_cursor(tmp_path: Path) -> None:
    issues = sorted(recorded_issues(), key=lambda i: i.updated_at)
    *older, newest = issues
    fetch_issues(ListSource(older), tmp_path)
    cursor = max(i.updated_at for i in older)

    stored = fetch_one(ListSource(issues), tmp_path, newest.number)

    assert stored == newest
    assert newest.number in load_index(tmp_path / "open-webui" / "open-webui")
    again = ListSource(issues)
    fetch_issues(again, tmp_path)
    assert again.asked == [cursor]  # not the single issue's newer date


def test_fetch_one_on_a_store_without_cursor_keeps_the_old_resume_point(
    tmp_path: Path,
) -> None:
    issues = sorted(recorded_issues(), key=lambda i: i.updated_at)
    *older, newest = issues
    fetch_issues(ListSource(older), tmp_path)
    repo_dir = tmp_path / "open-webui" / "open-webui"
    (repo_dir / "cursor.json").unlink()  # a store from before the cursor

    fetch_one(ListSource(issues), tmp_path, newest.number)
    again = ListSource(issues)
    fetch_issues(again, tmp_path)

    assert again.asked == [max(i.updated_at for i in older)]


def test_fetch_one_returns_none_for_an_unknown_issue(tmp_path: Path) -> None:
    assert fetch_one(ListSource([]), tmp_path, 7) is None
    assert not (tmp_path / "open-webui").exists()
