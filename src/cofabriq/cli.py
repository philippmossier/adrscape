"""Command-line entry point (`cofabriq ...`)."""

import json as json_mod
import time
import webbrowser
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from statistics import fmean, median

import click
import httpx
import psycopg
import typer
from neo4j import Driver, GraphDatabase
from pydantic import TypeAdapter
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
from cofabriq.eval import gate as gate_eval
from cofabriq.eval import human as human_eval
from cofabriq.eval import impact as impact_eval
from cofabriq.eval import loop as loop_eval
from cofabriq.eval import readiness as readiness_eval
from cofabriq.eval import refine as refine_eval
from cofabriq.eval import review as review_eval
from cofabriq.eval import triage as triage_eval
from cofabriq.eval import witness as witness_eval
from cofabriq.eval.bugs import (
    PlantedBug,
    fix_path,
    load_bugs,
    planted_for,
    repro_path,
)
from cofabriq.issues import triage as triage_mod
from cofabriq.issues import vectors
from cofabriq.issues import verdict as verdict_mod
from cofabriq.issues.fetch import fetch_created, fetch_issues, load_issue, load_issues
from cofabriq.issues.github import GitHubIssueSource
from cofabriq.issues.model import Issue, check_repo
from cofabriq.loop import gate as gate_mod
from cofabriq.loop import pr as pr_mod
from cofabriq.loop import publish as publish_mod
from cofabriq.loop import refine as refine_mod
from cofabriq.loop import run as run_mod
from cofabriq.loop.engine import AgentError, ApiKey, Auth, ClaudeCode, Subscription
from cofabriq.loop.sandbox import Podman
from cofabriq.map import build as map_build_mod
from cofabriq.map import components as components_mod
from cofabriq.map import fixes as fixes_mod
from cofabriq.map import history
from cofabriq.map import impact as impact_mod
from cofabriq.map import store as map_store
from cofabriq.mcp_server import build as build_mcp
from cofabriq.steps import Steps

app = typer.Typer(no_args_is_help=True)
eval_app = typer.Typer(no_args_is_help=True, help="Measure retrieval and verdicts.")
app.add_typer(eval_app, name="eval")
map_app = typer.Typer(no_args_is_help=True, help="The repo map: components, co-change.")
app.add_typer(map_app, name="map")
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


@map_app.command("build")
def map_build(clone: Path, repo: str | None = None, until: str | None = None) -> None:
    """Build the repo map from a local clone's history into Neo4j, replacing
    that repo's map. `--until 2022-12-31`: the map as of that day (evals must
    not see the fixes they predict). A clone without file contents is
    enough: `git clone --filter=blob:none --no-checkout <url>`."""
    target = _target_repo(repo)
    day = date.fromisoformat(until) if until else None
    rules = components_mod.load_rules(get_settings().targets_dir, target)
    rev = history.rev_at(clone, day)
    m = map_build_mod.build(
        history.files_at(clone, rev), history.commits(clone, rev), rules
    )
    fixed = fixes_mod.fixes(clone, rev, target)
    rows = map_store.fix_rows(fixed, rules, set(m.files.values()))
    with _graph() as driver:
        report = map_store.write(driver, target, rev, day, m, rows)
    typer.echo(
        f"{target} at {rev[:10]} ({until or 'head'}): {report.components} "
        f"components, {report.files} files; {m.used} commits counted, "
        f"{m.skipped} skipped (over {map_build_mod.MAX_FILES} files); "
        f"{report.component_pairs} component pairs, {report.file_pairs} file "
        f"pairs, {report.fixes} fix commits"
    )


@map_app.command("show")
def map_show(
    repo: str | None = None, component: str | None = None, k: int = 10
) -> None:
    """The map's components (most changed first), or one component's
    strongest co-change partners."""
    target = _target_repo(repo)
    with _graph() as driver:
        if component is None:
            for c in map_store.components(driver, target)[:k]:
                typer.echo(f"{c.commits:>6} commits  {c.files:>5} files  {c.name}")
            return
        for p in map_store.changes_with(driver, target, component, k):
            typer.echo(f"{p.n:>6} together  {p.share:>5.0%}  {p.name}")


def _clone(repo: str) -> Path:
    return Path("data/clones") / repo.replace("/", "-")


def _impact_path(repo: str, test: bool) -> Path:
    suffix = "-test" if test else ""
    return EVALS_DIR / f"impact-{repo.replace('/', '-')}{suffix}.json"


