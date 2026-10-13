"""Command-line entry point (`cofabriq ...`)."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median

import psycopg
import typer
from neo4j import Driver, GraphDatabase

from cofabriq.config import get_settings
from cofabriq.context.ingest.chunk import chunk_docs
from cofabriq.context.ingest.embed import Embed, openai_embedder
from cofabriq.context.ingest.extract import CANDIDATES, ExtractedDoc, OpenAIExtractor
from cofabriq.context.ingest.extract import load as load_extracted
from cofabriq.context.ingest.extract import sync as extract_sync
from cofabriq.context.ingest.fetch import fetch as fetch_source
from cofabriq.context.ingest.parse import load_docs
from cofabriq.context.ingest.source import LocalGitSource, Source
from cofabriq.context.store import graph as kg
from cofabriq.context.store import postgres
from cofabriq.eval import dedupe
from cofabriq.issues import vectors
from cofabriq.issues import verdict as verdict_mod
from cofabriq.issues.fetch import fetch_issues, load_issues
from cofabriq.issues.github import GitHubIssueSource
from cofabriq.issues.model import Issue, check_repo
from cofabriq.mcp_server import build as build_mcp

app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True, help="Measure retrieval and verdicts.")
app.add_typer(eval_app, name="eval")
EVALS_DIR = Path("evals")
CANDIDATE_ISSUES = 10  # vector top-k the verdict chooses from


@app.callback()
def main() -> None:
    """Map a company's engineering landscape from its repos."""


def _check_postgres() -> str:
    with psycopg.connect(get_settings().postgres_dsn, connect_timeout=3) as conn:
        version = conn.execute("SHOW server_version").fetchone()
    return f"server {version[0] if version else '?'}"


def _check_pgvector() -> str:
    with psycopg.connect(get_settings().postgres_dsn, connect_timeout=3) as conn:
        row = conn.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
    if row is None:
        raise RuntimeError("extension 'vector' not installed")
    return f"vector {row[0]}"


def _check_neo4j() -> str:
    settings = get_settings()
    auth = (settings.neo4j_user, settings.neo4j_password.get_secret_value())
    with GraphDatabase.driver(settings.neo4j_uri, auth=auth) as driver:
        info = driver.get_server_info()
    return f"{info.agent}"


@app.command()
def health() -> None:
    """Check that Postgres, pgvector and Neo4j are reachable."""
    checks: dict[str, Callable[[], str]] = {
        "postgres": _check_postgres,
        "pgvector": _check_pgvector,
        "neo4j": _check_neo4j,
    }
    failed = False
    for name, check in checks.items():
        try:
            typer.echo(f"{name:<9} OK    {check()}")
        except Exception as exc:
            failed = True
            typer.echo(f"{name:<9} FAIL  {type(exc).__name__}: {exc}")
    raise typer.Exit(code=1 if failed else 0)


@app.command()
def docs(repos: list[Path]) -> None:
    """List the documents cofabriq would ingest from local git clones."""
    source: Source = LocalGitSource(repos)
    count = 0
    for ref in source.refs():
        count += 1
        typer.echo(f"{ref.kind:<9} {ref.sha[:7]}  {ref.repo}/{ref.path}")
    typer.echo(f"{count} documents")


@app.command()
def fetch(repos: list[Path]) -> None:
    """Copy documents from local git clones into the raw dir, skip unchanged."""
    raw_dir = get_settings().raw_dir
    report = fetch_source(LocalGitSource(repos), raw_dir)
    typer.echo(
        f"{report.written} written, {report.unchanged} unchanged, "
        f"{report.removed} removed -> {raw_dir}"
    )


def _target_repo(repo: str | None) -> str:
    target = repo or get_settings().target_repo
    if target is None:
        typer.echo("pass --repo or set TARGET_REPO (.env.example)", err=True)
        raise typer.Exit(code=1)
    return check_repo(target)


@app.command("fetch-issues")
def fetch_issues_cmd(repo: str | None = None) -> None:
    """Copy a GitHub repo's issues into the issues dir, only what changed."""
    settings = get_settings()
    target = _target_repo(repo)
    if settings.github_token is None:
        typer.echo("GITHUB_TOKEN is not set (.env.example)", err=True)
        raise typer.Exit(code=1)
    source = GitHubIssueSource(
        target,
        settings.github_token.get_secret_value(),
        comments=settings.issue_comments,
    )
    report = fetch_issues(source, settings.issues_dir)
    since = report.since.isoformat() if report.since else "the start"
    typer.echo(
        f"{report.written} written, {report.unchanged} unchanged "
        f"(updated since {since}) -> {settings.issues_dir}"
    )


@app.command("embed-issues")
def embed_issues(repo: str | None = None) -> None:
    """Embed fetched issues (title + body) into Postgres; skip unchanged ones."""
    settings = get_settings()
    issues = load_issues(settings.issues_dir, _target_repo(repo))
    with postgres.connect(settings.postgres_dsn) as conn:
        report = vectors.sync(conn, issues, _embedder())
    typer.echo(f"{report.embedded} issues embedded, {report.unchanged} unchanged")


