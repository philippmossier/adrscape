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


def similar_adrs(conn: Conn, doc: DocKey, k: int) -> list[tuple[str, str]] | None:
    """The `k` ADRs closest to a stored document, as (repo, path), best first.

    The document's vector is the mean of its chunk vectors; an ADR scores by
    its best chunk. No API call: everything is already in the table. Returns
    None if the document is not stored at this sha (embed has not run).
    """
    rows = conn.execute(
        "WITH q AS (SELECT avg(embedding) AS v FROM chunks"
        "           WHERE repo = %(repo)s AND path = %(path)s AND sha = %(sha)s)"
        " SELECT c.repo, c.path FROM chunks c, q"
        " WHERE q.v IS NOT NULL AND c.kind = 'adr'"
        "   AND NOT (c.repo = %(repo)s AND c.path = %(path)s)"
        " GROUP BY c.repo, c.path"
        " ORDER BY min(c.embedding <=> q.v) LIMIT %(k)s",
        {"repo": doc[0], "path": doc[1], "sha": doc[2], "k": k},
    ).fetchall()
    if not rows and not _stored(conn, doc):
        return None
    return [(r[0], r[1]) for r in rows]


def _stored(conn: Conn, doc: DocKey) -> bool:
    row = conn.execute(
        "SELECT 1 FROM chunks WHERE repo = %s AND path = %s AND sha = %s LIMIT 1",
        doc,
    ).fetchone()
    return row is not None


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