@eval_app.command("impact-set")
def impact_set(
    repo: str | None = None, test: bool = False, force: bool = False
) -> None:
    """Freeze the impact eval set: issues closed by a PR or commit, and the
    components their fix commits touched (from the clone in data/clones/).
    `--test` freezes the held-out split."""
    settings = get_settings()
    target = _target_repo(repo)
    path = _impact_path(target, test)
    if path.exists() and not force:
        typer.echo(f"{path} is frozen; --force to rebuild it", err=True)
        raise typer.Exit(code=1)
    rules = impact_eval.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo("needs [impact] in the target file", err=True)
        raise typer.Exit(code=1)
    clone = _clone(target)
    fixed = fixes_mod.fixes(clone, history.rev_at(clone, None), target)
    impact, pool = impact_eval.build(
        load_issues(settings.issues_dir, target),
        fixed,
        components_mod.load_rules(settings.targets_dir, target),
        rules,
        rules.test_window if test else rules.window,
        frozen_at=datetime.now(UTC),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(impact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    typer.echo(f"{len(impact.cases)} cases of {pool} -> {path}")


@eval_app.command("impact")
def eval_impact(repo: str | None = None, test: bool = False) -> None:
    """Which components will the fix touch, from the issue text alone?
    Popularity vs kNN over earlier fixed issues vs kNN plus co-change, as
    hit@k and recall@k. Free: embeddings and the clone, no LLM. The map is
    built in memory at the rules' `map_until`, so the Neo4j map can stay
    current for `impact`."""
    settings = get_settings()
    target = _target_repo(repo)
    impact = impact_eval.ImpactSet.model_validate_json(
        _impact_path(target, test).read_bytes()
    )
    until = impact.rules.map_until.isoformat()
    components = components_mod.load_rules(settings.targets_dir, target)
    clone = _clone(target)
    cut_rev = history.rev_at(clone, impact.rules.map_until)
    repo_map = map_build_mod.build(
        history.files_at(clone, cut_rev), history.commits(clone, cut_rev), components
    )
    popular = repo_map.popular()
    fixed = fixes_mod.fixes(clone, history.rev_at(clone, None), target)
    issues = {i.number: i for i in load_issues(settings.issues_dir, target)}
    cache: dict[str, list[tuple[str, float]]] = {}

    def partners(component: str) -> list[tuple[str, float]]:
        if component not in cache:
            cache[component] = repo_map.partners(component, 10)
        return cache[component]

    methods: dict[str, dict[int, list[str]]] = {
        "popularity": {},
        "kNN": {},
        "kNN + co-change": {},
        "kNN fixed": {},
        "kNN fixed + co-ch.": {},
    }
    empty = {"kNN": 0, "kNN fixed": 0}  # no neighbour had a fix: popularity only
    first_fix = {n: min(c.at for c in commits) for n, commits in fixed.items()}
    with postgres.connect(settings.postgres_dsn) as conn:
        for case in impact.cases:
            issue = issues[case.issue]
            filed = issue.created_at
            known = [n for n, at in first_fix.items() if at < filed]
            for name, among in [("kNN", None), ("kNN fixed", known)]:
                hits = vectors.nearest(
                    conn, target, issue.number, impact_eval.NEIGHBOURS, filed, among
                )
                ranked = impact_eval.knn(
                    [(h.number, h.score) for h in hits], filed, fixed, components
                )
                empty[name] += not ranked
                spread = impact_mod.spread(ranked, partners)
                with_co = "kNN + co-change" if among is None else "kNN fixed + co-ch."
                methods[name][case.issue] = impact_eval.fill(ranked, popular)
                methods[with_co][case.issue] = impact_eval.fill(spread, popular)
            methods["popularity"][case.issue] = popular
    sizes = [len(c.components) for c in impact.cases]
    start, end = impact.window
    typer.echo(
        f"{target} {start}..{end}, map cut {until}: {len(impact.cases)} issues, "
        f"{fmean(sizes):.1f} components touched on average, "
        f"{len(popular)} in the map"
    )
    ks = (1, 3, 5, 10)
    for name, predicted in methods.items():
        s = impact_eval.score(impact.cases, predicted, ks)
        hit = "  ".join(f"@{k} {v:.3f}" for k, v in s.hit_at.items())
        recall = "  ".join(f"@{k} {v:.3f}" for k, v in s.recall_at.items())
        typer.echo(f"  {name:<18} hit {hit}  |  recall {recall}")
    typer.echo(
        "  no evidence (popularity only): "
        + ", ".join(f"{name} {n}/{len(impact.cases)}" for name, n in empty.items())
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


NEIGHBOURS = 30  # earlier fixed issues voting in `impact`, as measured


def _impact_fn(
    conn: postgres.Conn, driver: Driver, embed: Embed
) -> Callable[[str, int | None, str | None, int], impact_mod.Impact]:
    """(repo, issue number or None, plan text or None, k) -> Impact. An issue
    must be fetched and embedded; it only sees fixes committed before it was
    filed. A plan sees everything on the map."""

    def run(
        repo: str, number: int | None, plan: str | None, k: int
    ) -> impact_mod.Impact:
        rev = map_store.rev(driver, repo)
        if rev is None:
            raise triage_mod.NotReady(f"no map for {repo}: run map build")
        if number is not None:
            issue = load_issue(get_settings().issues_dir, repo, number)
            if issue is None or vectors.embedding(conn, repo, number) is None:
                raise triage_mod.NotReady(
                    f"{repo}#{number} is not fetched and embedded: run triage first"
                )
            before = issue.created_at
            among = map_store.fixed_issues(driver, repo, before)
            hits = vectors.nearest(conn, repo, number, NEIGHBOURS, before, among)
        elif plan is not None:
            before = None
            among = map_store.fixed_issues(driver, repo, None)
            [vector] = embed([plan[: vectors.MAX_BODY]])
            hits = vectors.nearest_to(conn, repo, vector, NEIGHBOURS, among)
        else:
            raise ValueError("an issue number or a plan")
        neighbours = [(h.number, h.score) for h in hits]
        fixed = map_store.fixed(driver, repo, [n for n, _ in neighbours], before)

        def partners(component: str) -> list[tuple[str, float]]:
            found = map_store.changes_with(driver, repo, component, 10)
            return [(p.name, p.share) for p in found]

        return impact_mod.assemble(repo, rev, neighbours, fixed, partners, k)

    return run


@app.command()
def impact(
    number: int | None = typer.Argument(None),
    plan: Path | None = None,
    repo: str | None = None,
    k: int = 8,
    json: bool = False,
) -> None:
    """Components a fix for issue <number> (or a plan, `--plan plan.md`) will
    likely touch: earlier similar issues vote with what their fixes touched,
    co-change on the map adds the blast radius. Needs `map build`."""
    if (number is None) == (plan is None):
        typer.echo("pass an issue number or --plan, not both", err=True)
        raise typer.Exit(code=1)
    target = _target_repo(repo)
    text = plan.read_text("utf-8") if plan else None
    with _graph() as driver, postgres.connect(get_settings().postgres_dsn) as conn:
        try:
            result = _impact_fn(conn, driver, _embedder())(target, number, text, k)
        except triage_mod.NotReady as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
    if json:
        typer.echo(result.model_dump_json(indent=2))
        return
    subject = f"#{number}" if number is not None else str(plan)
    typer.echo(f"{target} {subject}, map at {result.map_rev[:10]}")
    if not result.components:
        typer.echo("  no earlier issue with a fix on the map: no evidence to go on")
    for t in result.components:
        why = ", ".join(f"#{n}" for n in t.via[:4])
        typer.echo(f"  {t.score:>6.2f}  {t.component}  (fixes of {why})")
    if result.blast:
        typer.echo("  usually changes with them (blast radius):")
    for t in result.blast:
        typer.echo(f"  {t.score:>6.2f}  {t.component}")


def _agent() -> ClaudeCode:
    settings = get_settings()
    auth: Auth
    if settings.agent_auth == "subscription":
        if settings.claude_code_oauth_token is None:
            typer.echo(
                "CLAUDE_CODE_OAUTH_TOKEN is not set (`claude setup-token`)", err=True
            )
            raise typer.Exit(code=1)
        auth = Subscription(settings.claude_code_oauth_token.get_secret_value())
        typer.echo("agent: your Claude plan; costs shown are API list prices", err=True)
    elif settings.anthropic_api_key is None:
        typer.echo("ANTHROPIC_API_KEY is not set (see .env.example)", err=True)
        raise typer.Exit(code=1)
    else:
        auth = ApiKey(settings.anthropic_api_key.get_secret_value())
    return ClaudeCode(
        auth=auth,
        model=settings.agent_model,
        effort=settings.agent_effort,
    )


@app.command()
def refine(number: int, repo: str | None = None, json: bool = False) -> None:
    """Spec for one issue from a read-only agent reading the code (paid, one
    agent run). Updates the clone in data/clones/ first and fetches the issue
    if it is new or changed on GitHub (needs GITHUB_TOKEN)."""
    settings = get_settings()
    target = _target_repo(repo)
    agent = _agent()
    steps = Steps(Console(stderr=True, soft_wrap=True))
    clone = settings.clones_dir / target.replace("/", "-")
    with steps.step("clone", f"updating {clone}") as done:
        commit = refine_mod.update_clone(target, clone)
        done(f"{clone} at {commit[:7]}")
    issue = _refresh_issue(target, number, steps)
    if issue is None:
        typer.echo(f"#{number} is not in {settings.issues_dir}", err=True)
        raise typer.Exit(code=1)
    with steps.step("agent", f"reading the code ({agent.model})") as done:
        try:
            refined = refine_mod.refine(issue, clone, agent)
        except AgentError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc
        r = refined.run
        done(f"{r.turns} turns, {r.latency_ms / 1000:.0f} s, ${r.cost_usd:.3f}")
    if refined.unknown_paths:
        steps.note(
            "paths",
            f"not in {commit[:7]}: {', '.join(refined.unknown_paths)}",
            ok=False,
        )
    else:
        steps.note("paths", "all in the commit")
    if json:
        typer.echo(refined.model_dump_json(indent=2))
    else:
        _show_spec(refined)


def _refresh_issue(repo: str, number: int, steps: Steps) -> Issue | None:
    """The stored issue, refreshed from GitHub first if there is a token."""
    settings = get_settings()
    token = settings.github_token
    if token is None:
        steps.note("GitHub", "skipped: no GITHUB_TOKEN, local copy", ok=False)
        return load_issue(settings.issues_dir, repo, number)
    source = GitHubIssueSource(
        repo, token.get_secret_value(), comments=settings.issue_comments
    )
    with steps.step("GitHub", f"checking #{number}") as done:
        got = triage_mod.refresh_issue(source, settings.issues_dir, number)
        done(_fetched(number, got))
    return got[0] if got is not None else load_issue(settings.issues_dir, repo, number)


def _show_spec(refined: refine_mod.Refined) -> None:
    """The spec for the dev at the plan gate. Agent text goes in as `Text`
    (never parsed as markup). Piped, lines are not wrapped (as the card)."""
    out = Console(highlight=False)
    if out.is_terminal:
        _render_spec(out, refined)
        return
    wide = Console(highlight=False, width=10_000)
    with wide.capture() as captured:
        _render_spec(wide, refined)
    typer.echo("\n".join(line.rstrip() for line in captured.get().splitlines()))


def _render_spec(out: Console, refined: refine_mod.Refined) -> None:
    s = refined.spec

    def section(title: str, lines: list[str]) -> None:
        out.print(Text(title, style="bold"))
        for line in lines or ["(none)"]:
            out.print(Padding(Text(line), (0, 0, 0, 2)))

    out.print(
        Text(
            f"{refined.repo}#{refined.number} at {refined.commit[:7]}, size {s.size}",
            style="bold",
        )
    )
    section("problem", [s.problem])
    section("files", [f"{f.path}: {f.why}" for f in s.files])
    section("tests", [f"{f.path}: {f.why}" for f in s.tests])
    section("plan", [f"{n}. {step}" for n, step in enumerate(s.plan, 1)])
    section("assumptions", s.assumptions)
    section("open questions", s.open_questions)


@app.command()
def run(
    number: int,
    repo: str = "philippmossier/local-ai-chat-playground",
    spec: Path | None = None,
    yes: bool = False,
    keep: bool = False,
    planted: bool = False,
    apply: bool = False,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """The core loop for one issue: refine, plan gate, the agent writes a
    failing test, then fixes the code, repro and checks in a container, max
    3 attempts each (paid). On a planted logic bug the planted repro judges
    the result afterwards; `--planted` hands it to the agent instead.
    `--spec` takes a `refine --json` file instead of a new refine run (the
    dev may edit it); `--yes` skips the plan gate; `--keep` leaves the
    worktree in data/work/. When green, a reviewer reads the change and the
    PR body goes to data/runs/ with the result; `--apply` also pushes a new
    branch and opens a draft PR (playground only, GITHUB_WRITE_TOKEN)."""
    settings = get_settings()
    target = check_repo(repo)
    rules = run_mod.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo(f"no [loop] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    write_token = _write_token(target) if apply else None  # before paying
    agent = _agent()
    steps = Steps(Console(stderr=True, soft_wrap=True))
    clone = settings.clones_dir / target.replace("/", "-")
    with steps.step("clone", f"updating {clone}") as done:
        commit = refine_mod.update_clone(target, clone)
        done(f"{clone} at {commit[:7]}")
    issue = _refresh_issue(target, number, steps)
    if issue is None:
        typer.echo(f"#{number} is not in {settings.issues_dir}", err=True)
        raise typer.Exit(code=1)
    bug = planted_for(issue.title, load_bugs(bugs, seed), seed)
    if bug is not None and bug.kind != "logic":
        bug = None  # a UI bug's repro is Playwright: not run by the loop yet
    if planted and bug is None:
        typer.echo(f"#{number} is not a planted logic bug: no repro", err=True)
        raise typer.Exit(code=1)
    if bug is not None:
        role = "handed to the agent" if planted else "judges the result"
        steps.note("repro", f"planted {bug.key} ({bug.repro}) {role}")
    if spec is not None:
        refined = refine_mod.Refined.model_validate_json(spec.read_text("utf-8"))
        if (refined.repo, refined.number) != (target, number):
            typer.echo(f"{spec} is for {refined.repo}#{refined.number}", err=True)
            raise typer.Exit(code=1)
        steps.note("spec", f"from {spec} (at {refined.commit[:7]})")
    else:
        with steps.step("refine", f"reading the code ({agent.model})") as done:
            try:
                refined = refine_mod.refine(issue, clone, agent)
            except AgentError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1) from exc
            r = refined.run
            done(f"{r.turns} turns, {r.latency_ms / 1000:.0f} s, ${r.cost_usd:.3f}")
    _show_spec(refined)
    if not yes and not typer.confirm("Approve the plan and let the agent edit?"):
        raise typer.Exit(code=1)
    worktree = settings.work_dir / f"{target.replace('/', '-')}-{number}"
    sandbox = Podman(image=rules.image, setup=rules.setup)
    with steps.step("loop", f"repro, fix, checks in {worktree}") as done:
        try:
            result = run_mod.run_loop(
                repo=target,
                number=number,
                commit=refined.commit,
                spec=refined.spec,
                clone=clone,
                worktree=worktree,
                repro_file=repro_path(bugs, bug) if planted and bug else None,
                rules=rules,
                agent=agent.editing(budget_usd=1.0),
                sandbox=sandbox,
            )
            # the review gate on the green tree, before the judge resets it:
            # review, blockers proven by a test, fixed, reviewed again
            gated = None
            if result.status == "green":
                gated = gate_mod.review_gate(
                    spec=refined.spec,
                    rules=rules,
                    result=result,
                    worktree=worktree,
                    reader=agent,
                    editor=agent.editing(budget_usd=1.0),
                    sandbox=sandbox,
                )
            # no verdicts by default: an issue without a planted bug has no
            # answer key, and with --planted the agent already had the repro
            judged = loop_eval.Judged(result=result)
            if bug is not None and not planted:
                if keep:  # validity resets the code: keep the fix to look at
                    judged.judge = loop_eval.judge(
                        worktree, rules, repro_path(bugs, bug), sandbox
                    )
                else:
                    judged = loop_eval.against_key(
                        result,
                        worktree,
                        rules,
                        repro_path(bugs, bug),
                        fix_path(bugs, bug),
                        sandbox,
                    )
        finally:
            if not keep:
                run_mod.remove_worktree(clone, worktree)
        tries = len(result.attempts)
        done(f"{result.status}, {tries} attempts, ${result.cost_usd:.3f}")
    _show_loop(judged)
    stem = f"{target.replace('/', '-')}-{number}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    out = settings.runs_dir / f"{stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    record = judged.model_dump(mode="json")
    if gated is not None:
        record["gate"] = gated.model_dump(mode="json")
        _show_gate(gated)
    if gated is not None and gated.status != "blocked":
        draft = pr_mod.draft(
            issue_title=issue.title,
            spec=refined.spec,
            result=gated.result,
            tests=rules.tests,
            risk_rules=pr_mod.load_risk(settings.targets_dir, target),
            components=components_mod.load_rules(settings.targets_dir, target),
            review=gated.review,
            rounds=gated.rounds,
        )
        record["draft"] = draft.model_dump(mode="json")
        body = settings.runs_dir / f"{stem}.pr.md"
        body.write_text(draft.body, "utf-8")
        _show_draft(draft)
        if write_token is None:
            typer.echo(f"-> {body} (dry run: nothing sent to GitHub)")
        else:
            typer.echo(f"-> {body}")
            record["published"] = _publish(  # a PR or the error: the run is kept
                target, clone, issue, gated.result, draft, write_token, steps
            )
    out.write_text(json_mod.dumps(record, indent=2), "utf-8")
    typer.echo(f"-> {out}")
    blocked = gated is not None and gated.status == "blocked"
    judged_red = judged.judge is not None and not judged.judge.ok
    if result.status != "green" or blocked or judged_red:
        raise typer.Exit(code=2)


def _write_token(repo: str) -> str:
    """The write token for `--apply`, after the repo check."""
    try:
        publish_mod.check_writable(repo)
    except publish_mod.PublishError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    token = get_settings().github_write_token
    if token is None:
        typer.echo("GITHUB_WRITE_TOKEN is not set (see .env.example)", err=True)
        raise typer.Exit(code=1)
    return token.get_secret_value()


def _publish(
    repo: str,
    clone: Path,
    issue: Issue,
    result: run_mod.LoopResult,
    draft: pr_mod.Draft,
    token: str,
    steps: Steps,
) -> dict[str, object]:
    """The PR as a dict, or `{"error": ...}` (the run is saved either way)."""
    try:
        with steps.step("publish", f"branch + draft PR on {repo}") as done:
            published = publish_mod.publish(
                repo=repo,
                clone=clone,
                base_commit=result.commit,
                diff=result.diff,
                number=result.number,
                issue_title=issue.title,
                title=draft.title,
                body=draft.body,
                token=token,
                pulls=publish_mod.GitHubPulls(token),
            )
            done(f"{published.url} ({published.branch})")
    except (publish_mod.PublishError, httpx.HTTPError) as exc:
        typer.echo(str(exc), err=True)
        return {"error": str(exc)}
    return published.model_dump(mode="json")


def _indented(out: Console, text: Text) -> None:
    """Agent text under a trace line, wrapped: on a `soft_wrap` console
    `Padding` would crop it at the terminal width."""
    out.print(Padding(text, (0, 0, 0, 9)), soft_wrap=False)


def _show_gate(gated: gate_mod.Gated) -> None:
    """Per review round: blockers proven or not, the fix against them."""
    out = Console(highlight=False, soft_wrap=True)
    for n, r in enumerate(gated.rounds, 1):
        verdict = "approved" if r.review.approved else "changes requested"
        out.print(Text(f"review {n} {verdict}, {len(r.proofs)} blockers"))
        for p in r.proofs:
            state = f"proven by {', '.join(p.tests)}" if p.proven else "unproven"
            where = gate_mod.finding_text(p.finding)
            _indented(out, Text(f"{state}: {where}"))
        if r.attempts:
            ok = "green" if r.attempts[-1].green else "still red"
            _indented(out, Text(f"fix: {ok} after {len(r.attempts)}"))
    if gated.error:
        out.print(Text(f"review failed: {gated.error}", style="yellow"))
    blocked = gated.status == "blocked"
    note = " (a proven blocker is left: no PR)" if blocked else ""
    line = f"gate     {gated.status}{note}, ${gated.cost_usd:.3f}"
    out.print(Text(line, style="red" if blocked else ""))


def _show_draft(draft: pr_mod.Draft) -> None:
    """Tier and review verdict; agent text as `Text`."""
    out = Console(highlight=False, soft_wrap=True)
    tiers = ", ".join(f"{f.path} ({f.component}, {f.tier})" for f in draft.risk.files)
    out.print(Text(f"risk     {draft.risk.tier}: {tiers}"))
    review = draft.review
    if review is None:
        out.print(Text("review   did not run", style="yellow"))
        return
    r, run = review.reviewed, review.run
    verdict = "approved" if review.approved else "CHANGES REQUESTED"
    cost = f"${run.cost_usd:.3f} {run.turns} turns {run.latency_ms / 1000:.0f} s"
    out.print(Text(f"review   {verdict} ({cost})"))
    _indented(out, Text(r.summary))
    for f in r.findings:
        where = f.path if f.line is None else f"{f.path}:{f.line}"
        _indented(out, Text(f"{f.severity} {where}: {f.text}"))


def _show_loop(judged: loop_eval.Judged) -> None:
    """The diff, then the trace per node and the verdicts: the verdicts end
    the output, where the eye lands. Agent text goes in as `Text`."""
    result = judged.result
    out = Console(highlight=False, soft_wrap=True)

    def attempts(node: str, items: list[run_mod.Attempt]) -> None:
        for a in items:
            run = a.run
            agent = (
                f"${run.cost_usd:.3f} {run.turns} turns {run.latency_ms / 1000:.0f} s"
                if run is not None
                else f"failed: {a.error}"
            )
            checks = " ".join(
                f"{c.name}:{'ok' if c.ok else 'FAIL'}({c.seconds:.0f}s)"
                for c in a.checks
            )
            out.print(Text(f"{node} {a.n}  agent {agent} | {checks}"))
            if a.reverted:
                reverted = ", ".join(a.reverted)
                out.print(Text(f"         reverted: {reverted}", style="yellow"))
            if a.summary:
                _indented(out, Text(a.summary))

    if result.diff:
        out.print(Text(result.diff))
    out.print(Text(f"setup    {result.setup_s:.0f} s (worktree, install)"))
    attempts("repro", result.reproduce)
    if result.red is not None:
        state = "red" if not result.red.ok else "green before any change"
        source = "planted" if result.planted else "the agent's"
        line = f"{source} {', '.join(result.repro)}: {state}"
        out.print(Text(f"repro    {line}, {result.red.seconds:.0f} s"))
    attempts("fix", result.attempts)
    out.print(Text(f"{result.status}: {', '.join(result.changed) or 'no changes'}"))
    for name, check, meaning in [
        ("judge", judged.judge, "the planted repro on the fix"),
        ("valid", judged.valid, "the agent's repro with the reference fix"),
    ]:
        if check is not None:
            verdict = "green" if check.ok else "RED"
            out.print(Text(f"{name}    {verdict} ({meaning})"))


@eval_app.command("refine")
def eval_refine(
    repo: str = "philippmossier/local-ai-chat-playground",
    only: str | None = None,
    workers: int = 4,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """Refine on the planted bugs: spec files vs the reference fix's files
    (hit@k, recall@k, precision) and the size guess, per bug size.

    Paid: one agent run per bug, nothing cached; `--only key,key` for a few.
    Needs the seeded issues fetched (`fetch-issues`). The clone is updated to
    the tip of main, the planted code. Every result goes to data/refine/.
    """
    settings = get_settings()
    target = check_repo(repo)
    agent = _agent()
    planted = load_bugs(bugs, seed)
    if only is not None:
        keys = only.split(",")
        unknown = set(keys) - {b.key for b in planted}
        if unknown:
            typer.echo(f"unknown bug keys: {', '.join(sorted(unknown))}", err=True)
            raise typer.Exit(code=1)
        planted = [b for b in planted if b.key in keys]
    items = [
        s for s in triage_eval.load_seed(seed) if s.key in {b.key for b in planted}
    ]
    matched = triage_eval.match(items, load_issues(settings.issues_dir, target))
    clone = settings.clones_dir / target.replace("/", "-")
    commit = refine_mod.update_clone(target, clone)
    typer.echo(f"{target} at {commit[:7]}: {len(planted)} planted bugs, {agent.model}")

    def run(key: str) -> refine_mod.Refined | AgentError:
        try:
            return refine_mod.refine(matched[key], clone, agent)
        except AgentError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(
            zip(
                [b.key for b in planted],
                pool.map(run, [b.key for b in planted]),
                strict=True,
            )
        )
    done = {k: r for k, r in results.items() if not isinstance(r, AgentError)}
    scores = [
        refine_eval.score_bug(b, done[b.key].spec) for b in planted if b.key in done
    ]
    for b in planted:
        r = results[b.key]
        if isinstance(r, AgentError):
            typer.echo(f"  {b.key:<30} {b.size}  failed: {r}")
            continue
        sc = next(x for x in scores if x.key == b.key)
        listed = ", ".join(f.path for f in r.spec.files)
        mark = "hit@1" if sc.hit[1] else "hit@3" if sc.hit[3] else "miss "
        typer.echo(
            f"  {b.key:<30} {b.size}->{sc.size_guess}  {mark}  "
            f"${r.run.cost_usd:.3f} {r.run.turns:>2}t  {listed}"
        )
    for label, group in [
        ("all", scores),
        *[(f"size {z}", [x for x in scores if x.size == z]) for z in ("S", "M", "L")],
    ]:
        t = refine_eval.summarize(group)
        if t.n:
            typer.echo(
                f"  {label:<7} n={t.n:<3} hit@1 {t.hit[1]:.2f}  hit@3 {t.hit[3]:.2f}  "
                f"recall@3 {t.recall[3]:.2f}  precision {t.precision:.2f}  "
                f"size {t.size_ok:.2f}  files listed {t.listed:.1f}"
            )
    runs = [r.run for r in done.values()]
    if runs:
        typer.echo(
            f"  per bug: ${fmean(r.cost_usd for r in runs):.3f} mean, "
            f"{median(r.turns for r in runs):.0f} turns and "
            f"{median(r.latency_ms for r in runs) / 1000:.0f} s median, "
            f"${sum(r.cost_usd for r in runs):.2f} total, "
            f"{len(results) - len(done)} failed"
        )
    out = settings.refine_dir / f"eval-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        TypeAdapter(dict[str, refine_mod.Refined]).dump_json(done, indent=2)
    )
    typer.echo(f"  -> {out}")


@eval_app.command("loop")
def eval_loop(
    repo: str = "philippmossier/local-ai-chat-playground",
    only: str | None = None,
    workers: int = 4,
    planted: bool = False,
    wrong: bool = False,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """The core loop on the planted logic bugs: the agent writes the repro
    (red rate, valid against the reference fix), then fixes; resolved = the
    hidden planted repro and every check green. Also first try, fix in the
    reference's files, cost and time, per bug size. `--planted` hands the
    planted repro to the agent instead (the step 6 baseline).

    Paid: the reproducer's and implementer's runs only. Each bug starts from the spec
    `eval refine` stored in data/refine/ (refine is measured there); a bug
    without one, or with one made at another commit, is reported and
    skipped. Every result goes to data/runs/.

    `--wrong`: the implementer is told to make a plausible fix that does
    not fix the bug (wrong-*.json, the reviewer eval's negatives; a judge
    red is the goal there).
    """
    if planted and wrong:
        typer.echo("--wrong needs the agent's own repro, not --planted", err=True)
        raise typer.Exit(code=1)
    settings = get_settings()
    target = check_repo(repo)
    rules = run_mod.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo(f"no [loop] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    agent = _agent().editing(budget_usd=1.0)
    logic = [b for b in load_bugs(bugs, seed) if b.kind == "logic"]
    if only is not None:
        keys = only.split(",")
        unknown = set(keys) - {b.key for b in logic}
        if unknown:
            names = ", ".join(sorted(unknown))
            typer.echo(f"unknown logic bug keys: {names}", err=True)
            raise typer.Exit(code=1)
        logic = [b for b in logic if b.key in keys]
    clone = settings.clones_dir / target.replace("/", "-")
    commit = refine_mod.update_clone(target, clone)
    specs = loop_eval.load_specs(settings.refine_dir)
    ready = [b for b in logic if b.key in specs and specs[b.key].commit == commit]
    for b in logic:
        if b not in ready:
            typer.echo(f"  {b.key:<30} skipped: no stored spec at {commit[:7]}")
    typer.echo(f"{target} at {commit[:7]}: {len(ready)} logic bugs, {agent.model}")
    sandbox = Podman(image=rules.image, setup=rules.setup)
    prefix = "wrong" if wrong else "eval"

    def run(bug: PlantedBug) -> loop_eval.Judged:
        refined = specs[bug.key]
        worktree = settings.work_dir / f"{prefix}-{bug.key}"
        try:
            result = run_mod.run_loop(
                repo=target,
                number=refined.number,
                commit=refined.commit,
                spec=refined.spec,
                clone=clone,
                worktree=worktree,
                repro_file=repro_path(bugs, bug) if planted else None,
                rules=rules,
                agent=agent,
                sandbox=sandbox,
                instructions=review_eval.WRONG if wrong else run_mod.INSTRUCTIONS,
            )
            return loop_eval.against_key(
                result,
                worktree,
                rules,
                repro_path(bugs, bug),
                fix_path(bugs, bug),
                sandbox,
            )
        finally:
            run_mod.remove_worktree(clone, worktree)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip([b.key for b in ready], pool.map(run, ready), strict=True))
    scores = [loop_eval.score_bug(b, results[b.key]) for b in ready]
    for sc in scores:
        target_mark = "on target" if sc.on_target else "elsewhere"
        valid = {None: "-", True: "valid", False: "INVALID"}[sc.valid]
        verdict = "resolved" if sc.resolved else sc.status
        if sc.status == "green" and not sc.resolved:
            verdict = "judge red"
        typer.echo(
            f"  {sc.key:<30} {sc.size}  repro {valid:<7} {verdict:<9} "
            f"{sc.attempts} tries  {target_mark:<9}  ${sc.cost_usd:.3f}  "
            f"setup {sc.setup_s:.0f}s agent {sc.agent_s:.0f}s "
            f"checks {sc.checks_s:.0f}s"
        )
    for label, group in [
        ("all", scores),
        *[(f"size {z}", [x for x in scores if x.size == z]) for z in ("S", "M", "L")],
    ]:
        t = loop_eval.summarize(group)
        if t.n:
            typer.echo(
                f"  {label:<7} n={t.n:<3} reproduced {t.reproduced:.2f}  "
                f"valid {t.valid:.2f}  green {t.green:.2f}  "
                f"resolved {t.resolved:.2f}  first try {t.first_try:.2f}  "
                f"on target {t.on_target:.2f}  ${t.cost_usd:.3f}/bug  "
                f"{t.attempts:.0f} tries and {t.seconds:.0f} s median"
            )
    total = sum(sc.cost_usd for sc in scores)
    typer.echo(f"  total ${total:.2f}")
    if wrong:
        kept = sum(review_eval.is_case(j, "wrong") for j in results.values())
        typer.echo(f"  wrong fixes kept (green, judge red): {kept}/{len(results)}")
    out = settings.runs_dir / f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        TypeAdapter(dict[str, loop_eval.Judged]).dump_json(results, indent=2)
    )
    typer.echo(f"  -> {out}")


@eval_app.command("witness")
def eval_witness(
    repo: str = "philippmossier/local-ai-chat-playground",
    only: str | None = None,
    workers: int = 4,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """Prove the wrong fixes of `eval loop --wrong` that the planted repro
    missed (judge green): an agent writes a test for what the fix leaves
    broken; confirmed when it is red on the fix and green with the
    reference fix. Paid (one test-writing run per fix, up to 3 tries).
    Results to data/runs/witness-*.json, read by `eval review`."""
    settings = get_settings()
    target = check_repo(repo)
    rules = run_mod.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo(f"no [loop] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    agent = _agent().editing(budget_usd=1.0)
    by_key = {b.key: b for b in load_bugs(bugs, seed)}
    clone = settings.clones_dir / target.replace("/", "-")
    specs = loop_eval.load_specs(settings.refine_dir)
    wrong = review_eval.load_runs(settings.runs_dir, "wrong")
    todo = [
        k
        for k, j in sorted(wrong.items())
        if review_eval.usable(j) and j.judge is not None and j.judge.ok
    ]
    if only is not None:
        todo = [k for k in todo if k in only.split(",")]
    typer.echo(f"{target}: {len(todo)} wrong fixes the planted repro missed")
    sandbox = Podman(image=rules.image, setup=rules.setup)

    def run(key: str) -> witness_eval.Witness:
        worktree = settings.work_dir / f"witness-{key}"
        try:
            return witness_eval.witness(
                judged=wrong[key],
                spec=specs[key].spec,
                rules=rules,
                clone=clone,
                worktree=worktree,
                agent=agent,
                sandbox=sandbox,
                patch=fix_path(bugs, by_key[key]),
            )
        finally:
            run_mod.remove_worktree(clone, worktree)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = dict(zip(todo, pool.map(run, todo), strict=True))
    for key, w in results.items():
        if w.confirmed:
            state = "confirmed"
        elif w.error:
            state = w.error
        elif w.red is None:
            state = "no red witness"
        else:
            state = "witness red with the reference fix too"
        typer.echo(f"  {key:<30} {state}  {len(w.attempts)} tries  ${w.cost_usd:.3f}")
    kept = sum(w.confirmed for w in results.values())
    total = sum(w.cost_usd for w in results.values())
    typer.echo(f"  confirmed {kept}/{len(results)}  total ${total:.2f}")
    out = settings.runs_dir / f"witness-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_bytes(
        TypeAdapter(dict[str, witness_eval.Witness]).dump_json(results, indent=2)
    )
    typer.echo(f"  -> {out}")


@eval_app.command("human")
def eval_human(
    repo: str = "philippmossier/local-ai-chat-playground",
    publish: bool = False,
    update_bodies: bool = False,
    shuffle: int = 0,
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """Human minutes per merged PR. `--publish` (once, paid nothing, writes
    to GitHub): one draft PR per planted logic bug, 3 of them a proven
    wrong fix, shuffled by `--shuffle`; the set goes to data/human/set.json
    (the answer key: don't open it before reviewing). Without it: the
    timed review, one PR after the other, resumable; run it in your own
    terminal. Nothing is merged; the decision is recorded here."""
    settings = get_settings()
    target = check_repo(repo)
    set_path = settings.human_dir / "set.json"
    records_path = settings.human_dir / "reviews.json"
    if publish:
        _publish_human_set(target, set_path, shuffle, seed, update_bodies)
        return
    if not set_path.is_file():
        typer.echo(f"no {set_path}: run with --publish first", err=True)
        raise typer.Exit(code=1)
    hset = human_eval.HumanSet.model_validate_json(set_path.read_text("utf-8"))
    adapter = TypeAdapter(list[human_eval.Record])
    records = (
        adapter.validate_json(records_path.read_bytes())
        if records_path.is_file()
        else []
    )
    done = {r.url for r in records}
    todo = [e for e in hset.entries if e.url not in done]
    typer.echo(f"{len(done)} of {len(hset.entries)} reviewed")
    choice = click.Choice(["merge", "changes", "close"])
    for i, e in enumerate(todo, len(done) + 1):
        typer.echo(f"\n[{i}/{len(hset.entries)}] {e.url}")
        typer.echo("Review it on GitHub as usual (don't click merge).")
        webbrowser.open(e.url)
        start = time.monotonic()
        typer.prompt("Enter when you have decided", default="", show_default=False)
        seconds = round(time.monotonic() - start, 1)
        decision = typer.prompt("decision", type=choice)
        note = typer.prompt("note (optional)", default="", show_default=False)
        records.append(
            human_eval.Record(url=e.url, seconds=seconds, decision=decision, note=note)
        )
        records_path.write_bytes(adapter.dump_json(records, indent=2))
        typer.echo(f"  {seconds / 60:.1f} min, {decision}")
    t = human_eval.summarize(hset.entries, records)
    typer.echo(
        f"\nn={t.n}  {t.minutes:.1f} min per PR (median)  "
        f"{t.per_merge:.1f} min per merged PR  merged {t.merged}  "
        f"wrong caught {t.caught}/{t.wrong}  right merged {t.right_merged}/"
        f"{t.n - t.wrong}  agreed with the reviewer {t.agreed:.2f}"
    )


def _publish_human_set(
    target: str, set_path: Path, shuffle: int, seed: Path, update: bool
) -> None:
    """Draft PRs for the set; resumable (published picks are skipped, or
    get the current body with `update`)."""
    settings = get_settings()
    token = _write_token(target)
    rules = run_mod.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo(f"no [loop] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    found = review_eval.cases(
        settings.runs_dir, witness_eval.load_witnessed(settings.runs_dir)
    )
    runs = {(k, kind): j for k, kind, j in found}
    reviews = review_eval.load_reviews(settings.runs_dir)
    if set_path.is_file():
        hset = human_eval.HumanSet.model_validate_json(set_path.read_text("utf-8"))
    else:
        right = [k for k, kind, _ in found if kind == "right"]
        wrong = [k for k, kind, _ in found if kind == "wrong"]
        hset = human_eval.HumanSet(picks=human_eval.pick(right, wrong, shuffle))
    missing = [p for p in hset.picks if (p.key, p.kind) not in reviews]
    if missing:
        names = ", ".join(f"{p.kind} {p.key}" for p in missing)
        typer.echo(f"no stored review for {names}: run eval review", err=True)
        raise typer.Exit(code=1)
    specs = loop_eval.load_specs(settings.refine_dir)
    titles = {s.key: s.title for s in triage_eval.load_seed(seed)}
    clone = settings.clones_dir / target.replace("/", "-")
    pulls = publish_mod.GitHubPulls(token)
    published = {(e.key, e.kind): e for e in hset.entries}
    set_path.parent.mkdir(parents=True, exist_ok=True)
    for p in hset.picks:
        if (p.key, p.kind) in published and not update:
            continue
        result = runs[(p.key, p.kind)].result
        review = reviews[(p.key, p.kind)]
        draft = pr_mod.draft(
            issue_title=titles[p.key],
            spec=specs[p.key].spec,
            result=result,
            tests=rules.tests,
            risk_rules=pr_mod.load_risk(settings.targets_dir, target),
            components=components_mod.load_rules(settings.targets_dir, target),
            review=review,
        )
        entry = published.get((p.key, p.kind))
        if entry is not None:
            pulls.update_body(target, entry.pr, draft.body)
            typer.echo(f"  #{result.number}: body updated on {entry.url}")
            continue
        try:
            got = publish_mod.publish(
                repo=target,
                clone=clone,
                base_commit=result.commit,
                diff=result.diff,
                number=result.number,
                issue_title=titles[p.key],
                title=draft.title,
                body=draft.body,
                token=token,
                pulls=pulls,
            )
        except (publish_mod.PublishError, httpx.HTTPError) as exc:
            set_path.write_text(hset.model_dump_json(indent=2), "utf-8")
            typer.echo(f"  #{result.number}: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        hset.entries.append(
            human_eval.Entry(
                key=p.key,
                kind=p.kind,
                number=result.number,
                pr=got.number,
                url=got.url,
                branch=got.branch,
                approved=review.approved,
            )
        )
        set_path.write_text(hset.model_dump_json(indent=2), "utf-8")
        typer.echo(f"  #{result.number} -> {got.url}")
    typer.echo(f"{len(hset.entries)} draft PRs, set in {set_path}")


@eval_app.command("gate")
def eval_gate(
    repo: str = "philippmossier/local-ai-chat-playground",
    only: str | None = None,
    workers: int = 4,
    bugs: Path = Path("playground/bugs.toml"),
    seed: Path = Path("playground/seed.toml"),
) -> None:
    """The review gate on fixes with a known verdict: right ones should be
    published, wrong ones blocked or fixed, never leaked (published and
    still wrong by the planted repro or the witness test). Proofs checked
    against the reference fix. Each case replays its diff with an install;
    paid (reviews, proofs and fix rounds; about $0.15-0.40 per case)."""
    settings = get_settings()
    target = check_repo(repo)
    rules = run_mod.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo(f"no [loop] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    reader = _agent()
    editor = reader.editing(budget_usd=1.0)
    by_key = {b.key: b for b in load_bugs(bugs, seed)}
    clone = settings.clones_dir / target.replace("/", "-")
    specs = loop_eval.load_specs(settings.refine_dir)
    witnesses = witness_eval.load_witnesses(settings.runs_dir)
    witnessed = frozenset(k for k, w in witnesses.items() if w.confirmed)
    todo = review_eval.cases(settings.runs_dir, witnessed)
    if only is not None:
        todo = [c for c in todo if c[0] in only.split(",")]
    typer.echo(
        f"{target}: {sum(k == 'right' for _, k, _ in todo)} right, "
        f"{sum(k == 'wrong' for _, k, _ in todo)} wrong fixes, {reader.model}"
    )
    sandbox = Podman(image=rules.image, setup=rules.setup)

    def run(case: tuple[str, review_eval.Kind, loop_eval.Judged]) -> gate_eval.GateCase:
        key, kind, judged = case
        worktree = settings.work_dir / f"gate-{kind}-{key}"
        try:
            return gate_eval.gate_case(
                key=key,
                kind=kind,
                judged=judged,
                spec=specs[key].spec,
                rules=rules,
                clone=clone,
                worktree=worktree,
                reader=reader,
                editor=editor,
                sandbox=sandbox,
                planted=repro_path(bugs, by_key[key]),
                patch=fix_path(bugs, by_key[key]),
                witness=witnesses.get(key),
            )
        finally:
            run_mod.remove_worktree(clone, worktree)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, todo))
    for c in results:
        g = c.gated
        if g is None:
            typer.echo(f"  {c.kind:<5} {c.key:<30} setup failed: {c.error}")
            continue
        pv = c.proofs_valid
        valid = "-" if pv is None else ("valid" if pv.ok else "INVALID")
        typer.echo(
            f"  {c.kind:<5} {c.key:<30} {g.status:<9} {c.outcome:<11} "
            f"{len(g.rounds)} reviews {c.proven} proven (proofs {valid})  "
            f"${g.cost_usd:.3f}"
        )
    t = gate_eval.summarize(results)
    typer.echo(
        f"  right n={t.right} published {t.published:.2f}  wrong n={t.wrong} "
        f"blocked {t.blocked:.2f} fixed {t.fixed:.2f} leaked {t.leaked:.2f}  "
        f"proofs {t.proofs} valid {t.proofs_valid:.2f}  ${t.cost_usd:.3f}/case"
    )
    out = settings.runs_dir / f"gate-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_bytes(TypeAdapter(list[gate_eval.GateCase]).dump_json(results, indent=2))
    typer.echo(f"  -> {out}")


@eval_app.command("review")
def eval_review(
    repo: str = "philippmossier/local-ai-chat-playground",
    only: str | None = None,
    workers: int = 4,
) -> None:
    """The reviewer on fixes with a known verdict: right ones (`eval loop`,
    judge green) should be approved, wrong ones (`eval loop --wrong`, judge
    red) not. Each stored diff is replayed in a fresh worktree, so this
    pays for reviewer runs only (about $0.04 each). Results to data/runs/."""
    settings = get_settings()
    target = check_repo(repo)
    rules = run_mod.load_rules(settings.targets_dir, target)
    if rules is None:
        typer.echo(f"no [loop] in the target file of {target}", err=True)
        raise typer.Exit(code=1)
    agent = _agent()
    clone = settings.clones_dir / target.replace("/", "-")
    specs = loop_eval.load_specs(settings.refine_dir)
    todo = review_eval.cases(
        settings.runs_dir, witness_eval.load_witnessed(settings.runs_dir)
    )
    if only is not None:
        todo = [c for c in todo if c[0] in only.split(",")]
    typer.echo(
        f"{target}: {sum(k == 'right' for _, k, _ in todo)} right, "
        f"{sum(k == 'wrong' for _, k, _ in todo)} wrong fixes, {agent.model}"
    )

    def run(case: tuple[str, review_eval.Kind, loop_eval.Judged]) -> review_eval.Case:
        key, kind, judged = case
        return review_eval.review_case(
            key=key,
            kind=kind,
            judged=judged,
            spec=specs[key].spec,
            rules=rules,
            clone=clone,
            worktree=settings.work_dir / f"review-{kind}-{key}",
            agent=agent,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(run, todo))
    for c in results:
        if c.review is None:
            typer.echo(f"  {c.kind:<5} {c.key:<30} failed: {c.error}")
            continue
        r = c.review
        good = c.approved == (c.kind == "right")
        verdict = "approved" if c.approved else "rejected"
        typer.echo(
            f"  {c.kind:<5} {c.key:<30} {verdict:<9} {'ok ' if good else 'MISS'} "
            f"test {'y' if r.reviewed.test_matches_spec else 'n'} "
            f"cause {'y' if r.reviewed.fixes_cause else 'n'} "
            f"{len(r.reviewed.findings)} findings  ${r.run.cost_usd:.3f}"
        )
    t = review_eval.summarize(results)
    typer.echo(
        f"  right n={t.right} passed {t.passed:.2f}  wrong n={t.wrong} caught "
        f"{t.caught:.2f}  failed runs {t.failed}  ${t.cost_usd:.3f}/review"
    )
    out = settings.runs_dir / f"review-{datetime.now(UTC):%Y%m%dT%H%M%S}.json"
    out.write_bytes(TypeAdapter(list[review_eval.Case]).dump_json(results, indent=2))
    typer.echo(f"  -> {out}")


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
        impact_run = _impact_fn(conn, driver, embed)
        build_mcp(
            embed, conn, driver, run, default_repo=target, impact_fn=impact_run
        ).run("stdio")
