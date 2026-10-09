"""Split parsed documents into chunks for embedding."""

import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass

from adrscape.ingest.parse import ParsedDoc, Section
from adrscape.ingest.source import DocKind

# ~600 tokens. Every ADR in the dev fixture (max 2103 chars) fits whole, so
# Context and Decision are never embedded apart. Step 4's model must take it.
MAX_CHARS = 2400
PARAGRAPH = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class Chunk:
    """One embeddable piece of a document, keeping the document's identity."""

    repo: str
    path: str
    sha: str
    kind: DocKind
    ord: int
    heading: str
    text: str
    meta: Mapping[str, str]


def chunk_doc(doc: ParsedDoc, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """Heading-aware chunking: cut at headings, merge small, split big.

    Consecutive sections are merged while they fit in `max_chars`; a chunk's
    heading is the trail of its first section. A section that alone exceeds
    the budget is split at blank lines, and a single paragraph that still
    does is cut hard (rare: huge tables or code blocks).
    """
    pieces = [p for s in doc.sections for p in _fit(s, max_chars)]
    chunks: list[Chunk] = []
    for heading, text in _merge(pieces, max_chars):
        ref = doc.ref
        chunks.append(
            Chunk(
                repo=ref.repo,
                path=ref.path,
                sha=ref.sha,
                kind=ref.kind,
                ord=len(chunks),
                heading=" > ".join(heading) or doc.title,
                text=text,
                meta=doc.meta,
            )
        )
    return chunks


def chunk_docs(
    docs: Iterable[ParsedDoc], max_chars: int = MAX_CHARS
) -> Iterator[Chunk]:
    for doc in docs:
        yield from chunk_doc(doc, max_chars)


def _fit(section: Section, max_chars: int) -> Iterator[Section]:
    """Yield the section itself, or paragraph groups of it that fit the budget."""
    if len(section.text) <= max_chars:
        yield section
        return
    paragraphs = [
        part[i : i + max_chars]
        for p in PARAGRAPH.split(section.text)
        if (part := p.strip())
        for i in range(0, len(part), max_chars)
    ]
    for _, text in _merge([Section(section.path, p) for p in paragraphs], max_chars):
        yield Section(section.path, text)


def _merge(
    pieces: list[Section], max_chars: int
) -> Iterator[tuple[tuple[str, ...], str]]:
    """Greedily join consecutive pieces while the joined text fits."""
    heading: tuple[str, ...] = ()
    texts: list[str] = []
    size = 0
    for piece in pieces:
        added = len(piece.text) + (2 if texts else 0)  # "\n\n" separator
        if texts and size + added > max_chars:
            yield heading, "\n\n".join(texts)
            texts, size = [], 0
            added = len(piece.text)
        if not texts:
            heading = piece.path
        texts.append(piece.text)
        size += added
    if texts:
        yield heading, "\n\n".join(texts)
