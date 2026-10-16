"""The repo map in Neo4j, next to (not inside) the ADR graph.

    (:Repo {name, rev, until})-[:HAS]->(:Component {repo, name, files, commits})
    (:File {repo, path})-[:IN]->(:Component)
    (:Component)-[:CHANGES_WITH {n}]->(:Component)   stored once, a.name < b.name
    (:File)-[:CHANGES_WITH {n}]->(:File)             each file's strongest pairs
    (:Fix {repo, sha, at, issues})-[:TOUCHED]->(:Component)
                                                     "fixes #N" commits

Every node carries its repo, so maps of several repos live side by side. A
rebuild replaces one repo's map whole: a map is a snapshot as of `rev`,
nothing in it is edited in place.
"""

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, LiteralString

from neo4j import Driver

from cofabriq.map.build import RepoMap
from cofabriq.map.components import ComponentRules, component_of
from cofabriq.map.fixes import Fix

BATCH = 5000

CONSTRAINTS: list[LiteralString] = [
    "CREATE CONSTRAINT repo_name IF NOT EXISTS FOR (n:Repo) REQUIRE n.name IS UNIQUE",
    "CREATE CONSTRAINT component_key IF NOT EXISTS "
    "FOR (n:Component) REQUIRE (n.repo, n.name) IS UNIQUE",
    "CREATE CONSTRAINT file_key IF NOT EXISTS "
    "FOR (n:File) REQUIRE (n.repo, n.path) IS UNIQUE",
    "CREATE CONSTRAINT fix_key IF NOT EXISTS "
    "FOR (n:Fix) REQUIRE (n.repo, n.sha) IS UNIQUE",
]


@dataclass(frozen=True)
class MapReport:
    components: int
    files: int
    component_pairs: int
    file_pairs: int
    fixes: int


@dataclass(frozen=True)
class FixRow:
    sha: str
    at: datetime
    issues: list[int]
    components: list[str]


def fix_rows(
    fixed: Mapping[int, list[Fix]], rules: ComponentRules, known: set[str]
) -> list[FixRow]:
    """One row per fix commit, with every issue it fixes and the components
    it touched that are on the map (`known`)."""
    by_sha: dict[str, tuple[datetime, set[int], set[str]]] = {}
    for number, commits in fixed.items():
        for c in commits:
            _, issues, components = by_sha.setdefault(c.sha, (c.at, set(), set()))
            issues.add(number)
            components.update(
                x for f in c.files if (x := component_of(f, rules)) in known
            )
    return [
        FixRow(sha, at, sorted(issues), sorted(components))
        for sha, (at, issues, components) in sorted(by_sha.items())
        if components
    ]


def write(
    driver: Driver,
    repo: str,
    rev: str,
    until: date | None,
    m: RepoMap,
    fixes: Sequence[FixRow] = (),
) -> MapReport:
    for constraint in CONSTRAINTS:
        driver.execute_query(constraint)
    _delete(driver, repo)
    files_per = Counter(m.files.values())
    driver.execute_query(
        "MERGE (r:Repo {name: $repo}) SET r.rev = $rev, r.until = $until",
        repo=repo,
        rev=rev,
        until=until.isoformat() if until else None,
    )
    components = [
        {"name": c, "files": n, "commits": m.commits[c]} for c, n in files_per.items()
    ]
    _batched(
        driver,
        "MATCH (r:Repo {name: $repo}) UNWIND $rows AS row"
        " CREATE (r)-[:HAS]->(:Component {repo: $repo, name: row.name,"
        " files: row.files, commits: row.commits})",
        repo,
        components,
    )
    _batched(
        driver,
        "UNWIND $rows AS row"
        " MATCH (c:Component {repo: $repo, name: row.component})"
        " CREATE (:File {repo: $repo, path: row.path})-[:IN]->(c)",
        repo,
        [{"path": p, "component": c} for p, c in m.files.items()],
    )
    _batched(
        driver,
        "UNWIND $rows AS row"
        " MATCH (a:Component {repo: $repo, name: row.a}),"
        " (b:Component {repo: $repo, name: row.b})"
        " CREATE (a)-[:CHANGES_WITH {n: row.n}]->(b)",
        repo,
        [{"a": a, "b": b, "n": n} for (a, b), n in m.components_together.items()],
    )
    _batched(
        driver,
        "UNWIND $rows AS row"
        " MATCH (a:File {repo: $repo, path: row.a}),"
        " (b:File {repo: $repo, path: row.b})"
        " CREATE (a)-[:CHANGES_WITH {n: row.n}]->(b)",
        repo,
        [{"a": a, "b": b, "n": n} for a, b, n in m.files_together],
    )
    _batched(
        driver,
        "UNWIND $rows AS row"
        " CREATE (f:Fix {repo: $repo, sha: row.sha, at: row.at, issues: row.issues})"
        " WITH f, row UNWIND row.components AS name"
        " MATCH (c:Component {repo: $repo, name: name})"
        " CREATE (f)-[:TOUCHED]->(c)",
        repo,
        [
            {"sha": f.sha, "at": f.at, "issues": f.issues, "components": f.components}
            for f in fixes
        ],
    )
    return MapReport(
        len(components),
        len(m.files),
        len(m.components_together),
        len(m.files_together),
        len(fixes),
    )


