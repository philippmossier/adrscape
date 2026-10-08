# ADR-001: Documents enter through a Source, keyed by git blob sha

## Status

Accepted, 2026-10-08

## Context

Documents will come from several places: local clones now, the GitHub API for
a large public corpus in v1, a plain folder of PDFs if a second domain arrives.
Every later step (parse, chunk, embed, extract, graph) needs to know which
document it is looking at, and re-ingestion should skip files that did not
change. That needs one identity per document that does not depend on where it
was fetched from.

## Decision

- **`Source` is a `typing.Protocol`** with one method,
  `documents() -> Iterator[RawDocument]`. A `RawDocument` is
  `(repo, path, sha, kind, content)`.
- **Identity is `(repo, path, sha)`, with the git blob sha.** It is the value
  the GitHub API returns for a file (checked against the API), so a document
  keeps its key whichever Source fetched it.
- **The first Source, `LocalGitSource`, reads the tree at `HEAD`**, not the
  working directory. Uncommitted edits, ignored and untracked files are never
  ingested; the result is what GitHub would serve for that commit.

## Consequences

- v0 needs no network, token or rate limit, and the tests use a temporary git
  repo.
- `LocalGitSource` requires a git repository. A plain folder Source is a
  separate implementation behind the same Protocol, written when it is needed.
- Unchanged files can be skipped by comparing shas, without reading content.

## Alternatives considered

- **GitHub API as the only Source:** needs a token and rate-limit handling from
  the start, puts the network into the tests, and does not cover a company that
  only hands over clones.
- **Walk the working directory:** ingests whatever is on disk, including
  ignored and uncommitted files, and gives no sha that matches GitHub.
- **Content hash (sha256) as identity:** works for any folder, but two Sources
  would disagree on the key for the same file.
