"""Neo4j: knowledge graph of projects, decisions, owners, technologies.

Shape (one node per thing, one edge per extracted link):

    (:Project)-[:HAS]->(:Document)
    (:Document)-[:USES]->(:Technology)
    (:Document)-[:OWNED_BY]->(:Team)
    (:Document)-[:REVISES|REVISITS {evidence, sha}]->(:Document)
    (:Document)-[:DEPENDS_ON {evidence, sha}]->(:Project)
    (:Document)-[:AFFECTS]->(:Project)

Every edge leaves a Document, so a changed document is rewritten by deleting
its outgoing edges and writing the new ones. Nothing else is touched.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import LiteralString

from neo4j import Driver, GraphDatabase, ManagedTransaction

from cofabriq.context.domain import AdrStatus, Link
from cofabriq.context.ingest.extract import ExtractedDoc, doc_id
from cofabriq.context.ingest.parse import ParsedDoc

# Bump when the graph shape or ALIASES change: every document is rewritten.
GRAPH_VERSION = 1

# True synonyms the LLM spells differently, by normalised key. Case, spaces
# and hyphens are handled by `key()`; this is only for different words.
# Hand-curated on the dev fixture, so it grows with the corpus.
ALIASES = {
    "serversentevents": "SSE",
    "contentsecuritypolicy": "CSP",
    "tailwindcss": "Tailwind",
    "shadcnui": "shadcn",
    "webworkers": "Web Worker",
    "openaicompatibleapi": "OpenAI-compatible",
    "openaicompatibleservers": "OpenAI-compatible",
}

CONSTRAINTS: list[LiteralString] = [
    "CREATE CONSTRAINT project_name IF NOT EXISTS "
    "FOR (n:Project) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT document_id IF NOT EXISTS "
    "FOR (n:Document) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT technology_key IF NOT EXISTS "
    "FOR (n:Technology) REQUIRE n.key IS UNIQUE",
    "CREATE CONSTRAINT team_key IF NOT EXISTS FOR (n:Team) REQUIRE n.key IS UNIQUE",
]

# Status values whose ADR is not (or no longer) a decision.
NOT_IN_FORCE = [
    AdrStatus.PROPOSED.value,
    AdrStatus.REJECTED.value,
    AdrStatus.DEPRECATED.value,
    AdrStatus.SUPERSEDED.value,
]


@dataclass(frozen=True)
class GraphReport:
    written: int  # documents written (new, changed, or another version)
    unchanged: int  # documents already in the graph at this sha and version
    removed: int  # documents deleted (gone from the input)


@dataclass(frozen=True)
class Decision:
    id: str
    title: str
    date: str | None
    status: str | None
    revised_by: list[str]  # accepted ADRs that revise this one
    revisits: list[str]  # earlier ADRs deciding the same question elsewhere
    revisited_by: list[str]
    in_force: bool  # status allows it and no accepted ADR revises it


@dataclass(frozen=True)
class Affected:
    project: str
    hops: int  # 0: the decision names it; n: depends on such a project n steps away


def connect(uri: str, user: str, password: str) -> Driver:
    """Open a driver with the uniqueness constraints in place.

    The server's "unrecognized" notices (a label, type or property not in the
    graph yet, e.g. REVISES while no ADR revises another) are filtered out;
    every other warning still shows.
    """
    driver = GraphDatabase.driver(
        uri,
        auth=(user, password),
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )
    for constraint in CONSTRAINTS:
        driver.execute_query(constraint)
    return driver


def key(name: str) -> str:
    """Merge key for a technology or team: 'Tailwind-CSS' -> 'tailwindcss'.

    Signs are spelled out before stripping, so C++ and C# stay distinct.
    """
    spelled = name.casefold().replace("+", "plus").replace("#", "sharp")
    return re.sub(r"[^a-z0-9]", "", spelled)


def canonical(name: str) -> tuple[str, str]:
    """(key, display name) after aliasing."""
    alias = ALIASES.get(key(name))
    return (key(alias), alias) if alias else (key(name), name.strip())


def sync(
    driver: Driver, docs: Iterable[ParsedDoc], records: Iterable[ExtractedDoc]
) -> GraphReport:
    """Make the graph mirror the extraction records, writing only what changed.

    A record is written with its parsed document (title, date). A record whose
    document is not at the same sha is stale (rerun `extract`) and skipped,
    like a document that has no record (manifests). Runs as one transaction.
    """
    parsed = {(d.ref.repo, d.ref.path, d.ref.sha): d for d in docs}
    current = {
        doc_id(r.ref): (parsed[k], r)
        for r in records
        if (k := (r.ref.repo, r.ref.path, r.ref.sha)) in parsed
    }

    def work(tx: ManagedTransaction) -> GraphReport:
        rows = tx.run("MATCH (d:Document) RETURN d.id AS id, d.version AS v")
        stored: dict[str, str | None] = {r["id"]: r["v"] for r in rows}
        gone = [i for i in stored if i not in current]
        todo = [
            (doc, record)
            for i, (doc, record) in sorted(current.items())
            if stored.get(i) != _version(record)
        ]

        tx.run("MATCH (d:Document) WHERE d.id IN $ids DETACH DELETE d", ids=gone)
        for doc, record in todo:
            _write(tx, doc, record)
        _prune(tx)
        return GraphReport(
            written=len(todo),
            unchanged=len(current) - len(todo),
            removed=len(gone),
        )

    with driver.session() as session:
        return session.execute_write(work)


def decisions(driver: Driver, project: str | None = None) -> list[Decision]:
    """Every ADR with what revises and revisits it, oldest first."""
    records, _, _ = driver.execute_query(
        """
        MATCH (p:Project)-[:HAS]->(d:Document {kind: 'adr'})
        WHERE $project IS NULL OR p.name = $project
        RETURN d.id AS id, d.title AS title, d.date AS date, d.status AS status,
            COLLECT {
                MATCH (n:Document)-[:REVISES]->(d)
                WHERE n.status IS NULL OR NOT n.status IN $not_in_force
                RETURN n.id ORDER BY n.id
            } AS revised_by,
            COLLECT { MATCH (d)-[:REVISITS]->(o) RETURN o.id ORDER BY o.id }
                AS revisits,
            COLLECT { MATCH (n)-[:REVISITS]->(d) RETURN n.id ORDER BY n.id }
                AS revisited_by
        ORDER BY d.date, d.id
        """,
        project=project,
        not_in_force=NOT_IN_FORCE,
    )
    return [
        Decision(
            **r.data(),
            in_force=r["status"] not in NOT_IN_FORCE and not r["revised_by"],
        )
        for r in records
    ]


def affected(driver: Driver, adr: str) -> list[Affected]:
    """Projects a decision touches: the ones it names, plus every project that
    depends on one of those, directly or through others (nearest first)."""
    records, _, _ = driver.execute_query(
        """
        MATCH (:Document {id: $adr})-[:AFFECTS]->(named:Project)
        MATCH (p:Project)
              ((:Project)-[:HAS]->(:Document)-[:DEPENDS_ON]->(via:Project))*
              (named)
        RETURN p.name AS project, min(size(via)) AS hops
        ORDER BY hops, project
        """,
        adr=adr,
    )
    return [Affected(**r.data()) for r in records]


def _version(record: ExtractedDoc) -> str:
    return f"{record.ref.sha}|{record.version}|graph/{GRAPH_VERSION}"


def _write(tx: ManagedTransaction, doc: ParsedDoc, record: ExtractedDoc) -> None:
    e = record.extraction
    sha = record.ref.sha
    d = doc_id(record.ref)
    tx.run("MATCH (:Document {id: $id})-[r]->() DELETE r", id=d)
    tx.run(
        """
        MERGE (p:Project {name: $repo})
        MERGE (d:Document {id: $id})
        SET d += $props
        MERGE (p)-[:HAS]->(d)
        """,
        repo=record.ref.repo,
        id=d,
        props={
            "repo": record.ref.repo,
            "path": record.ref.path,
            "sha": sha,
            "kind": record.ref.kind.value,
            "title": doc.title,
            "date": doc.meta.get("date"),
            "status": e.status.value if e.status else None,
            "version": _version(record),
        },
    )
    tx.run(
        """
        MATCH (d:Document {id: $id})
        UNWIND $names AS n
        MERGE (t:Technology {key: n.key}) ON CREATE SET t.name = n.name
        MERGE (d)-[:USES]->(t)
        """,
        id=d,
        names=_names(e.technologies),
    )
    tx.run(
        """
        MATCH (d:Document {id: $id})
        UNWIND $names AS n
        MERGE (t:Team {key: n.key}) ON CREATE SET t.name = n.name
        MERGE (d)-[:OWNED_BY]->(t)
        """,
        id=d,
        names=_names(e.teams),
    )
    # Relationship types can't be parameters in Cypher: one query per type.
    tx.run(
        """
        MATCH (d:Document {id: $id})
        UNWIND $links AS l
        MERGE (t:Document {id: l.target})
        MERGE (d)-[r:REVISES]->(t) SET r.evidence = l.evidence, r.sha = $sha
        """,
        id=d,
        sha=sha,
        links=_links(e.revises),
    )
    tx.run(
        """
        MATCH (d:Document {id: $id})
        UNWIND $links AS l
        MERGE (t:Document {id: l.target})
        MERGE (d)-[r:REVISITS]->(t) SET r.evidence = l.evidence, r.sha = $sha
        """,
        id=d,
        sha=sha,
        links=_links(e.revisits),
    )
    tx.run(
        """
        MATCH (d:Document {id: $id})
        UNWIND $links AS l
        MERGE (p:Project {name: l.target})
        MERGE (d)-[r:DEPENDS_ON]->(p) SET r.evidence = l.evidence, r.sha = $sha
        """,
        id=d,
        sha=sha,
        links=_links(e.depends_on),
    )
    tx.run(
        """
        MATCH (d:Document {id: $id})
        UNWIND $names AS name
        MERGE (p:Project {name: name})
        MERGE (d)-[:AFFECTS]->(p)
        """,
        id=d,
        names=e.affects,
    )


def _prune(tx: ManagedTransaction) -> None:
    """Drop what no document points at any more, and link targets that were
    never written (a record pointing at a document that isn't in the input)."""
    tx.run("MATCH (d:Document) WHERE d.sha IS NULL DETACH DELETE d")
    tx.run("MATCH (t:Technology|Team) WHERE NOT EXISTS { ()-->(t) } DELETE t")
    tx.run("MATCH (p:Project) WHERE NOT EXISTS { (p)-[:HAS]->() } DETACH DELETE p")


def _names(raw: Iterable[str]) -> list[Mapping[str, str]]:
    """One entry per key; the first spelling seen names the node."""
    seen: dict[str, str] = {}
    for name in raw:
        k, display = canonical(name)
        if k:
            seen.setdefault(k, display)
    return [{"key": k, "name": n} for k, n in seen.items()]


def _links(links: Iterable[Link]) -> list[Mapping[str, str]]:
    return [{"target": link.target, "evidence": link.evidence} for link in links]
