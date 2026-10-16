"""Which commits fixed which issue, from commit messages in a local clone.

A commit fixes issue #N if its message says so with GitHub's closing
keywords ("fixes #N", "Closes #N", "resolved: <issue url>"). A plain "#N"
is a mention, not a fix. One issue can be fixed by several commits; its
fix is all of them together.
"""

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

RECORD = "\x1e"  # starts a commit
FILES = "\x1f"  # ends its message, the changed paths follow


@dataclass(frozen=True)
class Fix:
    sha: str
    at: datetime  # committer date
    files: tuple[str, ...]


def closing_pattern(repo: str) -> re.Pattern[str]:
    url = re.escape(f"https://github.com/{repo}/issues/")
    return re.compile(
        rf"\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)[:\s]+(?:#|{url})(\d+)\b",
        re.IGNORECASE,
    )


def fixes(clone: Path, rev: str, repo: str) -> dict[int, list[Fix]]:
    """Issue number -> the non-merge commits reachable from `rev` that fix it."""
    out = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "log",
            "--no-merges",
            "--no-renames",
            "--name-only",
            f"--format={RECORD}%H %cI%n%B{FILES}",
            rev,
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    pattern = closing_pattern(repo)
    found: dict[int, list[Fix]] = {}
    for record in out.split(RECORD)[1:]:
        head, _, rest = record.partition("\n")
        message, _, paths = rest.partition(FILES)
        numbers = {int(n) for n in pattern.findall(message)}
        if not numbers:
            continue
        sha, at = head.split(" ")
        fix = Fix(
            sha, datetime.fromisoformat(at), tuple(p for p in paths.split("\n") if p)
        )
        for n in numbers:
            found.setdefault(n, []).append(fix)
    return found
