"""Publish a green change: one branch and one draft PR (`run --apply`).

The only place cofabriq writes to GitHub, and only to `WRITABLE`. What it
writes, and nothing else:

    a commit with the run's diff on top of the spec's commit
      -> pushed to a new branch `cofabriq/<n>-<slug>-<sha>`
         (refused if the branch exists: nothing is overwritten)
      -> a draft PR from it to the default branch, the rendered body

Nothing is merged, commented, labelled or pushed to an existing branch; the
dev reviews and merges. The one other write: a new body for a PR cofabriq
opened itself (`GitHubPulls.update_body`, when the body format changes).

The commit is built from the stored diff in a throwaway index, so the
clone's checkout, its index and the run's worktree stay as they are (git
only gains objects). The push authenticates with the write token as an
HTTP header, with credential helpers switched off: if the token is
refused, git fails instead of falling back to the caller's own login.
"""

import base64
import re
import subprocess
import tempfile
from pathlib import Path

import httpx
from pydantic import BaseModel

from cofabriq.loop.refine import git_env

# the repos `--apply` may write to: the demo playground only, through 0.2
WRITABLE = frozenset({"philippmossier/local-ai-chat-playground"})

API = "https://api.github.com"


class PublishError(RuntimeError):
    pass


class Published(BaseModel):
    branch: str
    sha: str
    number: int
    url: str


def check_writable(repo: str) -> None:
    if repo not in WRITABLE:
        raise PublishError(f"{repo} is not writable (only {', '.join(WRITABLE)})")


def branch_name(number: int, title: str, sha: str) -> str:
    """`cofabriq/51-title-cut-too-early-1a2b3c4`: the issue title (untrusted)
    reduced to [a-z0-9-]."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return f"cofabriq/{number}-{slug or 'fix'}-{sha[:7]}"


def _git(clone: Path, *args: str, env: dict[str, str], stdin: str = "") -> str:
    done = subprocess.run(
        ["git", "-C", str(clone), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if done.returncode != 0:
        raise PublishError(f"git {args[0]}: {done.stderr.strip()[:500]}")
    return done.stdout.strip()


def commit_diff(clone: Path, base: str, diff: str, message: str) -> str:
    """A commit of `diff` applied to `base`, built in a throwaway index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = git_env() | {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(clone, "read-tree", base, env=env)
        _git(clone, "apply", "--cached", "-", env=env, stdin=diff)
        tree = _git(clone, "write-tree", env=env)
        return _git(clone, "commit-tree", tree, "-p", base, "-m", message, env=env)


def push_env(token: str) -> dict[str, str]:
    """git's environment for the push: the token as a header (not in argv,
    not in a URL), no credential helper, no prompt."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    config = {
        "credential.helper": "",  # an empty value clears the helper list
        "http.https://github.com/.extraheader": f"AUTHORIZATION: basic {basic}",
    }
    env = git_env() | {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": str(len(config))}
    for i, (key, value) in enumerate(config.items()):
        env |= {f"GIT_CONFIG_KEY_{i}": key, f"GIT_CONFIG_VALUE_{i}": value}
    return env


def push_new_branch(
    clone: Path, remote: str, sha: str, branch: str, token: str
) -> None:
    """Push `sha` as `branch`; refused if the branch exists (a lease on an
    empty value: the ref must be absent)."""
    ref = f"refs/heads/{branch}"
    lease = f"--force-with-lease={ref}:"
    _git(clone, "push", "-q", lease, remote, f"{sha}:{ref}", env=push_env(token))


class GitHubPulls:
    """The two REST calls `--apply` needs: the default branch, a draft PR."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
            transport=transport,
        )

    def default_branch(self, repo: str) -> str:
        resp = self._client.get(f"/repos/{repo}")
        _raise(resp)
        return str(resp.json()["default_branch"])

    def open_draft(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]:
        payload = {"title": title, "head": head, "base": base, "body": body}
        resp = self._client.post(f"/repos/{repo}/pulls", json=payload | {"draft": True})
        _raise(resp)
        data = resp.json()
        return int(data["number"]), str(data["html_url"])

    def update_body(self, repo: str, number: int, body: str) -> None:
        """A new body for a PR cofabriq opened (the body format changed)."""
        resp = self._client.patch(f"/repos/{repo}/pulls/{number}", json={"body": body})
        _raise(resp)


def _raise(resp: httpx.Response) -> None:
    if resp.is_error:
        raise PublishError(f"GitHub {resp.status_code}: {resp.text[:300]}")


def publish(
    *,
    repo: str,
    clone: Path,
    base_commit: str,
    diff: str,
    number: int,
    issue_title: str,
    title: str,
    body: str,
    token: str,
    pulls: GitHubPulls,
    remote: str | None = None,
) -> Published:
    """The run's diff as a new branch and a draft PR on `repo`. `remote`:
    the push URL (tests push to a local bare repo)."""
    check_writable(repo)
    if not diff:
        raise PublishError("nothing to publish: the diff is empty")
    sha = commit_diff(clone, base_commit, diff, title)
    branch = branch_name(number, issue_title, sha)
    push_new_branch(
        clone, remote or f"https://github.com/{repo}.git", sha, branch, token
    )
    base = pulls.default_branch(repo)
    pr, url = pulls.open_draft(repo, head=branch, base=base, title=title, body=body)
    return Published(branch=branch, sha=sha, number=pr, url=url)
