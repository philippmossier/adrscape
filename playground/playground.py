"""Seed and reset the demo playground on GitHub.

    uv run python playground/playground.py seed
    uv run python playground/playground.py reset --yes   # wipe, then seed

Writes to GitHub through the `gh` CLI with the caller's own login, not
through cofabriq (which stays read-only towards GitHub through v2). The repo
is fixed below on purpose: this script can't be pointed at anything else.

reset: deletes every issue, closes open pull requests, deletes every branch
but main, moves main back to the `baseline` tag, then seeds.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

from cofabriq.eval.triage import load_seed

REPO = "philippmossier/local-ai-chat-playground"
SEED = Path(__file__).with_name("seed.toml")
PAUSE = 2.0  # seconds between creates: distinct created_at, kinder to the API


def gh(*args: str) -> str:
    done = subprocess.run(["gh", *args], check=True, capture_output=True, text=True)
    return done.stdout


def gh_json(*args: str) -> list[dict[str, object]]:
    return json.loads(gh(*args))


def issue_numbers() -> list[int]:
    found = gh_json(
        "issue", "list", "-R", REPO, "--state", "all", "--limit", "1000",
        "--json", "number",
    )  # fmt: skip
    return [int(str(i["number"])) for i in found]


def seed() -> None:
    if issue_numbers():
        sys.exit(f"{REPO} already has issues: run `reset --yes` instead")
    for n, item in enumerate(load_seed(SEED)):
        if n:
            time.sleep(PAUSE)
        url = gh(
            "issue", "create", "-R", REPO, "--title", item.title,
            "--body", item.body.strip(),
        ).strip()  # fmt: skip
        if item.closed:
            gh("issue", "close", url, "-R", REPO)
        print(f"{item.key:<24} {url}{'  (closed)' if item.closed else ''}")


def reset() -> None:
    for number in issue_numbers():
        gh("issue", "delete", str(number), "-R", REPO, "--yes")
        print(f"deleted issue #{number}")
    prs = gh_json("pr", "list", "-R", REPO, "--state", "open", "--json", "number")
    for pr in prs:
        gh("pr", "close", str(pr["number"]), "-R", REPO)
        print(f"closed PR #{pr['number']}")
    branches = gh("api", f"repos/{REPO}/branches", "--paginate", "-q", ".[].name")
    for branch in branches.split():
        if branch != "main":
            gh("api", "-X", "DELETE", f"repos/{REPO}/git/refs/heads/{branch}")
            print(f"deleted branch {branch}")
    sha = gh("api", f"repos/{REPO}/commits/baseline", "-q", ".sha").strip()
    gh(
        "api", "-X", "PATCH", f"repos/{REPO}/git/refs/heads/main",
        "-f", f"sha={sha}", "-F", "force=true",
    )  # fmt: skip
    print(f"main -> baseline {sha[:7]}")
    seed()


def main() -> None:
    args = sys.argv[1:]
    if args == ["seed"]:
        seed()
    elif args == ["reset", "--yes"]:
        reset()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
