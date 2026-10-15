"""`GitHubIssueSource`: a repo's issues over the GitHub GraphQL API."""

import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from cofabriq.issues.model import Closer, Comment, Issue, IssueRef, check_repo

API = "https://api.github.com"
PAGE_SIZE = 50
MAX_RETRIES = 5

# The fields cofabriq keeps per issue: its first N comments, duplicate marks
# and what closed it last (a merged pull request, a commit, or a person).
FIELDS = """
fragment IssueFields on Issue {
  number title body state stateReason createdAt updatedAt closedAt
  author { __typename login }
  labels(first: 20) { nodes { name } }
  comments(first: $comments) {
    totalCount
    nodes { author { __typename login } body createdAt }
  }
  timelineItems(first: 20,
                itemTypes: [MARKED_AS_DUPLICATE_EVENT,
                            UNMARKED_AS_DUPLICATE_EVENT]) {
    nodes {
      __typename
      ... on MarkedAsDuplicateEvent {
        canonical { ...Target } duplicate { ...Target }
      }
      ... on UnmarkedAsDuplicateEvent {
        canonical { ...Target } duplicate { ...Target }
      }
    }
  }
  closed: timelineItems(last: 1, itemTypes: [CLOSED_EVENT]) {
    nodes { ... on ClosedEvent { closer { __typename } } }
  }
}

fragment Target on IssueOrPullRequest {
  ... on Issue { number repository { nameWithOwner } }
  ... on PullRequest { number repository { nameWithOwner } }
}
"""

# One request per page of issues. REST would need two more calls per issue
# (comments, timeline), about 14k calls for open-webui against a budget of
# 5000 per hour.
QUERY = (
    """
query($owner: String!, $name: String!, $since: DateTime, $after: String,
      $pageSize: Int!, $comments: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issues(first: $pageSize, after: $after, filterBy: {since: $since},
           orderBy: {field: UPDATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { ...IssueFields }
    }
  }
}
"""
    + FIELDS
)

# One issue by number (`triage` refreshing the issue it is asked about).
ISSUE_QUERY = (
    """
query($owner: String!, $name: String!, $number: Int!, $comments: Int!) {
  rateLimit { cost remaining resetAt }
  repository(owner: $owner, name: $name) {
    issue(number: $number) { ...IssueFields }
  }
}
"""
    + FIELDS
)

# Issues created in a date range: the GraphQL `issues` connection filters by
# update time only, search filters by creation. Search stops at 1000 results
# per query, so `created` cuts the range into slices below that.
SEARCH_QUERY = (
    """
query($q: String!, $after: String, $pageSize: Int!, $comments: Int!) {
  rateLimit { cost remaining resetAt }
  search(type: ISSUE, query: $q, first: $pageSize, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on Issue { ...IssueFields } }
  }
}
"""
    + FIELDS
)

COUNT_QUERY = """
query($q: String!) { search(type: ISSUE, query: $q, first: 0) { issueCount } }
"""

SEARCH_CAP = 1000
CLOSERS: dict[str, Closer] = {"PullRequest": "pull_request", "Commit": "commit"}

type Json = dict[str, Any]


class GitHubError(RuntimeError):
    pass


