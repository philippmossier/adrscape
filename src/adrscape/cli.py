"""Command-line entry point (`adrscape ...`)."""

from collections.abc import Callable
from pathlib import Path

import psycopg
import typer
from neo4j import GraphDatabase

from adrscape.config import get_settings
from adrscape.ingest.chunk import chunk_docs
from adrscape.ingest.fetch import fetch as fetch_source
from adrscape.ingest.parse import load_docs
from adrscape.ingest.source import LocalGitSource, Source

app = typer.Typer(no_args_is_help=True)


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
    """List the documents adrscape would ingest from local git clones."""
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
