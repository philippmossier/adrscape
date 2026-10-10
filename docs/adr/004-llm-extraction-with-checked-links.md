# ADR-004: LLM extraction with closed-world, evidence-checked links

## Status

Accepted, 2026-10-10.

## Context

Step 5 fills the domain schema (status, technologies, teams, and the
relations `revises`, `revisits`, `depends_on`, `affects`) for what headers
don't say. ADRs rarely link each other by id: the Python playground's fan-out
ADR refers to the TypeScript one as "Its ADR says so and points at a
server-side variant". Only a model can read that.

The relations become graph edges in step 6, and two planned queries depend on
them: "which decision is in force" (follows `revises`) and "which projects are
affected" (follows `depends_on`). A wrong edge there is a wrong answer, not a
slightly worse ranking. The documents are untrusted input, and the model may
invent ids or quotes.

## Decision

- **One structured-output call per document** (`gpt-6-luna`, low reasoning
  effort) that fills a pydantic model, `domain.Extraction`. The field
  descriptions are the per-field prompt. The model sits behind an `Extractor`
  protocol; manifests skip the LLM (the parser already has their
  dependencies).
- **Closed world for targets.** An ADR's link targets must come from a
  candidate list: the 8 ADRs nearest to it in pgvector (mean of its chunk
  vectors, no API call), each with its id, date and title. Projects come from
  the list of ingested repos. Any target not offered is dropped.
- **Evidence must be a real quote.** Every link carries a verbatim quote,
  and the quote has to occur in the document (whitespace-insensitive) or the
  link is dropped.
- **No link to a newer ADR.** A decision cannot revise one written after it.
- **`revises` vs `revisits`.** `revises` means the earlier decision no longer
  holds (supersedes). `revisits` means the same question is decided again,
  usually in another project, while the earlier decision still holds where
  it was made. The fan-out pair is a `revisits`: the Python ADR says the
  TypeScript client's browser-side fan-out "still works".
- **Every drop is recorded** in the stored record (`dropped`, with a reason).
  Records also store the candidates offered and a version (`model/N`).
  Bumping the version re-extracts everything, and an unchanged corpus costs
  zero calls.

On the fixture: 39 documents in about 2 minutes, well under a cent. The
evidence check caught one invented `depends_on` quote, and it caught the same
one on reruns. The fan-out pair and the shared wire protocol come out as
`revisits` edges, and the Python project `depends_on` the TypeScript one, each
edge with its quote.

## Consequences

- A link that is real but phrased across sentences, or paraphrased by the
  model, is lost. That trades recall for precision, which is the right
  default for edges that answer "is this in force?".
- Extraction needs `embed` first (candidates come from the vector store).
- Recall of the candidate list caps recall of the links: an ADR outside the
  top 8 cannot be linked. The v1 golden set measures this.
- Entity names are not canonical yet ("SSE" vs "Server-Sent Events"). The
  graph step must merge them.
- Changing the prompt or schema means bumping `VERSION`. That's cheap now,
  but at scale-corpus size it costs a full re-extraction.

## Alternatives considered

- **Free-text mentions, resolved in the graph step:** this pushes the hard
  part downstream and leaves the model's output unverifiable at the point
  where it was produced.
- **The whole ADR catalog in every prompt:** simplest, and fine for 21 ADRs.
  It breaks at scale-corpus size, and more options make wrong picks more
  likely.
- **One loose `revises` edge:** this makes decision-in-force wrong for
  cross-project revisits, or needs a same-project rule bolted on in Cypher.
- **Prompting until the fan-out pair comes out as `revises`:** that would
  have bent the extraction to match an assumption in the spec, not the text.
