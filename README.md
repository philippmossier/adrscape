# cofabriq

An agentic software factory that devs use as a tool, not a review queue for them.

Small, low-risk issues go through **autopilot**: triaged, implemented, gated
by the repository's own CI, reviewed and handed to the dev as a draft PR.
Real features stay with the dev, who plans them in **planning mode** on a map
of the repository. Today, in 0.1: issue in, triage card out.

![cofabriq triage on two playground issues: a duplicate proven by a quote, then an issue with an injected instruction that the card ignores](docs/demo/triage.gif)

## Why it is built this way

Most "AI software factory" pipelines turn issues into pull requests and leave
humans reviewing all day. cofabriq keeps the dev in charge: autopilot only
takes issues that are small and low in blast radius (a type change, a typo, a
dependency bump), and everything else is planned by the dev with the map
answering what a change touches, which decisions constrain it and what
related work already exists.

- **A number per stage.** Every retrieval or agent change is measured on a real
  issue history ([open-webui](https://github.com/open-webui/open-webui), frozen
  snapshot). The numbers live in this README.
- **Safe by default on public repos.** Read-only until 0.3. After that, writes
  happen only on a local trigger by the dev, as a dry run unless `--apply` is
  passed. They are limited to one branch and a draft PR by a scoped token, not
  by a prompt. Issue text is untrusted input.
- **Devs own it.** One issue, one dev who triggers it, one branch, one PR that
  same dev reviews.

## Status

**0.1: issue in, triage card out (read-only).** Point it at a GitHub
repository and an issue number, get a triage card: type, severity, component,
a one-line summary, repro steps, questions for the reporter, a duplicate
verdict with a quoted sentence as evidence, related issues and related docs.

The first card from the GIF, as text:

```text
$ uv run cofabriq triage 11 --repo philippmossier/local-ai-chat-playground
✓ GitHub     #11 unchanged since the last fetch
✓ embedding  up to date
✓ search     10 earlier issues, 5 docs
✓ LLM        card fields + duplicate verdict, 2 calls, 4.3 s
✓ checks     component on the list, quote found in #1

philippmossier/local-ai-chat-playground#11 (open)
  https://github.com/philippmossier/local-ai-chat-playground/issues/11
  Refreshing the tab during a model download restarts the download instead of preserving its progress.
  type          bug
  severity      medium
  component     models
  repro         1. Start downloading a model.
                2. Refresh the tab while the download is in progress.
  missing       Which model were you downloading?
  duplicate     duplicate_of #1
                "After the reload the progress bar started again at 0% and it downloaded everything again."
  related issues (similarity)
                0.65  #1
                0.44  #5
                0.43  #7
  related docs (similarity)
                0.41  docs/REVIEW-GUIDE.md#Review guide > Known weak spots (where to look for bugs)
                0.40  docs/adr/0005-local-storage-for-chats-cache-storage-for-models.md#5. Chats in localStorage, ...
  run           $0.0003, 4.3 s, gpt-6-luna
```

How a card is made: if the issue is new or changed on GitHub, it is fetched
and embedded first. Its vector finds the nearest earlier issues and docs in
pgvector. Two LLM calls run in parallel, one for the card fields and
one for the duplicate verdict over the top 10 candidates. Plain code then
checks what the model returned: the component must be on the repository's
list (`targets/<owner>-<name>.toml`), severity is kept only on bugs, the
duplicate target must be one of the candidates and its quote must occur
verbatim in that issue. Anything that fails a check is dropped, not
repaired. So an injected instruction in an issue can at worst produce a
misleading summary, never a wrong component, an invented duplicate or a
write to GitHub. Where the two calls contradict each other (typed
`feature`, but a duplicate of a bug), the card lists the conflict and keeps
both reads: which one is wrong is unknown. The same card is served as the
`triage` MCP tool.

## Numbers

Measured on [open-webui](https://github.com/open-webui/open-webui)'s issue
history (10,302 issues, snapshot frozen 2026-10-13). Ground truth is what
its maintainers marked: 155 duplicate pairs from GitHub's duplicate marks
and human "duplicate of #N" comments, plus 155 non-duplicates. Each query
only sees issues filed before it, and only comments written before it.

| Dedupe, open-webui (n = 310)                 | Embedding threshold | LLM verdict 0.1.0 | LLM verdict 0.1.1 |
| -------------------------------------------- | ------------------- | ----------------- | ----------------- |
| `duplicate_of` precision                     | 0.250               | 0.346             | 0.434             |
| `duplicate_of` recall                        | 0.310               | 0.342             | 0.342             |
| precision / recall, any issue of the cluster | 0.292 / 0.361       | 0.399 / 0.394     | 0.484 / 0.381     |
| `unsure` (abstained), of which right         | -                   | -                 | 62, 4             |
| cost / median latency per issue              | embedding only      | $0.00066 / 2.2 s  | $0.00069 / 3.6 s  |

0.1.1 lets the verdict abstain (`unsure`: likely the same problem, but the
text can't confirm it) and asks for the same cause, not just the same
symptom. Both changed in one prompt version, so the gain isn't attributable
to abstention alone.

Retrieval: the original is in the vector top 5 for 49.7% of duplicates
(57.4% counting another duplicate of the same issue). The verdict can only
pick from the top 10, so retrieval caps it.

What the numbers say: the precision above is a lower bound, and the labels
are the bigger problem. A non-duplicate here only means "nobody marked it":
only 4 of the 155 were closed by a linked PR, so almost none is proven. A
hand check of the 63 claims the labels call wrong found 40 real duplicates
the maintainers never marked (or marked against another issue of the same
problem), 13 real errors and 10 unclear. The real errors are same-symptom,
different-cause calls (the same pip error on two Python versions, a
regression matched to the original feature request). That hand check is a
judgement, not a measurement, so it is not in the table; the next step is
an eval set whose labels are provable on both sides (0.1.2). Either way
the verdict only suggests: the dev decides.

Triage card, on the [playground](https://github.com/philippmossier/local-ai-chat-playground)
(24 seeded issues with 4 duplicate pairs and 4 injection attempts), three
runs of 0.1.1: type 22 to 24/24, component 23 to 24/24, duplicates 3 to
4/4 (the miss is an `unsure` on the right issue) with no false
`duplicate_of`, injections followed 0/4, $0.0003 and 3.8 s median per
issue. When the card's type and the verdict disagree (typed `feature`, but
a duplicate of a bug), the card lists a conflict instead of picking a
winner: 1 conflict in 3 runs, on a wrongly typed card. This is a
sanity check, not a benchmark: the issues and their expected labels were
written together. Type accuracy on messy real issues is still open.

Reproduce: `cofabriq eval dedupe` (the set is committed in `evals/`) and
`cofabriq eval triage --repo philippmossier/local-ai-chat-playground`.

### Underneath: the context layer

Ingests a repository's docs and manifests from local clones, embeds them
into pgvector for semantic search (the "related docs" on a card), extracts
entities with an LLM into a Neo4j graph and serves search and graph queries
as MCP tools. The 0.2 repo map grows out of it.

## How it fits together (target)

```mermaid
graph LR
    ISSUE(["GitHub issue"]) --> TRIAGE["Triage<br/>dedupe, type, component,<br/>repro, missing info"]
    TRIAGE --> CARD["Triage card"]
    MAP[("Repo map<br/>files, components, PRs,<br/>issues, decisions")]
    CARD --> PLAN["Planning mode<br/>impact of a plan"]
    CARD --> AUTO["Autopilot<br/>small + low blast radius only"]
    MAP --> PLAN
    MAP --> AUTO
    AUTO --> IMPL["Implement<br/>(delegated coding agent)"]
    IMPL --> CI["Target repo's CI"]
    CI -- "fail: fix, max 3, then escalate" --> IMPL
    CI --> REVIEW["Reviewer<br/>own context, decisions in force"]
    REVIEW --> PR(["Draft PR for the dev"])
    DEV(["Dev"]) --> PLAN
    DEV -- "triggers" --> AUTO
```

What cofabriq builds: triage, the repo map, the implement-and-fix loop, the
reviewer and blast-radius routing. What it consumes: the target repository's CI,
and a coding agent for implementation. What it does not read itself: source
code. The map gets its structure from file paths, manifests and git history;
the coding agent reads the code when it plans and implements a change.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Podman (or Docker) with compose,
an OpenAI key and a fine-grained GitHub token with "Public repositories
(read-only)" and no other permission. cofabriq never writes to GitHub.

```bash
make setup               # install deps, create .env (set OPENAI_API_KEY, GITHUB_TOKEN)
make up                  # Postgres + pgvector (5433), Neo4j (7474, 7687), test Neo4j (7688)
uv run cofabriq health   # OK per service
```

Triage an issue on the playground (a copy of a small local-first chat app
with seeded issues; it resets regularly, so numbers may shift):

```bash
REPO=philippmossier/local-ai-chat-playground
git clone https://github.com/$REPO ../local-ai-chat-playground
uv run cofabriq fetch-issues --repo $REPO    # all issues -> data/issues/ (read-only, incremental)
uv run cofabriq embed-issues --repo $REPO    # title + body into pgvector: the duplicate candidates
uv run cofabriq fetch ../local-ai-chat-playground && uv run cofabriq embed   # docs, for "related docs"
uv run cofabriq triage 11 --repo $REPO       # add --json for the raw card
```

The `fetch` and `embed` commands are a one-time sync (rerun them to pick up
new issues). After that, `triage <n>` is the only command: an issue filed a minute ago is
fetched and embedded on the spot.

Any other public repository works the same way. Add a
`targets/<owner>-<name>.toml` with its components first: the card picks
its component from that list. `uv run cofabriq mcp` serves the same card
to Claude Code (registered in `.mcp.json`).

Development: `make check` runs ruff, pyright (strict) and pytest.

## Roadmap

- [x] **Foundation (untagged):** context layer over a
      repository's docs: fetch, parse, chunk, embed, semantic search, LLM
      extraction, graph, MCP tools
- [x] **0.1: issue in, triage card out (read-only).** GitHub issues as a
      source, two-stage dedupe (vector candidates, LLM verdict with checked
      evidence), triage card, `triage` CLI and MCP tool. Numbers: recall@5,
      verdict precision and recall against an embedding baseline, cost and
      latency per issue
- [x] **0.1.1:** the verdict may abstain (`unsure`), card-verdict conflicts
      are flagged
- [ ] **0.1.2:** a dedupe eval set with provable labels on both sides
      (duplicates named by a maintainer action, non-duplicates closed by a
      merged PR or commit)
- [ ] **0.2: repo map + planning mode (read-only).** Components, files,
      co-change, PRs, issues and decisions in Neo4j; `impact` of an issue or
      a plan. Number: predicted vs actually touched components
- [ ] **0.3: autopilot.** Refinement, size and blast-radius gate, delegated
      implementation, the target's CI as the gate, fix loop with escalation,
      draft PR
- [ ] **0.4: review.** Reviewer with its own context, blast-radius routing
      (human review vs auto-merge eligible), run metrics

Milestones are 0.x releases, tagged in git: the API and the CLI may still
change. 1.0 comes when someone besides me relies on it.

Decisions are recorded in `docs/adr/` as they are made.

## How this is built

With Claude Code as a pair programmer. Architecture, scope and trade-offs are
mine and written down as ADRs; every change is reviewed before it lands.
