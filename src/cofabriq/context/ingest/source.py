"""Where documents come from: the `Source` seam and its implementations."""

import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol


class DocKind(StrEnum):
    ADR = "adr"
    README = "readme"
    DOC = "doc"
    MANIFEST = "manifest"


@dataclass(frozen=True)
class DocRef:
    """One file as a Source lists it: identity and kind, no content.

    `sha` is the git blob sha, the same value the GitHub API returns, so a
    document has one identity (repo, path, sha) whichever Source listed it.
    """

    repo: str
    path: str
    sha: str
    kind: DocKind


class Source(Protocol):
    """Lists documents cheaply, reads content only on request.

    Listing and reading are separate so a caller can compare shas first and
    read only what changed: one tree call plus one call per changed file
    against a rate-limited API.
    """

    def refs(self) -> Iterator[DocRef]: ...

    def read(self, ref: DocRef) -> str: ...


MANIFESTS = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "compose.yaml",
        "compose.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
    }
)
SKIPPED_DIRS = frozenset(
    {
        ".github",
        "node_modules",
        "vendor",
        "third_party",
        ".venv",
        "venv",
        "site-packages",
    }
)
SKIPPED_STEMS = frozenset({"changelog", "license", "licence", "code_of_conduct"})
ADR_FILENAME = re.compile(r"^adr-\d+", re.IGNORECASE)


def _is_adr_dir(name: str) -> bool:
    return name in {"adr", "adrs"} or "decision" in name


def classify(path: str) -> DocKind | None:
    """Decide from the path alone whether a file is ingested, and as what.

    Docs and manifests only; source code is out of scope. Every committed
    markdown file counts except a small denylist: docs folders are named too
    differently (`docs`, `documentation`, `wiki`) for an allowlist, and a
    missing doc is silent while a noisy one shows up in `cofabriq docs`.
    ADR folders vary as well (`docs/adr`, `docs/architecture/decisions`,
    `docs/arch/adr-001-*`), so ADRs are matched by folder name or filename.
    """
    p = PurePosixPath(path)
    dirs = [d.lower() for d in p.parts[:-1]]
    if SKIPPED_DIRS.intersection(dirs):
        return None
    if p.name in MANIFESTS:
        return DocKind.MANIFEST
    if p.suffix.lower() != ".md" or p.stem.lower() in SKIPPED_STEMS:
        return None
    if any(_is_adr_dir(d) for d in dirs) or ADR_FILENAME.match(p.name):
        return DocKind.ADR
    if p.stem.lower() == "readme":
        return DocKind.README
    return DocKind.DOC


class LocalGitSource:
    """Documents from local git clones, as committed on HEAD.

    Reads the git tree, not the working directory: uncommitted edits and
    ignored files (`.env`, local notes) never get ingested, and the result
    matches what the GitHub API would return for the same commit.
    """

    def __init__(self, repos: Sequence[Path]) -> None:
        self._repos: dict[str, Path] = {}
        for repo in repos:
            name = repo.resolve().name
            if name in self._repos:
                clash = f"{self._repos[name]}, {repo}"
                raise ValueError(f"two repos named {name!r}: {clash}")
            self._repos[name] = repo

    def refs(self) -> Iterator[DocRef]:
        for name, repo in self._repos.items():
            tree = _git(repo, "ls-tree", "-r", "-z", "HEAD")
            for entry in tree.split("\0"):
                if not entry:
                    continue
                meta, path = entry.split("\t", 1)
                _mode, obj_type, sha = meta.split()
                kind = classify(path)
                if obj_type == "blob" and kind is not None:
                    yield DocRef(repo=name, path=path, sha=sha, kind=kind)

    def read(self, ref: DocRef) -> str:
        return _git(self._repos[ref.repo], "cat-file", "blob", ref.sha)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        encoding="utf-8",
    ).stdout
