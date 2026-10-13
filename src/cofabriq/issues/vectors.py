"""Issue vectors in pgvector: one row per issue, nearest earlier issues."""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from pgvector import Vector
from psycopg import sql

from cofabriq.context.ingest.embed import DIM, Embed
from cofabriq.context.store.postgres import Conn
from cofabriq.issues.model import Issue

MAX_BODY = 6000  # characters; bodies with pasted logs reach 50k+

# No ANN index: a few thousand issues per repo scan exactly in milliseconds,
# and an HNSW scan with a `created_at` filter can return fewer than k rows.
SCHEMA = sql.SQL("""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS issues (
    repo       text        NOT NULL,
    number     int         NOT NULL,
    created_at timestamptz NOT NULL,
    input_sha  text        NOT NULL,
    embedding  vector({dim}) NOT NULL,
    PRIMARY KEY (repo, number)
);
""").format(dim=sql.Literal(DIM))


@dataclass(frozen=True)
class IssueSyncReport:
    embedded: int
    unchanged: int


@dataclass(frozen=True)
class Neighbour:
    number: int
    score: float  # cosine similarity, 1 = same direction


def embed_input(issue: Issue) -> str:
    """Title and body only. Comments are left out on purpose: on a closed
    duplicate they name the answer ("duplicate of #N"), and a new issue has
    none yet anyway.
    """
    return f"{issue.title}\n\n{issue.body[:MAX_BODY]}"


def create_schema(conn: Conn) -> None:
    conn.execute(SCHEMA)
    conn.commit()


def sync(conn: Conn, issues: Iterable[Issue], embed: Embed) -> IssueSyncReport:
    """Embed issues whose input text changed since the last sync.

    Keyed by a hash of the input, not `updated_at`: a new comment bumps
    `updated_at` but changes nothing that gets embedded.
    """
    create_schema(conn)
    todo: list[tuple[Issue, str, str]] = []
    unchanged = 0
    stored = {
        (r[0], r[1]): r[2]
        for r in conn.execute("SELECT repo, number, input_sha FROM issues")
    }
    for issue in issues:
        text = embed_input(issue)
        sha = hashlib.sha256(text.encode()).hexdigest()
        if stored.get((issue.repo, issue.number)) == sha:
            unchanged += 1
        else:
            todo.append((issue, text, sha))

    vectors = embed([text for _, text, _ in todo]) if todo else []
    if len(vectors) != len(todo):
        raise RuntimeError(f"asked for {len(todo)} embeddings, got {len(vectors)}")

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO issues (repo, number, created_at, input_sha, embedding)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (repo, number) DO UPDATE SET"
            " input_sha = EXCLUDED.input_sha, embedding = EXCLUDED.embedding",
            [
                (i.repo, i.number, i.created_at, sha, Vector(v))
                for (i, _, sha), v in zip(todo, vectors, strict=True)
            ],
        )
    return IssueSyncReport(embedded=len(todo), unchanged=unchanged)


def nearest(
    conn: Conn, repo: str, number: int, k: int, before: datetime
) -> list[Neighbour]:
    """The k stored issues closest to issue `number`, created before `before`.

    The time cut makes an eval honest: a duplicate can only point at an issue
    that existed when it was filed.
    """
    rows = conn.execute(
        "WITH q AS (SELECT embedding FROM issues"
        "           WHERE repo = %(repo)s AND number = %(n)s)"
        " SELECT i.number, 1 - (i.embedding <=> q.embedding) FROM issues i, q"
        " WHERE i.repo = %(repo)s AND i.number <> %(n)s"
        "   AND i.created_at < %(before)s"
        " ORDER BY i.embedding <=> q.embedding LIMIT %(k)s",
        {"repo": repo, "n": number, "before": before, "k": k},
    ).fetchall()
    return [Neighbour(number=r[0], score=float(r[1])) for r in rows]


def embedding(conn: Conn, repo: str, number: int) -> list[float] | None:
    """The stored vector of one issue, None if it was never embedded."""
    row = conn.execute(
        "SELECT embedding FROM issues WHERE repo = %s AND number = %s",
        (repo, number),
    ).fetchone()
    return None if row is None else row[0].to_list()
