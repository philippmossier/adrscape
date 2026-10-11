from collections.abc import Iterator
from typing import LiteralString

import pytest
from neo4j import Driver
from neo4j.exceptions import ServiceUnavailable

from adrscape.config import get_settings
from adrscape.domain import AdrStatus, Extraction, Link
from adrscape.ingest.extract import VERSION, ExtractedDoc
from adrscape.ingest.parse import MarkdownParser, ParsedDoc
from adrscape.ingest.source import DocKind, DocRef
from adrscape.store import graph

# The throwaway instance from compose (`neo4j-test`), never the dev graph.
TEST_URI = "bolt://localhost:7688"


@pytest.fixture
def driver() -> Iterator[Driver]:
    settings = get_settings()
    password = settings.neo4j_password.get_secret_value()
    try:
        d = graph.connect(TEST_URI, settings.neo4j_user, password)
    except ServiceUnavailable:
        pytest.skip("Neo4j test instance not reachable (make up)")
    with d:
        d.execute_query("MATCH (n) DETACH DELETE n")
        yield d


def adr(
    repo: str,
    n: int,
    sha: str = "aaa",
    date: str = "2026-10-05",
    status: AdrStatus | None = AdrStatus.ACCEPTED,
    **links: object,
) -> tuple[ParsedDoc, ExtractedDoc]:
    """A parsed ADR and its extraction record, linked as given."""
    ref = DocRef(repo, f"docs/adr/000{n}.md", sha, DocKind.ADR)
    doc = MarkdownParser().parse(ref, f"# {n}. Decision\n\nDate: {date}\n")
    fields: dict[str, object] = {
        "status": status,
        "technologies": [],
        "teams": [],
        "revises": [],
        "revisits": [],
        "depends_on": [],
        "affects": [repo],
    }
    extraction = Extraction.model_validate(fields | links)
    record = ExtractedDoc(
        ref=ref, version=VERSION, candidates=[], extraction=extraction, dropped=[]
    )
    return doc, record


def sync(driver: Driver, *pairs: tuple[ParsedDoc, ExtractedDoc]) -> graph.GraphReport:
    return graph.sync(driver, [d for d, _ in pairs], [r for _, r in pairs])


def link(target: str) -> Link:
    return Link(target=target, evidence="quote")


def count(driver: Driver, pattern: LiteralString) -> int:
    records, _, _ = driver.execute_query(f"MATCH {pattern} RETURN count(*) AS n")
    return records[0]["n"]


def test_names_merge_by_key_and_alias() -> None:
    assert graph.canonical("Tailwind-CSS") == ("tailwind", "Tailwind")
    assert graph.canonical("Server-Sent Events") == ("sse", "SSE")
    assert graph.canonical(" uvicorn ") == ("uvicorn", "uvicorn")
    assert len({graph.key(n) for n in ["C", "C++", "C#"]}) == 3


def test_sync_writes_once_and_merges_technologies(driver: Driver) -> None:
    a = adr("repo-a", 1, technologies=["SSE", "Tailwind CSS"])
    b = adr("repo-b", 1, technologies=["Server-Sent Events", "tailwind"])

    first = sync(driver, a, b)
    second = sync(driver, a, b)

    assert (first.written, second.written, second.unchanged) == (2, 0, 2)
    assert count(driver, "(:Technology)") == 2
    assert count(driver, "(:Document)-[:USES]->(:Technology {key: 'sse'})") == 2


def test_a_changed_document_replaces_its_edges(driver: Driver) -> None:
    ts = adr("repo-ts", 3)
    py = adr(
        "repo-py",
        2,
        date="2026-10-06",
        technologies=["FastAPI"],
        revisits=[link("repo-ts:docs/adr/0003.md")],
        depends_on=[link("repo-ts")],
    )
    sync(driver, ts, py)
    assert count(driver, "()-[:REVISITS]->()") == 1

    edited = adr("repo-py", 2, sha="bbb", date="2026-10-06")
    report = sync(driver, ts, edited)

    assert (report.written, report.unchanged) == (1, 1)
    assert count(driver, "()-[:REVISITS|DEPENDS_ON]->()") == 0
    assert count(driver, "(:Technology)") == 0  # FastAPI: nobody uses it now


def test_a_removed_document_and_its_empty_project_are_deleted(driver: Driver) -> None:
    a, b = adr("repo-a", 1), adr("repo-b", 1)
    sync(driver, a, b)

    report = sync(driver, a)

    assert report.removed == 1
    assert count(driver, "(:Project)") == 1


def test_a_record_without_its_parsed_document_is_skipped(driver: Driver) -> None:
    doc, _ = adr("repo-a", 1, sha="new")
    _, stale = adr("repo-a", 1, sha="old")

    report = graph.sync(driver, [doc], [stale])

    assert report.written == 0
    assert count(driver, "(:Document)") == 0


def test_only_an_accepted_revision_takes_a_decision_out_of_force(
    driver: Driver,
) -> None:
    sync(
        driver,
        adr("repo-a", 1),
        adr("repo-a", 2, revises=[link("repo-a:docs/adr/0001.md")]),
        adr("repo-a", 3),
        adr(
            "repo-a",
            4,
            status=AdrStatus.PROPOSED,
            revises=[link("repo-a:docs/adr/0003.md")],
        ),
        adr("repo-a", 5, status=AdrStatus.SUPERSEDED),
    )

    found = {d.id.split("/")[-1]: d for d in graph.decisions(driver)}

    assert found["0001.md"].revised_by == ["repo-a:docs/adr/0002.md"]
    in_force = sorted(k for k, d in found.items() if d.in_force)
    assert in_force == ["0002.md", "0003.md"]  # 4 is only proposed


def test_affected_follows_depends_on_transitively(driver: Driver) -> None:
    # protocol <- client <- app, and app <- client closes a cycle.
    sync(
        driver,
        adr("protocol", 1),
        adr("client", 1, depends_on=[link("protocol")]),
        adr("app", 1, depends_on=[link("client")]),
        adr("client", 2, depends_on=[link("app")]),
        adr("unrelated", 1),
    )

    found = graph.affected(driver, "protocol:docs/adr/0001.md")

    assert [(a.project, a.hops) for a in found] == [
        ("protocol", 0),
        ("client", 1),
        ("app", 2),
    ]
