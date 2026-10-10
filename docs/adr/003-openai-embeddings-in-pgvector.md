# ADR-003: OpenAI text-embedding-3-small, stored in pgvector

## Status

Accepted, 2026-10-10.

## Context

Step 4 turns the chunks from ADR-002 into vectors and makes them searchable.
The model is the hardest part of this step to reverse: its dimension is fixed
in the `vector(N)` column, and a vector is only comparable with vectors from
the same model, so switching models means re-embedding the whole corpus.

Constraints from earlier steps: chunks go up to 2400 characters (about 600
tokens), so a 512-token model would silently truncate the longest ADRs. The
corpus is small now (126 chunks) and in the low thousands at the v1 scale
corpus. The app is deployed to a small CPU-only VPS (step 8).

## Decision

- **Model: OpenAI `text-embedding-3-small`**, 1536 dimensions, 8191-token
  window. Model name and dimension are constants in `ingest/embed.py`, the
  only module that knows the provider; everything else sees a plain
  `Embed = Callable[[Sequence[str]], list[list[float]]]`.
- **Embedded text = heading trail + chunk text**, so a chunk split from a long
  section still carries its document's title.
- **Storage: one `chunks` table in Postgres/pgvector**, primary key
  `(repo, path, sha, ord)`, cosine distance, HNSW index. The schema is
  created idempotently on connect.
- **Re-embed by document identity:** a sync embeds only documents whose
  `(repo, path, sha)` is not stored yet and deletes rows of shas that are no
  longer in the input. An unchanged corpus costs zero API calls. The API call
  happens before the write transaction, so a failed call leaves the table as
  it was.

On the fixture: 43 documents, 126 chunks embedded in one request; a second
run embeds nothing. "Why is the fan-out on the server" returns both fan-out
ADRs (the Python one that moves fan-out to the server, and the TS one it
revises) at 0.59 and 0.53, the next hit at 0.32.

## Consequences

- An API key is required to ingest and to search (every query is embedded),
  and document text leaves the machine. Fine for public repos; a customer
  with private repos may need a local model, which is a new column plus a
  full re-embed, not a code rewrite.
- Cost is negligible at this scale (the fixture is about 50k tokens, a few
  tenths of a cent per full embed).
- Only `embed.py` and the column dimension change if the model changes.
- Retrieval quality is unmeasured until v1. One early data point: "how do we
  stream tokens to the browser" ranks architecture docs above the
  event-protocol ADR. The golden set decides whether that is the model, the
  chunking or the query.

## Alternatives considered

- **Local model via FastEmbed** (`nomic-embed-text-v1.5`, 768 dimensions,
  8k window): no key, documents never leave the machine, deployable as a
  ~500 MB model baked into the image. Rejected for v0 to keep the image small
  and setup to one key; the first candidate if private-repo ingestion
  becomes a requirement, and a baseline worth comparing at v1.
- **`bge-small-en-v1.5`** (384 dimensions): the FastEmbed default, but its
  512-token window truncates the longest chunks.
- **Voyage `voyage-3.5-lite`**: strong retrieval quality, same trade-offs as
  any API, one more vendor account for no measured gain yet.
- **Vectors in Neo4j** (it has vector indexes): would merge the two stores;
  kept apart on purpose (see the README architecture section); the
  graph step records that decision in its own ADR.
