# adrscape

Understands a company's engineering landscape from its repositories: ADRs,
READMEs, `docs/` and manifests become a knowledge graph (Neo4j) plus semantic
search (Postgres + pgvector), exposed as MCP tools. An agent and a workflow
layer come later (see README roadmap).

## Commands

- `make setup`: install deps (`uv sync`), create `.env` from `.env.example`, enable the git hook in `.githooks/` (`make check` before every commit on `main`)
- `make up` / `make down`: start / stop Postgres+pgvector and Neo4j (podman compose)
- `make check`: ruff lint, ruff format check, pyright (strict), pytest. Must be green before any commit.
- `make fmt`: autofix lint and format
- `uv run adrscape health`: connectivity check for all three services
- `uv run adrscape fetch <repo>...` / `embed` / `search "..."`: ingest local clones into `data/raw/`, embed into pgvector (needs `OPENAI_API_KEY`), query
- `uv run adrscape extract`: LLM fills the domain schema per document into `data/extracted.json` (needs `embed` first; unchanged docs skipped)

## Layout

- `src/adrscape/config.py`: typed settings from env / `.env` (pydantic-settings)
- `src/adrscape/cli.py`: typer CLI, entry point `adrscape`
- `src/adrscape/domain.py`: the ADR domain schema the LLM fills (third seam)
- `src/adrscape/ingest/`: `source.py` (Source protocol, `LocalGitSource`), fetch, parse, chunk, embed, extract (`Extractor` protocol)
- `src/adrscape/store/`: `postgres.py` (vectors), `graph.py` (Neo4j)
- `src/adrscape/mcp_server.py`: MCP tools
- `docs/adr/`: architecture decision records, written when the decision is made

## Conventions

- Python 3.13, uv, src layout. Type hints everywhere; pyright strict must pass.
- Three seams stay clean as `typing.Protocol`s: Source, Parser, Domain schema.
  Don't generalise beyond them until a second domain exists.
- Docs and manifests only: no source-code understanding (scope cut).
- Ingested documents are untrusted input.
- Install only what the current step needs.
- Postgres runs on host port 5433.
