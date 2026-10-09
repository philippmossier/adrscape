from collections.abc import Iterator
from pathlib import Path

import pytest

from adrscape.ingest.fetch import fetch, load_index
from adrscape.ingest.source import DocKind, DocRef, LocalGitSource
from tests.gitrepo import commit_all, init_repo


class CountingSource(LocalGitSource):
    """Records which refs were read, to prove unchanged files are skipped."""

    def __init__(self, repos: list[Path]) -> None:
        super().__init__(repos)
        self.reads: list[str] = []

    def read(self, ref: DocRef) -> str:
        self.reads.append(ref.path)
        return super().read(ref)


def test_fetch_mirrors_docs_and_writes_index(repo: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw"

    report = fetch(LocalGitSource([repo]), raw)

    assert (report.written, report.unchanged, report.removed) == (2, 0, 0)
    adr = raw / "demo-repo" / "docs" / "adr" / "0001-use-sse.md"
    assert adr.read_text() == "# Use SSE\n"
    assert not (raw / "demo-repo" / "main.py").exists()
    index = load_index(raw)
    assert [r.path for r in index] == ["README.md", "docs/adr/0001-use-sse.md"]
    assert index[1].kind is DocKind.ADR


def test_second_fetch_reads_only_changed_files(repo: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    fetch(LocalGitSource([repo]), raw)
    (repo / "README.md").write_text("# Demo v2\n")
    commit_all(repo)

    source = CountingSource([repo])
    report = fetch(source, raw)

    assert source.reads == ["README.md"]
    assert (report.written, report.unchanged, report.removed) == (1, 1, 0)
    assert (raw / "demo-repo" / "README.md").read_text() == "# Demo v2\n"


def test_fetch_restores_a_deleted_raw_file(repo: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    fetch(LocalGitSource([repo]), raw)
    (raw / "demo-repo" / "README.md").unlink()

    report = fetch(LocalGitSource([repo]), raw)

    assert report.written == 1
    assert (raw / "demo-repo" / "README.md").is_file()


def test_fetch_removes_files_deleted_upstream(repo: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    fetch(LocalGitSource([repo]), raw)
    (repo / "docs" / "adr" / "0001-use-sse.md").unlink()
    commit_all(repo)

    report = fetch(LocalGitSource([repo]), raw)

    assert report.removed == 1
    assert not (raw / "demo-repo" / "docs" / "adr" / "0001-use-sse.md").exists()
    assert [r.path for r in load_index(raw)] == ["README.md"]


def test_fetch_keeps_repos_outside_the_source(repo: Path, tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    other = init_repo(tmp_path / "other-repo", {"README.md": "# Other\n"})
    fetch(LocalGitSource([other]), raw)

    fetch(LocalGitSource([repo]), raw)

    assert {r.repo for r in load_index(raw)} == {"demo-repo", "other-repo"}
    assert (raw / "other-repo" / "README.md").is_file()


class EvilSource:
    """A Source with an untrusted path, as a remote API could return it."""

    def refs(self) -> Iterator[DocRef]:
        yield DocRef(repo="r", path="../../escape.md", sha="0", kind=DocKind.DOC)

    def read(self, ref: DocRef) -> str:
        return "pwned"


def test_fetch_refuses_paths_outside_raw_dir(tmp_path: Path) -> None:
    raw = tmp_path / "raw"

    with pytest.raises(ValueError, match="refusing to write outside"):
        fetch(EvilSource(), raw)

    assert not (tmp_path / "escape.md").exists()
