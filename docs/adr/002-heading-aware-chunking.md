# ADR-002: Heading-aware chunking, one ADR per chunk

## Status

Accepted, 2026-10-09.

## Context

Embeddings (step 4) need text cut into pieces: a small embedding model reads
about 512 tokens and drops the rest, and one vector for a 260-line README
averages setup, architecture and licence into something that matches no
question well. Where to cut decides what a search hit can contain.

The dev fixture has two very different shapes. ADRs are short (709 to 2103
characters across all 21) and their value is the link between Context and
Decision: a hit that says "we stream over SSE" without the "because" is half
an answer. Other docs range up to 490 lines, with deep heading trees and
sections from one line to several screens.

Parsing and chunking are kept apart: a `Parser` (second seam, one per format)
turns a file into sections with a heading trail plus metadata, and the
chunker works on sections only, so a PDF parser later reuses the chunker
unchanged.

## Decision

- **Cut at markdown headings**, then **merge** consecutive sections while
  they fit a budget of **2400 characters** (about 600 tokens), and **split**
  a section that alone exceeds it at blank lines (hard cut only for a single
  oversized paragraph).
- **Every chunk carries its heading trail** (`README > How it works`) and the
  document identity `(repo, path, sha)` from ADR-001.
- The budget is chosen so **every ADR stays one chunk**. ADR `status` and
  `date` are read from the header into metadata, verbatim.
- **Manifests become one chunk** rendered as `name / description /
  dependencies`; scripts and build config are dropped as noise for search.
  Manifests without a known schema (compose files) stay raw text.

On the fixture: 43 documents give 126 chunks, all 21 ADRs whole.

## Consequences

- Hits are citable by section and an ADR hit always contains its reasoning.
- Step 4 must use an embedding model whose window holds 2400 characters;
  a 512-token model would truncate the longest chunks.
- Merging means a chunk can span a few short sections under the first one's
  heading; the trail then names only where the chunk starts.
- Paragraph splitting can cut through a long code block. Docs are mostly
  prose, so this is accepted until it shows up in retrieval evals.
- The budget is one constant. At v1 the scale corpus and the eval harness
  decide whether it stays; changing it only means re-chunking `data/raw/`.

## Alternatives considered

- **Fixed size with overlap:** simplest and a common baseline, but ignores
  structure and separates an ADR's Context from its Decision. Worth keeping as
  an eval baseline at v1, not as the default.
- **One chunk per section, no merging:** exact citations, but a 490-line file
  of `###` cases becomes dozens of tiny chunks with weak vectors that still
  compete in every search.
- **Semantic chunking** (cut where embedding similarity drops): costs an
  embedding pass before chunking and is hard to debug; headings already mark
  topic changes in this corpus.