@dataclass(frozen=True)
class Component:
    name: str
    files: int
    commits: int


@dataclass(frozen=True)
class Partner:
    name: str
    n: int  # commits that changed both
    share: float  # n / commits of the asked component


def components(driver: Driver, repo: str) -> list[Component]:
    """A repo's components, most changed first."""
    records, _, _ = driver.execute_query(
        "MATCH (:Repo {name: $repo})-[:HAS]->(c:Component)"
        " RETURN c.name AS name, c.files AS files, c.commits AS commits"
        " ORDER BY commits DESC, name",
        repo=repo,
    )
    return [Component(r["name"], r["files"], r["commits"]) for r in records]


def changes_with(driver: Driver, repo: str, component: str, k: int) -> list[Partner]:
    """The k components most often changed in the same commit as `component`."""
    records, _, _ = driver.execute_query(
        "MATCH (c:Component {repo: $repo, name: $name})-[e:CHANGES_WITH]-(o)"
        " RETURN o.name AS name, e.n AS n, toFloat(e.n) / c.commits AS share"
        " ORDER BY n DESC, name LIMIT $k",
        repo=repo,
        name=component,
        k=k,
    )
    return [Partner(r["name"], r["n"], r["share"]) for r in records]


def fixed(
    driver: Driver, repo: str, issues: Sequence[int], before: datetime | None
) -> dict[int, list[str]]:
    """Components touched by the fixes of these issues, counting only fix
    commits before `before` (None: all on the map)."""
    records, _, _ = driver.execute_query(
        "MATCH (f:Fix {repo: $repo})-[:TOUCHED]->(c)"
        " WHERE ($before IS NULL OR f.at < $before)"
        " UNWIND [n IN f.issues WHERE n IN $issues] AS issue"
        " RETURN issue, collect(DISTINCT c.name) AS components",
        repo=repo,
        issues=list(issues),
        before=before,
    )
    return {r["issue"]: sorted(r["components"]) for r in records}


def fixed_issues(driver: Driver, repo: str, before: datetime | None) -> list[int]:
    """Issues with a fix commit on the map before `before` (None: any)."""
    records, _, _ = driver.execute_query(
        "MATCH (f:Fix {repo: $repo})"
        " WHERE ($before IS NULL OR f.at < $before)"
        " UNWIND f.issues AS issue RETURN DISTINCT issue",
        repo=repo,
        before=before,
    )
    return sorted(r["issue"] for r in records)


def rev(driver: Driver, repo: str) -> str | None:
    records, _, _ = driver.execute_query(
        "MATCH (r:Repo {name: $repo}) RETURN r.rev AS rev", repo=repo
    )
    return records[0]["rev"] if records else None


def cut(driver: Driver, repo: str) -> str | None:
    """The date a repo's map was cut at (ISO), None for a head map or no map."""
    records, _, _ = driver.execute_query(
        "MATCH (r:Repo {name: $repo}) RETURN r.until AS until", repo=repo
    )
    return records[0]["until"] if records else None


def _delete(driver: Driver, repo: str) -> None:
    """Batched, so a 60k-file map doesn't need one huge transaction."""
    while True:
        _, summary, _ = driver.execute_query(
            "MATCH (n:Component|File|Fix {repo: $repo})"
            " WITH n LIMIT $batch DETACH DELETE n",
            repo=repo,
            batch=BATCH,
        )
        if summary.counters.nodes_deleted == 0:
            return


def _batched(
    driver: Driver, query: LiteralString, repo: str, rows: Sequence[dict[str, Any]]
) -> None:
    for chunk in _chunks(rows):
        driver.execute_query(query, repo=repo, rows=chunk)


def _chunks(rows: Sequence[dict[str, Any]]) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), BATCH):
        yield rows[start : start + BATCH]
