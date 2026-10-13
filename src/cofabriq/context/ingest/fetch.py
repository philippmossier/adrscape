"""Copy documents from a Source into `data/raw/`, skipping unchanged ones."""

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter

from cofabriq.context.ingest.source import DocRef, Source

INDEX_FILE = "index.json"
_index_adapter = TypeAdapter(list[DocRef])


@dataclass(frozen=True)
class FetchReport:
    written: int
    unchanged: int
    removed: int


def fetch(source: Source, raw_dir: Path) -> FetchReport:
    """Mirror the Source into `raw_dir/<repo>/<path>` plus `raw_dir/index.json`.

    Idempotent by (repo, path, sha): a ref whose sha is already in the index
    (and whose file still exists) is not read again. Files that disappeared
    from a fetched repo are removed; repos not in this Source are left alone,
    so fixtures can be fetched one at a time. A repo whose last document was
    deleted yields no refs and keeps its stale files (accepted for now).
    """
    root = raw_dir.resolve()
    # last fetch (from index.json) vs what the Source lists now
    prev = {(r.repo, r.path): r for r in load_index(root)}
    curr: dict[tuple[str, str], DocRef] = {}
    written = unchanged = 0

    for ref in source.refs():
        target = raw_path(root, ref)
        known = prev.get((ref.repo, ref.path))
        if known is not None and known.sha == ref.sha and target.is_file():
            unchanged += 1
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read(ref), encoding="utf-8")
            written += 1
        curr[(ref.repo, ref.path)] = ref

    fetched_repos = {repo for repo, _ in curr}
    removed = 0
    for key, ref in prev.items():
        if key in curr:
            continue
        if ref.repo in fetched_repos:
            raw_path(root, ref).unlink(missing_ok=True)
            removed += 1
        else:
            curr[key] = ref

    _write_index(root, sorted(curr.values(), key=lambda r: (r.repo, r.path)))
    return FetchReport(written=written, unchanged=unchanged, removed=removed)


def load_index(raw_dir: Path) -> list[DocRef]:
    index = raw_dir / INDEX_FILE
    if not index.is_file():
        return []
    return _index_adapter.validate_json(index.read_bytes())


def _write_index(root: Path, refs: list[DocRef]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f"{INDEX_FILE}.tmp"
    tmp.write_bytes(_index_adapter.dump_json(refs, indent=2))
    os.replace(tmp, root / INDEX_FILE)


def raw_path(root: Path, ref: DocRef) -> Path:
    """Where a ref lands on disk. Repo and path come from untrusted input."""
    repo_dir = (root / ref.repo).resolve()
    target = (repo_dir / ref.path).resolve()
    if repo_dir.parent != root or not target.is_relative_to(repo_dir):
        raise ValueError(f"refusing to write outside {root}: {ref.repo}/{ref.path}")
    return target
