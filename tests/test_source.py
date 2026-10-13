from pathlib import Path

import pytest

from cofabriq.context.ingest.source import DocKind, LocalGitSource, classify
from tests.gitrepo import git


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


def test_local_git_source_reads_committed_docs(repo: Path) -> None:
    source = LocalGitSource([repo])
    refs = {r.path: r for r in source.refs()}

    assert set(refs) == {"README.md", "docs/adr/0001-use-sse.md"}
    adr = refs["docs/adr/0001-use-sse.md"]
    assert adr.repo == "demo-repo"
    assert adr.kind is DocKind.ADR
    assert adr.sha == git(repo, "rev-parse", "HEAD:docs/adr/0001-use-sse.md")
    assert source.read(adr) == "# Use SSE\n"


def test_local_git_source_ignores_uncommitted_files(repo: Path) -> None:
    (repo / "docs" / "notes.md").write_text("private\n")
    (repo / "README.md").write_text("edited, not committed\n")

    source = LocalGitSource([repo])
    refs = {r.path: r for r in source.refs()}

    assert "docs/notes.md" not in refs
    assert source.read(refs["README.md"]) == "# Demo\n"


def test_local_git_source_rejects_duplicate_repo_names(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="two repos named"):
        LocalGitSource([tmp_path / "a" / "x", tmp_path / "b" / "x"])
