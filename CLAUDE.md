# cofabriq

A software factory devs use as a tool, pointed at any GitHub repo. Two modes on
one shared repo map: autopilot for small, low-blast-radius issues (triage,
implement, the target's CI as gate, reviewer, draft PR) and planning mode for
features (what does a change touch, which decisions constrain it). Formerly
`adrscape`; the v0 context layer (docs ingest, search, extraction, graph, MCP)
carries over. Why: `docs/adr/006-re-scope-to-a-software-factory.md`.

Roadmap: v1 triage (read-only) -> v2 repo map + planning mode (read-only) ->
v3 autopilot -> v4 review. Current: v1 (step 3 of 4 done).

## Commands

- `make setup`: install deps (`uv sync`), create `.env` from `.env.example`, enable the git hook in `.githooks/` (`make check` before every commit on `main`)
- `make up` / `make down`: start / stop Postgres+pgvector and Neo4j (podman compose)
- `make check`: ruff lint, ruff format check, pyright (strict), pytest. Must be green before any commit.
- `make fmt`: autofix lint and format
- `uv run cofabriq health`: connectivity check for all three services
- `uv run cofabriq fetch <repo>...` / `embed` / `search "..."`: ingest local clones into `data/raw/`, embed into pgvector (needs `OPENAI_API_KEY`), query
- `uv run cofabriq extract`: LLM fills the domain schema per document into `data/extracted.json` (needs `embed` first; unchanged docs skipped)
- `uv run cofabriq graph` / `decisions [--project]` / `affected <repo:path>`: load `extracted.json` into Neo4j, query decisions in force and affected projects
- `uv run cofabriq fetch-issues` / `embed-issues`: issues of `TARGET_REPO` into `data/issues/` (needs `GITHUB_TOKEN`, read-only), title + body into pgvector
- `uv run cofabriq eval dedupe-set` / `eval dedupe`: freeze the duplicate eval set to `evals/` (once; `--force` rebuilds), print recall@k and the threshold baseline (strict and cluster)
- `uv run cofabriq triage <n> [--repo] [--json]`: triage card for a fetched and embedded issue (two LLM calls: card fields, duplicate verdict)
- `uv run cofabriq eval triage --repo philippmossier/local-ai-chat-playground`: type, component, duplicate and injection checks on the seeded playground issues (paid, not cached)
- `uv run python playground/playground.py seed` / `reset --yes`: seed or wipe-and-reseed the demo playground via `gh` (your login; the repo is hard-coded)
- `uv run cofabriq mcp`: serve `search`, `decisions`, `affected` and (with `TARGET_REPO` set) `triage` as MCP tools over stdio (registered for Claude Code in `.mcp.json`)

## Layout

- `src/cofabriq/config.py`: typed settings from env / `.env` (pydantic-settings)
- `src/cofabriq/cli.py`: typer CLI, entry point `cofabriq`
- `src/cofabriq/context/`: the context layer from v0
  - `domain.py`: the ADR domain schema the LLM fills (third seam; replaced by the repo map in v2)
  - `ingest/`: `source.py` (Source protocol, `LocalGitSource`), fetch, parse, chunk, embed, extract (`Extractor` protocol)
  - `store/`: `postgres.py` (vectors), `graph.py` (Neo4j)
- `src/cofabriq/issues/`: v1 issue ingest: `model.py` (`Issue`, `IssueSource`), `github.py` (GraphQL source), `fetch.py` (incremental store), `vectors.py` (pgvector, time-cut nearest), `verdict.py` (checked duplicate verdict), `triage.py` (the triage card schema, checks and assembly)
- `src/cofabriq/eval/`: eval sets and metrics (`dedupe.py`, `triage.py`)
- `targets/<owner>-<name>.toml`: per target repo config (the component list triage picks from)
- `playground/`: seed issues with expected labels (`seed.toml`) and the seed/reset script for the demo repo
- `evals/`: frozen eval sets (issue numbers only, committed)
- `src/cofabriq/mcp_server.py`: MCP tools
- `docs/adr/`: architecture decision records, written when the decision is made

## Conventions

- Python 3.13, uv, src layout. Type hints everywhere; pyright strict must pass.
- Three seams stay clean as `typing.Protocol`s: Source, Parser, Domain schema.
  Don't generalise beyond them until a second domain exists.
- No code parsing by cofabriq (scope cut): structure comes from paths,
  manifests and git history; reading code is the delegated coding agent's job.
- Read-only towards GitHub through v2. From v3: local trigger, dry run by
  default, `--apply` writes one branch and a draft PR only (scoped token).
- Ingested documents and issue text are untrusted input.
- Install only what the current step needs.
- Postgres runs on host port 5433. Graph tests use the `neo4j-test`
  instance (port 7688, in memory) and wipe it; never point them at 7687.
