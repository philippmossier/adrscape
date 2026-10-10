"""Postgres + pgvector: chunk storage and semantic search."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import psycopg
from pgvector import Vector
from pgvector.psycopg import register_vector
from psycopg import sql
from psycopg.rows import TupleRow, class_row
from psycopg.types.json import Jsonb

from adrscape.ingest.chunk import Chunk
from adrscape.ingest.embed import DIM, Embed, embed_input

type Conn = psycopg.Connection[TupleRow]
type DocKey = tuple[str, str, str]  # (repo, path, sha)

SCHEMA = sql.SQL("""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS chunks (
    repo      text NOT NULL,
    path      text NOT NULL,
    sha       text NOT NULL,
    ord       int  NOT NULL,
    kind      text NOT NULL,
    heading   text NOT NULL,
    text      text NOT NULL,
    meta      jsonb NOT NULL,
    embedding vector({dim}) NOT NULL,
    PRIMARY KEY (repo, path, sha, ord)
);
CREATE INDEX IF NOT EXISTS chunks_embedding_idx
    ON chunks USING hnsw (embedding vector_cosine_ops);
""").format(dim=sql.Literal(DIM))


@dataclass(frozen=True)
class SyncReport:
    embedded: int  # documents embedded (new or changed sha)
    chunks: int  # chunks those documents produced
    unchanged: int  # documents already stored at the same sha
    removed: int  # documents dropped (changed sha or gone from the input)


@dataclass(frozen=True)
class Hit:
    repo: str
    path: str
    ord: int
    kind: str
    heading: str
    text: str
    score: float  # cosine similarity, 1 = same direction


def connect(dsn: str) -> Conn:
    """Open a connection with the schema in place and vectors adapted."""
    conn = psycopg.connect(dsn)
    conn.execute(SCHEMA)
    conn.commit()
    register_vector(conn)
    return conn


def sync(conn: Conn, chunks: Iterable[Chunk], embed: Embed) -> SyncReport:
    """Make the table mirror `chunks`, embedding only documents not stored yet.

    A document is identified by (repo, path, sha), so an unchanged file costs
    no API call. A changed file has a new sha: its old rows are stale and get
    deleted. The API call happens before the transaction opens; if it fails,
    the table is left as it was.
    """
    by_doc: dict[DocKey, list[Chunk]] = defaultdict(list)
    for c in chunks:
        by_doc[(c.repo, c.path, c.sha)].append(c)

    rows = conn.execute("SELECT DISTINCT repo, path, sha FROM chunks").fetchall()
    stored: set[DocKey] = {(r[0], r[1], r[2]) for r in rows}
    new = [c for key, cs in by_doc.items() if key not in stored for c in cs]
    stale = stored - by_doc.keys()

    vectors = embed([embed_input(c) for c in new]) if new else []
    if len(vectors) != len(new):
        raise RuntimeError(f"asked for {len(new)} embeddings, got {len(vectors)}")

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "DELETE FROM chunks WHERE repo = %s AND path = %s AND sha = %s",
            list(stale),
        )
        cur.executemany(
            "INSERT INTO chunks (repo, path, sha, ord, kind, heading, text, meta,"
            " embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [
                (
                    c.repo,
                    c.path,
                    c.sha,
                    c.ord,
                    c.kind.value,
                    c.heading,
                    c.text,
                    Jsonb(dict(c.meta)),
                    Vector(v),
                )
                for c, v in zip(new, vectors, strict=True)
            ],
        )

    embedded = len({(c.repo, c.path, c.sha) for c in new})
    return SyncReport(
        embedded=embedded,
        chunks=len(new),
        unchanged=len(by_doc) - embedded,
        removed=len(stale),
    )


def search(conn: Conn, query: list[float], k: int = 5) -> list[Hit]:
    """Nearest chunks by cosine distance (`<=>`), best first."""
    with conn.cursor(row_factory=class_row(Hit)) as cur:
        cur.execute(
            "SELECT repo, path, ord, kind, heading, text,"
            " 1 - (embedding <=> %(q)s) AS score"
            " FROM chunks ORDER BY embedding <=> %(q)s LIMIT %(k)s",
            {"q": Vector(query), "k": k},
        )
        return cur.fetchall()
