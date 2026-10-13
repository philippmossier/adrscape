# ADR-006: Re-scope from ADR landscape to a software factory devs use as a tool

## Status

Accepted, 2026-10-13. Renames the project from `adrscape` to `cofabriq`.
ADR-001 to ADR-005 stay valid as history and use the old name.

## Context

v0 (steps 1-7) built a context layer over a set of repositories: ingest
docs and manifests, heading-aware chunks, embeddings in pgvector, LLM
extraction with checked links, a Neo4j graph of decisions in force, and MCP
tools on top. It works on the fixture. The product around it does not hold up:

- **Evals have no honest ground truth.** Realistic ADR corpora with known
  relations are rare. A golden set over invented ADRs measures my own
  invention.
- **The demo is niche.** "Which decisions are in force?" matters to an
  architect who joins a company with many repos full of ADRs. Most companies
  don't have that corpus.
- **It does not transfer.** Every company has issues and a repo. Few have an
  ADR landscape.

The same engine fits a more common problem: issues come in, someone has to
triage them, find duplicates, plan the change, implement it and review it.
That pipeline has natural evals: was the duplicate right, was the component
right, did the tests pass, was it merged, how many turns did it take. Every
public repository with an issue tracker is test data.

The usual answer is an autopilot that turns issues into PRs, with humans
reviewing all day. That is a crowded space, and it makes reviewing the main
human job.

## Decision

- **Re-scope to a software factory that devs use as a tool**, pointed at any
  GitHub repository by config. It has two modes on one shared repo map:
  - **Autopilot** for small, low-blast-radius issues: triage, refine,
    implement on a branch, the target repo's own CI as the gate, a reviewer
    with its own context, a draft PR for the dev who triggered it.
  - **Planning mode** for real features: the dev plans as usual and asks the
    map what a change touches, which decisions constrain it and what related
    work already exists.
- **Devs own the factory.** One issue, the assigned dev triggers it, one
  branch, one PR, that dev reviews. Built for team use. Coordinating several
  agents on one repo is not built.
- **Build, consume, skip.** Built: triage, the repo map, refinement, the
  implement-and-fix loop, the reviewer, blast-radius routing. Consumed: the
  target repo's CI (lint, static analysis, tests), and the coding agent for
  implementation, which is delegated rather than built. Skipped: formal
  verification, parallel implementer fleets, and deploying the target app.
- **Versions, each released with numbers:**
  - **v1:** issue in, triage card out (read-only).
  - **v2:** repo map and planning mode (read-only).
  - **v3:** autopilot.
  - **v4:** review and blast-radius routing.
  
  The map comes before autopilot because sizing an issue and its blast radius
  needs it.
- **Read-only through v2.** Writes start in v3 and only happen on a local
  trigger by the dev. Dry run is the default. `--apply` may create one branch
  and a draft PR, nothing else, and that limit is enforced by a fine-grained
  token rather than by the prompt. Issue text is untrusted input.
- **Keep the engine, retire the domain.** Source, parsing, chunking,
  embeddings, extraction with checked evidence and the MCP server carry over
  under `cofabriq.context`. The ADR domain schema and ADR graph stay as they
  are until v2 rebuilds Neo4j as the repo map. In v1, issues live in Postgres
  only, until a question needs traversal.
- **Scope cut, revised.** Still no code parsing by cofabriq. The map gets its
  structure from file paths, manifests and git history: which files change
  together, which PR changed which files. Reading code is the coding agent's
  job.
- **Evaluated on a real issue history:** `open-webui/open-webui`, as a frozen
  read-only snapshot. Issues closed with a named duplicate give dedupe ground
  truth. Merged PRs that fix an issue give the touched components.
- **Rename in place** instead of starting a new repository.

## Consequences

- Decisions still matter, but as one input. ADRs, PR descriptions and
  "won't fix" closings become decision nodes in the v2 map. The reviewer
  checks a diff against them.
- The v2 map ADR will revise ADR-005: Neo4j stops holding ADR revision
  chains and holds the repo map instead.
- Implementation is delegated. Which engine (an agent SDK in process or a
  coding CLI as a subprocess) is decided at v3 by a spike: one fix loop end
  to end, with an ADR.
- The package, CLI, compose project and MCP server are renamed. Local
  databases are rebuilt from `data/` because volumes are named after the
  project. Nothing has to be migrated.
- Every version ends with a public release: numbers in the README, a demo on
  a playground copy of a small app that resets regularly, and a post.

## Alternatives considered

- **Keep adrscape and scale up its evals on a large public ADR corpus.** That
  fixes volume, not realism. The demo stays niche and does not transfer to a
  company without an ADR culture.
- **A new repository, with adrscape frozen.** That gives a clean name, but
  leaves two half-finished projects in public, copies the engine and loses
  the history that explains this decision.
- **A new repository with adrscape as a library dependency.** That gives a
  clean component boundary, but means two repositories for one person with
  no second consumer.
- **Autopilot first, map later.** That gets to the PR demo sooner, but
  autopilot would size issues without knowing their blast radius, which is
  the one judgement that decides whether a change may run unattended.
- **Drop Neo4j, Postgres only.** That is simpler for v1, but blast radius and
  planning mode are multi-hop questions (change, files, components,
  dependents, decisions), and those are what the graph is for.
