# ADR-005: Two databases, pgvector for meaning, Neo4j for connections

## Status

Accepted, 2026-10-11.

## Context

The architect's questions come in two shapes:

- **Similar meaning:** "did we decide anything about message queues?" has
  to find an ADR that only says "Kafka". This is fuzzy and blind to
  relations, which makes it a vector search.
- **Connections:** "which decision is in force?" follows the revision chain.
  "Which projects are affected if the wire protocol changes?" follows
  `depends_on` over several hops. These are exact and blind to meaning,
  which makes them graph traversals.

Steps 4 and 5 already produce both inputs: chunk vectors in pgvector, and
per-document extraction records with checked links in `data/extracted.json`.
Step 6 has to decide where the links live.

One database would work. Neo4j 5 has vector indexes, and Postgres can walk
a graph with recursive CTEs.

## Decision

- **Keep both.** pgvector holds chunks and vectors (step 4), and Neo4j holds
  one node per thing (Project, Document, Technology, Team), with an edge for
  every extracted link. Each answers its own question type natively. That
  split is the same one the planned tool typing makes, where the search tool
  goes to Postgres and the deterministic graph tools go to Neo4j.
- **Both are derived, neither is a source of truth.** Git is the source of
  truth. pgvector is rebuilt from `data/raw/`, and Neo4j is rebuilt from
  `data/extracted.json` without any LLM calls. Nothing writes to one store
  from the other, so the two never need a distributed transaction.
- **Consistency per document, by sha.** Both stores key a document by
  (repo, path, sha). Each Document node stores
  `sha|extraction version|graph version`. A sync rewrites only the
  documents whose stamp changed. It deletes a document's outgoing edges and
  writes the new ones, since every edge leaves a Document. Documents gone
  from the input are deleted, and Technology, Team and Project nodes that
  nothing points at any more are pruned. Each sync runs as one Neo4j
  transaction.
- **The queries are fixed Cypher**, with no LLM involved:
  - **In force:** the status is not proposed, rejected, deprecated or
    superseded, and no other ADR revises it, unless that ADR is itself only
    proposed or rejected.
  - **Affected:** the projects a decision names, plus every project that
    depends on one of them through any number of `depends_on` hops (a
    quantified path pattern).

On the fixture: 39 documents in one sync and 0 written on a rerun. All 21
ADRs are in force, and two are revisited across projects (wire protocol,
fan-out). Changing the event-protocol ADR reaches the TypeScript project
directly and the Python project at one hop.

## Consequences

- There are two services to run, back up and keep in sync. Sync is
  one-way from files, so a lost or drifted database is a rebuild, not a
  repair.
- A question that needs both stores ("is the decision about X in force?")
  is answered in two steps: vector search for candidates, then a graph
  lookup by document id. That join happens in the tool or agent layer
  (step 7, v2), not in a database.
- Neo4j Community has one database, so tests run against a second,
  in-memory instance (`neo4j-test`, port 7688) instead of a throwaway
  schema.
- Technology names are merged by a normalised key plus a short,
  hand-curated alias map (SSE for "Server-Sent Events", for example).
  Bumping `GRAPH_VERSION` rewrites every document when the map changes. The
  map grows with the corpus, which is a cost that a canonicalisation step
  may replace later.

## Alternatives considered

- **Neo4j only, with its vector index:** this gives one store and one
  query language. pgvector is already built and measured, and Postgres is
  the better home for the rows, metadata and SQL filters that search
  results need. Neo4j's vector index would also tie retrieval quality to
  the graph database's release cycle.
- **Postgres only, with recursive CTEs:** this works for "in force" and is
  painful for multi-hop variable-length paths. It also loses the graph as
  something you can show and query ad hoc, which the v2 graph sub-agent
  writes Cypher for.
- **Write to both stores from one ingest pass:** fewer commands, but a
  failure between the two writes leaves them inconsistent. Separate,
  idempotent syncs from files on disk make each one retryable on its own.