class GitHubIssueSource:
    """Issues of one repo, read-only. Needs a token (GraphQL has no anonymous
    access); a fine-grained token with public read-only access is enough.

    Rate limits: the query reports its cost and the remaining budget, so the
    source waits for the reset before a page it can't afford. Secondary limits,
    gateway errors (403/429 with `retry-after`, 502-504) and transport errors
    (a search page that times out) are retried with backoff.
    """

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        comments: int = 10,
        page_size: int = PAGE_SIZE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repo = check_repo(repo)
        self._comments = comments
        self._page_size = page_size
        self._sleep = sleep
        self._now = now
        self._client = httpx.Client(
            base_url=API,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            transport=transport,
        )

    @property
    def repo(self) -> str:
        return self._repo

    def issues(self, since: datetime | None = None) -> Iterator[Issue]:
        owner, name = self._repo.split("/")
        variables: Json = {
            "owner": owner,
            "name": name,
            "since": since.isoformat() if since else None,
            "after": None,
            "pageSize": self._page_size,
            "comments": self._comments,
        }
        while True:
            data = self._query(QUERY, variables)
            if data["repository"] is None:
                raise GitHubError(f"repository not found: {self._repo}")
            page = data["repository"]["issues"]
            for node in page["nodes"]:
                yield _issue(self._repo, node)
            if not page["pageInfo"]["hasNextPage"]:
                return
            variables["after"] = page["pageInfo"]["endCursor"]
            self._wait_for_budget(data["rateLimit"])

    def created(self, start: date, end: date) -> Iterator[Issue]:
        """Issues created from `start` to `end` (both included, UTC days),
        oldest first."""
        for first, last in self._slices(start, end):
            variables: Json = {
                "q": f"{self._search(first, last)} sort:created-asc",
                "after": None,
                "pageSize": self._page_size,
                "comments": self._comments,
            }
            while True:
                data = self._query(SEARCH_QUERY, variables)
                page = data["search"]
                for node in page["nodes"]:
                    if node:  # a pull request matches `... on Issue` as {}
                        yield _issue(self._repo, node)
                if not page["pageInfo"]["hasNextPage"]:
                    break
                variables["after"] = page["pageInfo"]["endCursor"]
                self._wait_for_budget(data["rateLimit"])

    def _search(self, first: date, last: date) -> str:
        return f"repo:{self._repo} is:issue created:{first}..{last}"

    def _slices(self, start: date, end: date) -> Iterator[tuple[date, date]]:
        """Day ranges in order, each under the search cap; halved until it fits."""
        todo = [(start, end)]
        while todo:
            first, last = todo.pop()
            count = self._query(COUNT_QUERY, {"q": self._search(first, last)})
            if count["search"]["issueCount"] <= SEARCH_CAP:
                yield first, last
                continue
            if first == last:
                raise GitHubError(f"more than {SEARCH_CAP} issues created on {first}")
            middle = first + timedelta(days=(last - first).days // 2)
            todo += [(middle + timedelta(days=1), last), (first, middle)]

    def issue(self, number: int) -> Issue | None:
        """One issue by number; None if it doesn't exist, is a pull request or
        isn't visible to the token."""
        owner, name = self._repo.split("/")
        variables: Json = {
            "owner": owner,
            "name": name,
            "number": number,
            "comments": self._comments,
        }
        try:
            data = self._query(ISSUE_QUERY, variables)
        except GitHubError as exc:
            # GraphQL answers an unknown number with a NOT_FOUND error.
            if "Could not resolve to an Issue" in str(exc):
                return None
            raise
        if data["repository"] is None:
            raise GitHubError(f"repository not found: {self._repo}")
        node = data["repository"]["issue"]
        return _issue(self._repo, node) if node else None

    def _query(self, query: str, variables: Json) -> Json:
        for attempt in range(MAX_RETRIES):
            try:
                resp = self._client.post(
                    "/graphql", json={"query": query, "variables": variables}
                )
            except httpx.TransportError:  # timeout, dropped connection
                self._sleep(_backoff(attempt))
                continue
            wait = self._retry_after(resp, attempt)
            if wait is not None:
                self._sleep(wait)
                continue
            resp.raise_for_status()
            body: Json = resp.json()
            if errors := body.get("errors"):
                if any(e.get("type") == "RATE_LIMITED" for e in errors):
                    self._sleep(self._until_reset(resp, attempt))
                    continue
                raise GitHubError("; ".join(e.get("message", "?") for e in errors))
            return body["data"]
        raise GitHubError(f"gave up after {MAX_RETRIES} attempts")

    def _retry_after(self, resp: httpx.Response, attempt: int) -> float | None:
        """Seconds to wait before retrying, or None if the response is final."""
        if resp.status_code in {502, 503, 504}:
            return _backoff(attempt)
        if resp.status_code in {403, 429} and (
            "retry-after" in resp.headers
            or resp.headers.get("x-ratelimit-remaining") == "0"
        ):
            return self._until_reset(resp, attempt)
        return None

    def _until_reset(self, resp: httpx.Response, attempt: int) -> float:
        if "retry-after" in resp.headers:
            return float(resp.headers["retry-after"])
        if "x-ratelimit-reset" in resp.headers:
            reset = datetime.fromtimestamp(int(resp.headers["x-ratelimit-reset"]), UTC)
            return max((reset - self._now()).total_seconds(), 0) + 1
        return _backoff(attempt)

    def _wait_for_budget(self, rate: Json) -> None:
        if rate["remaining"] >= rate["cost"]:
            return
        reset = datetime.fromisoformat(rate["resetAt"])
        self._sleep(max((reset - self._now()).total_seconds(), 0) + 1)


def _backoff(attempt: int) -> float:
    return 5.0 * 2**attempt


def _login(author: Json | None) -> str | None:
    return author["login"] if author else None


def _ref(target: Json | None) -> IssueRef | None:
    """None when the issue was deleted or isn't visible to the token."""
    if not target:
        return None
    repo = target["repository"]["nameWithOwner"].lower()
    return IssueRef(repo=repo, number=target["number"])


def _issue(repo: str, node: Json) -> Issue:
    # GitHub shows a duplicate mark on the canonical's timeline (observed on
    # open-webui: not on the duplicate's), so both directions are kept.
    me = IssueRef(repo=repo, number=node["number"])
    pairs: dict[tuple[IssueRef, IssueRef], None] = {}  # (duplicate, canonical)
    for event in node["timelineItems"]["nodes"]:
        duplicate = _ref(event.get("duplicate"))
        canonical = _ref(event.get("canonical"))
        if duplicate is None or canonical is None:
            continue
        if event["__typename"] == "MarkedAsDuplicateEvent":
            pairs[(duplicate, canonical)] = None
        else:
            pairs.pop((duplicate, canonical), None)
    reason = node["stateReason"]
    closed = node["closed"]["nodes"]
    closer = closed[0]["closer"] if closed else None
    return Issue(
        repo=repo,
        number=node["number"],
        title=node["title"],
        body=node["body"],
        state=node["state"].lower(),
        state_reason=reason.lower() if reason else None,
        author=_login(node["author"]),
        labels=[label["name"] for label in node["labels"]["nodes"]],
        created_at=node["createdAt"],
        updated_at=node["updatedAt"],
        closed_at=node["closedAt"],
        closed_by=CLOSERS.get(closer["__typename"]) if closer else None,
        comment_count=node["comments"]["totalCount"],
        comments=[
            Comment(
                author=_login(c["author"]),
                bot=bool(c["author"]) and c["author"]["__typename"] == "Bot",
                body=c["body"],
                created_at=c["createdAt"],
            )
            for c in node["comments"]["nodes"]
        ],
        duplicate_of=[c for d, c in pairs if d == me],
        duplicates=[d for d, c in pairs if c == me],
    )