def _evalset_path(repo: str) -> Path:
    return EVALS_DIR / f"dedupe-{repo.replace('/', '-')}.json"


@eval_app.command("dedupe-set")
def dedupe_set(repo: str | None = None, force: bool = False) -> None:
    """Build and freeze the duplicate eval set from the fetched issues."""
    target = _target_repo(repo)
    path = _evalset_path(target)
    if path.exists() and not force:
        typer.echo(f"{path} is frozen; --force to rebuild it", err=True)
        raise typer.Exit(code=1)
    issues = load_issues(get_settings().issues_dir, target)
    evalset, report = dedupe.build(issues, frozen_at=datetime.now(UTC))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evalset.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(
        f"{report.pairs} pairs ({report.by_signal}), {len(evalset.negatives)} "
        f"negatives from {len(issues)} issues; dropped {report.ambiguous} "
        f"ambiguous, {report.unusable} unusable -> {path}"
    )


@eval_app.command("dedupe")
def eval_dedupe(repo: str | None = None, verdict: bool = False) -> None:
    """Retrieval recall@k and the embedding-threshold baseline on the frozen set.

    `--verdict` adds the LLM duplicate verdict over the top 10 (paid calls,
    cached in the verdicts dir: a rerun costs nothing).
    """
    settings = get_settings()
    target = _target_repo(repo)
    evalset = dedupe.EvalSet.model_validate_json(_evalset_path(target).read_bytes())
    issues = {i.number: i for i in load_issues(settings.issues_dir, target)}
    with postgres.connect(settings.postgres_dsn) as conn:

        def rank(number: int) -> list[tuple[int, float]]:
            before = issues[number].created_at
            hits = vectors.nearest(conn, target, number, CANDIDATE_ISSUES, before)
            return [(h.number, h.score) for h in hits]

        ranked = dedupe.rank_all(evalset, rank)
    typer.echo(
        f"{target}, frozen {evalset.frozen_at:%Y-%m-%d}: "
        f"{len(evalset.pairs)} pairs, {len(evalset.negatives)} negatives"
    )
    records = _verdicts(target, ranked, issues) if verdict else None
    for name, cluster in [("strict", False), ("cluster", True)]:
        m = dedupe.score(ranked, cluster=cluster)
        recall = "  ".join(f"@{k} {r:.3f}" for k, r in m.recall_at.items())
        b = m.best
        typer.echo(
            f"  {name:<8} recall {recall}  |  threshold baseline t={b.value:.3f}: "
            f"precision {b.precision:.3f}, recall {b.recall:.3f}, F1 {b.f1:.3f}"
        )
        if records is not None:
            answers = {n: (r.verdict, r.target) for n, r in records.items()}
            v = dedupe.score_verdicts(ranked, answers, cluster=cluster)
            typer.echo(
                f"  {'':<8} LLM verdict ({v.predicted} duplicate_of): "
                f"precision {v.precision:.3f}, recall {v.recall:.3f}, F1 {v.f1:.3f}"
            )
    if records is not None:
        rows = list(records.values())
        dropped = sum(bool(r.dropped) for r in rows)
        typer.echo(
            f"  per issue: ${fmean(r.cost_usd for r in rows):.5f} mean, "
            f"{median(r.latency_ms for r in rows) / 1000:.1f} s median latency; "
            f"{dropped} of {len(rows)} verdicts failed the check"
        )


def _verdicts(
    repo: str, ranked: list[dedupe.Ranked], issues: dict[int, Issue]
) -> dict[int, verdict_mod.VerdictRecord]:
    """The checked LLM verdict for every eval issue, cached per candidate list."""
    jobs = [
        verdict_mod.Job(
            query=issues[r.query],
            candidates=[
                verdict_mod.Candidate.of(issues[n], issues[r.query])
                for n, _ in r.candidates
            ],
        )
        for r in ranked
    ]
    path = get_settings().verdicts_dir / f"{repo.replace('/', '-')}.json"
    judge = verdict_mod.OpenAIJudge(_openai_key())
    records, report = verdict_mod.judge_all(jobs, path, judge)
    typer.echo(f"  verdicts: {report.judged} judged, {report.cached} cached -> {path}")
    return records


@app.command()
def chunks(full: bool = False) -> None:
    """Parse and chunk the raw dir; print one line per chunk (or full text)."""
    count = 0
    docs: set[tuple[str, str]] = set()
    for c in chunk_docs(load_docs(get_settings().raw_dir)):
        count += 1
        docs.add((c.repo, c.path))
        where = f"{c.repo}/{c.path}#{c.ord}"
        typer.echo(f"{c.kind:<9} {len(c.text):>5}  {where}  {c.heading}")
        if full:
            typer.echo(f"{c.text}\n")
    typer.echo(f"{count} chunks from {len(docs)} documents")


