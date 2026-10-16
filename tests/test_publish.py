import json
from pathlib import Path

import httpx
import pytest

from cofabriq.loop.publish import (
    GitHubPulls,
    Published,
    PublishError,
    branch_name,
    publish,
    push_env,
    push_new_branch,
)
from tests.gitrepo import git, init_repo

PLAYGROUND = "philippmossier/local-ai-chat-playground"

DIFF = """\
diff --git a/src/a.ts b/src/a.ts
index 1111111..2222222 100644
--- a/src/a.ts
+++ b/src/a.ts
@@ -1 +1 @@
-bug
+fixed
diff --git a/src/a.test.sh b/src/a.test.sh
new file mode 100644
--- /dev/null
+++ b/src/a.test.sh
@@ -0,0 +1 @@
+grep -q fixed
"""


class FakeGitHub:
    """GitHub's REST API as a MockTransport: records the requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        url = "https://github.com/o/r/pull/7"
        return httpx.Response(201, json={"number": 7, "html_url": url})

    def pulls(self) -> GitHubPulls:
        return GitHubPulls("tok", transport=httpx.MockTransport(self))


def setup(tmp_path: Path) -> tuple[Path, Path, str]:
    clone = init_repo(tmp_path / "clone", {"src/a.ts": "bug\n"})
    git(clone, "config", "user.name", "dev")
    git(clone, "config", "user.email", "dev@example.com")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    return clone, remote, git(clone, "rev-parse", "HEAD")


def run(
    clone: Path, remote: Path, base: str, github: FakeGitHub, repo: str = PLAYGROUND
) -> Published:
    return publish(
        repo=repo,
        clone=clone,
        base_commit=base,
        diff=DIFF,
        number=51,
        issue_title="Title cut @too early!",
        title="Fix #51: title",
        body="the body",
        token="tok",
        pulls=github.pulls(),
        remote=str(remote),
    )


def test_publish_pushes_a_new_branch_and_opens_a_draft(tmp_path: Path) -> None:
    clone, remote, base = setup(tmp_path)
    github = FakeGitHub()
    got = run(clone, remote, base, github)
    assert got.branch == f"cofabriq/51-title-cut-too-early-{got.sha[:7]}"
    assert (got.number, got.url) == (7, "https://github.com/o/r/pull/7")
    # the branch holds the diff on top of the base, nothing else
    ref = f"refs/heads/{got.branch}"
    assert git(remote, "show", f"{ref}:src/a.ts") == "fixed"
    assert git(remote, "show", f"{ref}:src/a.test.sh") == "grep -q fixed"
    assert git(remote, "rev-parse", f"{ref}^") == base
    assert git(remote, "for-each-ref", "--format=%(refname)") == ref
    # the clone's checkout and index are untouched
    assert git(clone, "rev-parse", "HEAD") == base
    assert git(clone, "status", "--porcelain") == ""
    # a draft PR from the branch to the default branch
    post = github.requests[-1]
    assert post.method == "POST" and post.url.path == f"/repos/{PLAYGROUND}/pulls"
    payload = json.loads(post.content)
    assert payload == {
        "title": "Fix #51: title",
        "head": got.branch,
        "base": "main",
        "body": "the body",
        "draft": True,
    }


def test_an_existing_branch_is_never_overwritten(tmp_path: Path) -> None:
    clone, remote, base = setup(tmp_path)
    got = run(clone, remote, base, FakeGitHub())
    # the same branch again, even with the base on it: refused, left as it was
    with pytest.raises(PublishError, match="git push"):
        push_new_branch(clone, str(remote), base, got.branch, "tok")
    assert git(remote, "rev-parse", f"refs/heads/{got.branch}") == got.sha


def test_only_the_playground_is_writable(tmp_path: Path) -> None:
    clone, remote, base = setup(tmp_path)
    github = FakeGitHub()
    with pytest.raises(PublishError, match="not writable"):
        run(clone, remote, base, github, repo="microsoft/vscode")
    assert github.requests == []
    assert git(remote, "for-each-ref") == ""


def test_push_env_carries_the_token_as_a_header_without_helpers() -> None:
    env = push_env("tok")
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["credential.helper"] == ""
    header = pairs["http.https://github.com/.extraheader"]
    # base64 of "x-access-token:tok"
    assert header == "AUTHORIZATION: basic eC1hY2Nlc3MtdG9rZW46dG9r"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_branch_name_keeps_only_safe_characters() -> None:
    got = branch_name(3, "../../x; rm -rf ~", "abcdef123")
    assert got == "cofabriq/3-x-rm-rf-abcdef1"
    assert branch_name(3, "!!!", "abcdef123") == "cofabriq/3-fix-abcdef1"


def test_update_body_patches_only_the_body() -> None:
    github = FakeGitHub()
    github.pulls().update_body(PLAYGROUND, 7, "new body")
    patch = github.requests[-1]
    assert patch.method == "PATCH" and patch.url.path == f"/repos/{PLAYGROUND}/pulls/7"
    assert json.loads(patch.content) == {"body": "new body"}
