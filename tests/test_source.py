import subprocess
from pathlib import Path

import pytest

from adrscape.ingest.source import DocKind, LocalGitSource, classify


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("docs/adr/0001-one-protocol.md", DocKind.ADR),
        ("docs/architecture/decisions/0002-x.md", DocKind.ADR),
        ("docs/arch/adr-003-queues.md", DocKind.ADR),
        ("adr/0004-y.md", DocKind.ADR),
        ("README.md", DocKind.README),
        ("backend/readme.md", DocKind.README),
        ("docs/architecture-decisions/0005-z.md", DocKind.ADR),
        ("decision-records/0006-z.md", DocKind.ADR),
        ("docs/ARCHITECTURE.md", DocKind.DOC),
        ("documentation/setup.md", DocKind.DOC),
        ("CLAUDE.md", DocKind.DOC),
        ("CHANGELOG.md", None),
        ("LICENSE.md", None),
        (".github/PULL_REQUEST_TEMPLATE.md", None),
        (".venv/lib/pkg/README.md", None),
        ("package.json", DocKind.MANIFEST),
        ("backend/pyproject.toml", DocKind.MANIFEST),
        ("compose.yaml", DocKind.MANIFEST),
        ("src/app.py", None),
        ("docs/diagram.png", None),
        ("node_modules/lib/README.md", None),
        ("vendor/gem/package.json", None),
    ],
)
def test_classify(path: str, kind: DocKind | None) -> None:
    assert classify(path) == kind


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo-repo"
    (repo / "docs" / "adr").mkdir(parents=True)
    (repo / "docs" / "adr" / "0001-use-sse.md").write_text("# Use SSE\n")
    (repo / "README.md").write_text("# Demo\n")
    (repo / "main.py").write_text("print('hi')\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(
        repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"
    )
    return repo


def test_local_git_source_reads_committed_docs(repo: Path) -> None:
    docs = {d.path: d for d in LocalGitSource([repo]).documents()}

    assert set(docs) == {"README.md", "docs/adr/0001-use-sse.md"}
    adr = docs["docs/adr/0001-use-sse.md"]
    assert adr.repo == "demo-repo"
    assert adr.kind is DocKind.ADR
    assert adr.content == "# Use SSE\n"
    assert adr.sha == _git(repo, "rev-parse", "HEAD:docs/adr/0001-use-sse.md")


def test_local_git_source_ignores_uncommitted_files(repo: Path) -> None:
    (repo / "docs" / "notes.md").write_text("private\n")
    (repo / "README.md").write_text("edited, not committed\n")

    docs = {d.path: d for d in LocalGitSource([repo]).documents()}

    assert "docs/notes.md" not in docs
    assert docs["README.md"].content == "# Demo\n"