def _openai_key() -> str:
    key = get_settings().openai_api_key
    if key is None:
        typer.echo("OPENAI_API_KEY is not set (see .env.example)", err=True)
        raise typer.Exit(code=1)
    return key.get_secret_value()


def _embedder() -> Embed:
    return openai_embedder(_openai_key())


@app.command()
def embed() -> None:
    """Embed the raw dir's chunks into Postgres; skip documents already stored."""
    settings = get_settings()
    chunked = chunk_docs(load_docs(settings.raw_dir))
    with postgres.connect(settings.postgres_dsn) as conn:
        report = postgres.sync(conn, chunked, _embedder())
    typer.echo(
        f"{report.embedded} documents embedded ({report.chunks} chunks), "
        f"{report.unchanged} unchanged, {report.removed} removed"
    )


@app.command()
def search(query: str, k: int = 5, full: bool = False) -> None:
    """Semantic search over the embedded chunks, best match first."""
    [vector] = _embedder()([query])
    with postgres.connect(get_settings().postgres_dsn) as conn:
        hits = postgres.search(conn, vector, k)
    for h in hits:
        typer.echo(f"{h.score:.3f}  {h.kind:<9} {h.repo}/{h.path}#{h.ord}  {h.heading}")
        if full:
            typer.echo(f"{h.text}\n")


def _show(record: ExtractedDoc) -> None:
    e = record.extraction
    typer.echo(f"{record.ref.kind:<9} {record.ref.repo}/{record.ref.path}")
    if e.status:
        typer.echo(f"  status        {e.status}")
    for name, values in [
        ("technologies", e.technologies),
        ("teams", e.teams),
        ("revises", [link.target for link in e.revises]),
        ("revisits", [link.target for link in e.revisits]),
        ("depends_on", [link.target for link in e.depends_on]),
        ("affects", e.affects),
    ]:
        if values:
            typer.echo(f"  {name:<13} {', '.join(values)}")
    for reason in record.dropped:
        typer.echo(f"  dropped       {reason}")


@app.command()
def extract() -> None:
    """Fill the domain schema per document with the LLM; skip unchanged ones.

    Needs `embed` first: revision candidates come from the vector store.
    """
    settings = get_settings()
    docs = load_docs(settings.raw_dir)
    with postgres.connect(settings.postgres_dsn) as conn:
        report = extract_sync(
            docs,
            settings.extracted_path,
            OpenAIExtractor(_openai_key()),
            lambda ref: postgres.similar_adrs(
                conn, (ref.repo, ref.path, ref.sha), CANDIDATES
            ),
            on_extracted=_show,
        )
    typer.echo(
        f"{report.extracted} documents extracted, {report.unchanged} unchanged, "
        f"{report.removed} removed, {report.dropped} links dropped "
        f"-> {settings.extracted_path}"
    )


def _graph() -> Driver:
    settings = get_settings()
    return kg.connect(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password.get_secret_value(),
    )


@app.command()
def graph() -> None:
    """Load the extraction records into Neo4j; skip documents already there."""
    settings = get_settings()
    docs = load_docs(settings.raw_dir)
    records = load_extracted(settings.extracted_path)
    with _graph() as driver:
        report = kg.sync(driver, docs, records)
    typer.echo(
        f"{report.written} documents written, {report.unchanged} unchanged, "
        f"{report.removed} removed"
    )


@app.command()
def decisions(project: str | None = None) -> None:
    """List ADRs: in force, or what revises them; plus where they're revisited."""
    with _graph() as driver:
        found = kg.decisions(driver, project)
    for d in found:
        verdict = "in force" if d.in_force else (d.status or "revised")
        typer.echo(f"{verdict:<11} {d.date or '?':<10}  {d.id}")
        for label, ids in [
            ("revised by", d.revised_by),
            ("revisits", d.revisits),
            ("revisited by", d.revisited_by),
        ]:
            for i in ids:
                typer.echo(f"{'':<23}  {label} {i}")
    in_force = sum(d.in_force for d in found)
    typer.echo(f"{in_force} of {len(found)} decisions in force")


@app.command()
def affected(adr: str) -> None:
    """Projects a decision touches, directly or through `depends_on` (repo:path)."""
    with _graph() as driver:
        found = kg.affected(driver, adr)
    if not found:
        typer.echo(f"no affected projects for {adr} (id is repo:path)", err=True)
        raise typer.Exit(code=1)
    for a in found:
        how = "named" if a.hops == 0 else f"{a.hops} hop{'s' * (a.hops > 1)}"
        typer.echo(f"{how:<8} {a.project}")


@app.command()
def mcp() -> None:
    """Serve search and the graph queries as MCP tools over stdio."""
    embed = _embedder()
    with postgres.connect(get_settings().postgres_dsn) as conn, _graph() as driver:
        # Read-only and long-lived: one transaction per query, none left open.
        conn.autocommit = True
        build_mcp(embed, conn, driver).run("stdio")
