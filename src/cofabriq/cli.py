"""Command-line entry point (`cofabriq ...`)."""

from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from statistics import fmean, median

import psycopg
import typer
from neo4j import Driver, GraphDatabase
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

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
from cofabriq.eval import readiness as readiness_eval
from cofabriq.eval import triage as triage_eval
from cofabriq.issues import triage as triage_mod
from cofabriq.issues import vectors
from cofabriq.issues import verdict as verdict_mod
from cofabriq.issues.fetch import fetch_created, fetch_issues, load_issues
from cofabriq.issues.github import GitHubIssueSource
from cofabriq.issues.model import Issue, check_repo
from cofabriq.mcp_server import build as build_mcp
from cofabriq.steps import Steps

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


def _date_range(value: str) -> tuple[date, date]:
    """`2022-01-01..2024-12-31`, GitHub search syntax."""
    try:
        start, end = (date.fromisoformat(part) for part in value.split(".."))
    except ValueError:
        msg = f"expected YYYY-MM-DD..YYYY-MM-DD, got {value!r}"
        raise typer.BadParameter(msg) from None
    if start > end:
        raise typer.BadParameter(f"{start} is after {end}")
    return start, end


@app.command("fetch-issues")
def fetch_issues_cmd(repo: str | None = None, created: str | None = None) -> None:
    """Copy a GitHub repo's issues into the issues dir, only what changed.

    `--created 2022-01-01..2024-12-31` fetches the issues created in that
    window instead (eval sets on large repos); the update cursor is untouched.
    """
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
    if created is not None:
        start, end = _date_range(created)
        report = fetch_created(source, settings.issues_dir, start, end)
        typer.echo(
            f"{report.written} written, {report.unchanged} unchanged "
            f"(created {start}..{end}) -> {settings.issues_dir}"
        )
        return
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
    settings = get_settings()
    issues = load_issues(settings.issues_dir, target)
    rules = dedupe.load_rules(settings.targets_dir, target)
    evalset, report = dedupe.build(issues, frozen_at=datetime.now(UTC), rules=rules)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(evalset.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(
        f"{report.pairs} pairs ({report.by_signal}) of {report.found} found, "
        f"{len(evalset.negatives)} negatives of {report.negative_pool}, from "
        f"{len(issues)} issues; dropped {report.ambiguous} ambiguous, "
        f"{report.unusable} unusable -> {path}"
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
                f"; {v.unsure} unsure, {v.unsure_right} of them right"
            )
    if records is not None:
        rows = list(records.values())
        dropped = sum(bool(r.dropped) for r in rows)
        typer.echo(
            f"  per issue: ${fmean(r.cost_usd for r in rows):.5f} mean, "
            f"{median(r.latency_ms for r in rows) / 1000:.1f} s median latency; "
            f"{dropped} of {len(rows)} verdicts failed the check"
        )


def _infoset_path(repo: str, test: bool) -> Path:
    suffix = "-test" if test else ""
    return EVALS_DIR / f"readiness-{repo.replace('/', '-')}{suffix}.json"


@eval_app.command("readiness-set")
def readiness_set(
    repo: str | None = None, test: bool = False, force: bool = False
) -> None:
    """Build and freeze the missing-info eval set: maintainer labels from the
    fetched issues, each sampled issue's history checked on GitHub. `--test`
    freezes the held-out split (`test_window`) instead."""
    settings = get_settings()
    target = _target_repo(repo)
    path = _infoset_path(target, test)
    if path.exists() and not force:
        typer.echo(f"{path} is frozen; --force to rebuild it", err=True)
        raise typer.Exit(code=1)
    rules = readiness_eval.load_rules(settings.targets_dir, target)
    if rules is None or settings.github_token is None:
        typer.echo("needs [readiness] in the target file and GITHUB_TOKEN", err=True)
        raise typer.Exit(code=1)
    if test:
        if rules.test_window is None:
            typer.echo("no test_window in [readiness]", err=True)
            raise typer.Exit(code=1)
        rules = rules.model_copy(update={"window": rules.test_window})
    pool = readiness_eval.pools(load_issues(settings.issues_dir, target), rules)
    source = GitHubIssueSource(target, settings.github_token.get_secret_value())
    infoset = readiness_eval.build(
        target, pool, rules, source.history, frozen_at=datetime.now(UTC)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(infoset.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(
        f"{len(infoset.needs_info)} needs-info of {len(pool.needs_info)}, "
        f"{len(infoset.complete)} complete of {len(pool.complete)} -> {path}"
    )


@eval_app.command("readiness")
def eval_readiness(repo: str | None = None, test: bool = False) -> None:
    """Does the card ask for more info exactly when a maintainer did? The
    checked required items vs the model's own questions, on the frozen set.

    Paid: one card call per issue, cached in the readiness dir. `--test`
    scores the held-out split: once, after the method is chosen.
    """
    settings = get_settings()
    target = _target_repo(repo)
    infoset = readiness_eval.InfoSet.model_validate_json(
        _infoset_path(target, test).read_bytes()
    )
    wanted = set(infoset.needs_info) | set(infoset.complete)
    issues = {
        i.number: i
        for i in load_issues(settings.issues_dir, target)
        if i.number in wanted
    }
    path = settings.readiness_dir / f"{target.replace('/', '-')}.json"
    triager = triage_mod.OpenAITriager(_openai_key())
    records = readiness_eval.run_fields(list(issues.values()), triager, path)
    checked = {
        n: bool(readiness_eval.missing(r, issues[n])) for n, r in records.items()
    }
    asked = {n: bool(r.fields.missing_info) for n, r in records.items()}
    blocking = {
        n: r.fields.blocking and bool(r.fields.missing_info) for n, r in records.items()
    }
    card = {n: checked[n] or blocking[n] for n in records}
    typer.echo(
        f"{target}, frozen {infoset.frozen_at:%Y-%m-%d}: "
        f"{len(infoset.needs_info)} needs-info, {len(infoset.complete)} complete"
    )
    methods = [
        ("model asks anything", asked),
        ("checked items", checked),
        ("blocking questions", blocking),
        ("card (either)", card),
    ]
    for name, asks in methods:
        s = readiness_eval.score(infoset, asks)
        typer.echo(
            f"  {name:<20} needs_info on {s.predicted}: precision "
            f"{s.precision:.3f}, recall {s.recall:.3f}, F1 {s.f1:.3f}"
        )
    rows = list(records.values())
    types = Counter(r.fields.type for r in rows)
    typer.echo(f"  types: {dict(types.most_common())}")
    typer.echo(
        f"  per issue: ${fmean(r.cost_usd for r in rows):.5f} mean, "
        f"{median(r.latency_ms for r in rows) / 1000:.1f} s median latency"
    )


@eval_app.command("triage")
def eval_triage(
    repo: str | None = None, seed: Path = Path("playground/seed.toml")
) -> None:
    """Type, component, duplicate and injection checks on the seeded issues.

    Paid: two LLM calls per seeded issue, nothing cached. Needs the seeded
    issues fetched and embedded, and the repo's docs embedded.
    """
    settings = get_settings()
    target = _target_repo(repo)
    items = triage_eval.load_seed(seed)
    matched = triage_eval.match(items, load_issues(settings.issues_dir, target))
    with postgres.connect(settings.postgres_dsn) as conn:
        conn.autocommit = True  # one connection shared by the threads, reads only
        run = partial(_triage_fn(conn, refresh=False), target)
        with ThreadPoolExecutor(max_workers=4) as pool:
            found = pool.map(run, [i.number for i in matched.values()])
            cards = dict(zip(matched, found, strict=True))
    numbers = {k: i.number for k, i in matched.items()}
    s = triage_eval.score(items, cards, numbers)
    runs = [c.run for c in cards.values()]
    typer.echo(f"{target}: {s.n} seeded issues ({seed})")
    typer.echo(f"  type       {s.type_ok}/{s.n}")
    typer.echo(f"  component  {s.component_ok}/{s.n}")
    typer.echo(
        f"  duplicate  {s.duplicates_found}/{s.duplicates} found, "
        f"{s.false_duplicates} false duplicate_of"
    )
    typer.echo(f"  injections followed {s.injections_followed}/{s.injections}")
    typer.echo(
        f"  conflicts  {s.conflicts} card-verdict, "
        f"{s.conflicts_on_type_miss} on a wrong type"
    )
    typer.echo(
        f"  per issue: ${fmean(r.cost_usd for r in runs):.5f} mean, "
        f"{median(r.latency_ms for r in runs) / 1000:.1f} s median latency"
    )
    for miss in s.misses:
        typer.echo(f"  miss  {miss}")


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


def _triage_fn(
    conn: postgres.Conn, *, refresh: bool, steps: Steps | None = None
) -> Callable[[str, int], triage_mod.TriageCard]:
    """(repo, number) -> card: optionally refresh the issue from GitHub, then
    gather from the local stores and make both LLM calls.

    `refresh` needs `GITHUB_TOKEN`; without one the store is used as it is.
    The eval passes False: it scores the issues as fetched. `steps` reports
    each stage (CLI); None stays silent (MCP, eval).
    """
    settings = get_settings()
    key = _openai_key()
    triager, judge = triage_mod.OpenAITriager(key), verdict_mod.OpenAIJudge(key)
    embed = openai_embedder(key)
    token = settings.github_token if refresh else None
    show = steps or Steps(None)

    def run(repo: str, number: int) -> triage_mod.TriageCard:
        if token is not None:
            source = GitHubIssueSource(
                repo, token.get_secret_value(), comments=settings.issue_comments
            )
            with show.step("GitHub", f"checking #{number}") as done:
                got = triage_mod.refresh_issue(source, settings.issues_dir, number)
                done(_fetched(number, got))
            if got is not None:
                with show.step("embedding", f"embedding #{number}") as done:
                    changed = triage_mod.refresh_vector(conn, got[0], embed)
                    done(f"embedded #{number}" if changed else "up to date")
        elif refresh:
            show.note("GitHub", "skipped: no GITHUB_TOKEN, local copy", ok=False)
        with show.step("search", "nearest earlier issues and docs") as done:
            ctx = triage_mod.gather(
                conn, settings.issues_dir, settings.targets_dir, repo, number
            )
            done(f"{len(ctx.candidates)} earlier issues, {len(ctx.related_docs)} docs")
        with show.step("LLM", "card fields + duplicate verdict") as done:
            card = triage_mod.triage(ctx, triager, judge)
            seconds = card.run.latency_ms / 1000
            done(f"card fields + duplicate verdict, 2 calls, {seconds:.1f} s")
        show.note("checks", _checks(card), ok=not (card.dropped or card.conflicts))
        return card

    return run


def _fetched(number: int, got: tuple[object, str] | None) -> str:
    if got is None:
        return f"#{number} not on GitHub (or a pull request), local copy"
    return {
        "new": f"fetched #{number} (new)",
        "changed": f"fetched #{number} (changed on GitHub)",
    }.get(got[1], f"#{number} unchanged since the last fetch")


def _checks(card: triage_mod.TriageCard) -> str:
    """What the plain-code checks after the calls let through or dropped."""
    parts: list[str] = []
    if card.component:
        parts.append("component on the list")
    if card.duplicate.target is not None:
        parts.append(f"quote found in #{card.duplicate.target}")
    if card.dropped:
        parts.append(f"dropped {len(card.dropped)}")
    if card.conflicts:
        parts.append(f"{len(card.conflicts)} conflict")
    if card.missing:
        parts.append(f"{len(card.missing)} info missing")
    return ", ".join(parts) or "nothing to check"


@app.command()
def triage(number: int, repo: str | None = None, json: bool = False) -> None:
    """Triage card for one issue (two LLM calls). Fetches and embeds the issue
    first if it is new or changed on GitHub (needs GITHUB_TOKEN)."""
    target = _target_repo(repo)
    with postgres.connect(get_settings().postgres_dsn) as conn:
        try:
            steps = Steps(Console(stderr=True, soft_wrap=True))
            card = _triage_fn(conn, refresh=True, steps=steps)(target, number)
        except triage_mod.NotReady as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    if json:
        typer.echo(card.model_dump_json(indent=2))
    else:
        _show_card(card)


def _show_card(card: triage_mod.TriageCard) -> None:
    """The card for a human: a label column and a value column, long values
    wrapped at word boundaries under their value. Issue and LLM text goes in as
    `Text`, which rich never parses as markup (`[red]` in an issue body prints
    as text). Piped, lines are not wrapped at all."""
    out = Console(highlight=False)
    if out.is_terminal:
        _render_card(out, card)
        return
    # Piped: no wrapping, and no grid padding at the end of lines.
    wide = Console(highlight=False, width=10_000)
    with wide.capture() as captured:
        _render_card(wide, card)
    typer.echo("\n".join(line.rstrip() for line in captured.get().splitlines()))


def _render_card(out: Console, card: triage_mod.TriageCard) -> None:
    i, d, r = card.issue, card.duplicate, card.run
    rows: list[tuple[str, Text]] = []

    def row(label: str, value: str, style: str = "bold") -> None:
        rows.append((label, Text(value, style=style)))

    def flush() -> None:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="dim", width=13, no_wrap=True)
        grid.add_column()
        for label, value in rows:
            grid.add_row(label, value)
        out.print(Padding(grid, (0, 0, 0, 2), expand=False))
        rows.clear()

    out.print()
    out.print(Text.assemble((f"{i.repo}#{i.number}", "bold"), f" ({i.state})"))
    out.print(Padding(Text(i.url, style="dim"), (0, 0, 0, 2), expand=False))
    out.print(Padding(Text(card.summary), (0, 0, 0, 2), expand=False))
    row("type", card.type)
    if card.severity:
        row("severity", card.severity)
    row("component", card.component or "-")
    for n, step in enumerate(card.repro_steps, 1):
        row("repro" if n == 1 else "", f"{n}. {step}", "default")
    if card.missing:
        row("missing", ", ".join(card.missing), "yellow")
    for n, q in enumerate(card.missing_info):
        row("questions" if n == 0 else "", q, "default")
    target = f" #{d.target}" if d.target else ""
    style = {"duplicate_of": "bold yellow", "unsure": "yellow", "related": "cyan"}.get(
        d.verdict, "dim"
    )
    row("duplicate", f"{d.verdict}{target}", style)
    if d.evidence:
        row("", f'"{d.evidence}"', "italic")
    ready = card.readiness
    style = {"ready": "bold green", "needs_info": "bold yellow"}.get(
        ready.route, "bold"
    )
    row("route", ready.route, style)
    for reason in ready.reasons:
        row("", reason, "default")
    for n, line in enumerate((card.reply or "").splitlines()):
        row("reply draft" if n == 0 else "", line, "italic")
    flush()
    # Two separate searches (issues, doc sections); scores are cosine
    # similarity, not probabilities.
    out.print(Text("  related issues (similarity)", style="dim"))
    for x in card.related_issues:
        row("", f"{x.score:.2f}  #{x.number}", "default")
    if not card.related_issues:
        row("", "-", "default")
    flush()
    out.print(Text("  related docs (similarity)", style="dim"))
    clone = i.repo.split("/")[1] + "/"  # docs are keyed by the clone's folder
    for doc in card.related_docs:
        source = doc.source.removeprefix(clone)
        # A long heading trail is cut at the edge, not wrapped.
        rows.append(
            ("", Text(f"{doc.score:.2f}  {source}", no_wrap=True, overflow="ellipsis"))
        )
    if not card.related_docs:
        row("", "-", "default")
    flush()
    for reason in card.dropped:
        row("dropped", reason, "red")
    for reason in card.conflicts:
        row("conflict", reason, "yellow")
    row("run", f"${r.cost_usd:.4f}, {r.latency_ms / 1000:.1f} s, {r.model}", "dim")
    flush()


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
        # Long-lived: one transaction per query, none left open. Writes only
        # when `triage` refreshes an issue (its own transaction).
        conn.autocommit = True
        target = get_settings().target_repo
        run = _triage_fn(conn, refresh=True)
        build_mcp(embed, conn, driver, run, default_repo=target).run("stdio")
