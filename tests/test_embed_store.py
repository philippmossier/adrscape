import re
import uuid
import zlib
from collections.abc import Iterator, Sequence

import psycopg
import pytest
from psycopg import sql

from adrscape.config import get_settings
from adrscape.ingest.chunk import Chunk
from adrscape.ingest.embed import DIM, embed_input
from adrscape.ingest.source import DocKind
from adrscape.store import postgres


class FakeEmbedder:
    """Bag of words: one dimension per word. Records every text it embeds."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        self.seen.extend(texts)
        return [self.vector(t) for t in texts]

    @staticmethod
    def vector(text: str) -> list[float]:
        v = [0.0] * DIM
        for word in re.findall(r"[a-z]+", text.lower()):
            v[zlib.crc32(word.encode()) % DIM] += 1.0
        return v


def chunk(path: str, text: str, sha: str = "aaa", ord: int = 0) -> Chunk:
    return Chunk(
        repo="repo-a",
        path=path,
        sha=sha,
        kind=DocKind.ADR,
        ord=ord,
        heading="Title",
        text=text,
        meta={"status": "Accepted"},
    )


@pytest.fixture
def conn() -> Iterator[postgres.Conn]:
    """A connection whose tables live in a throwaway schema."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    ident = sql.Identifier(schema)
    dsn = get_settings().postgres_dsn
    try:
        admin = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable (make up)")
    with admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
        scoped = f"{dsn}?options=-csearch_path%3D{schema},public"
        try:
            with postgres.connect(scoped) as c:
                yield c
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(ident))


def test_embed_input_puts_the_heading_trail_first() -> None:
    assert embed_input(chunk("a.md", "Body")) == "Title\n\nBody"


def test_sync_embeds_only_documents_not_stored_yet(conn: postgres.Conn) -> None:
    docs = [chunk("a.md", "use kafka"), chunk("b.md", "use sse")]
    first, second = FakeEmbedder(), FakeEmbedder()

    r1 = postgres.sync(conn, docs, first)
    r2 = postgres.sync(conn, docs, second)

    assert (r1.embedded, r1.chunks, r1.unchanged, r1.removed) == (2, 2, 0, 0)
    assert (r2.embedded, r2.unchanged) == (0, 2)
    assert second.seen == []


def test_sync_replaces_changed_and_drops_missing(conn: postgres.Conn) -> None:
    # Yesterday's ingestion: a.md and b.md at sha "aaa".
    postgres.sync(conn, [chunk("a.md", "old"), chunk("b.md", "gone")], FakeEmbedder())

    # Today's: a.md was edited (new sha), b.md was deleted.
    report = postgres.sync(conn, [chunk("a.md", "new", sha="bbb")], FakeEmbedder())

    rows = conn.execute("SELECT path, sha, text FROM chunks").fetchall()
    assert rows == [("a.md", "bbb", "new")]
    assert (report.embedded, report.removed) == (1, 2)


def test_failed_embedding_leaves_the_table_untouched(conn: postgres.Conn) -> None:
    postgres.sync(conn, [chunk("a.md", "old")], FakeEmbedder())

    def broken(texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("API down")

    with pytest.raises(RuntimeError):
        postgres.sync(conn, [chunk("a.md", "new", sha="bbb")], broken)

    rows = conn.execute("SELECT sha FROM chunks").fetchall()
    assert rows == [("aaa",)]


def test_search_ranks_the_closest_chunk_first(conn: postgres.Conn) -> None:
    embed = FakeEmbedder()
    postgres.sync(
        conn,
        [
            chunk("queue.md", "we use kafka as message queue"),
            chunk("ui.md", "react components and css"),
            chunk("db.md", "postgres for storage", ord=0),
            chunk("db.md", "backups run nightly", ord=1),
        ],
        embed,
    )

    hits = postgres.search(conn, embed.vector("which message queue"), k=2)

    assert len(hits) == 2
    assert hits[0].path == "queue.md"
    assert hits[0].score > hits[1].score
    assert hits[0].kind == "adr"
