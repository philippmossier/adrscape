# cofabriq

An agentic software factory that devs use as a tool, not a review queue for them.

Small, low-risk issues go through **autopilot**: triaged, implemented, gated
by the repository's own CI, reviewed and handed to the dev as a draft PR.
Real features stay with the dev, who plans them in **planning mode** on a map
of the repository. Today, in 0.2: one command takes a bug report to a
draft PR, tried on a demo app with bugs planted on purpose.

What happens to one real bug report. A user filed
[#54](https://github.com/philippmossier/local-ai-chat-playground/issues/54):

> I wrote something at 11:50 PM and checked again at 12:10 AM. The message
> says "Today 11:50 PM". It was yesterday.

The dev runs `cofabriq run 54`:

1. **An agent reads the code and explains the bug.** The app counts 24-hour
   periods since the message, not calendar days: 20 minutes after midnight
   is less than a day, so it says "Today". Its plan: compare the two dates'
   midnights instead.
2. **The dev approves the plan.** No code is written before that.
3. **An agent writes a test that fails on the bug:** a message from 23:50,
   seen at 00:10, must say "Yesterday". It fails, so the bug is real.
4. **An agent fixes the code until that test passes.** It can't change the
   test. The project's own checks (types, lint, all tests) run in an
   isolated container.
5. **A second agent reviews the fix** without the first one's reasoning. It
   can stop the PR only by writing a test that fails on the fix.
6. **The dev gets a draft PR** with the plan, the test and the review, and
   decides whether to merge it. Example:
   [#80](https://github.com/philippmossier/local-ai-chat-playground/pull/80).

About 2 minutes and $0.35 for this bug.

> <details><summary><b>Show the real output of each step</b> (the same run, shortened)</summary>
>
> **1. The agent's spec**, shown to the dev:
>
> ```text
> problem
>   `messageTime` in src/lib/chat/time.ts:10 computes `days = Math.floor((now - at) / DAY)`.
>   That number is the elapsed time divided by 24 h. A message sent at 23:50 and viewed
>   at 00:10 the next day is 20 minutes old, so `days = 0` [...]
> plan
>   1. In src/lib/chat/time.ts, add a helper that returns local midnight for a timestamp.
>   2. Replace line 10 with days = Math.round((startOfDay(now) - startOfDay(at)) / DAY).
>   [...]
> open questions
>   Did the reporter reload the page, or was the tab left open across midnight? [...]
> ```
>
> **2. The plan gate:**
>
> ```text
> Approve the plan and let the agent edit? [y/N]: y
> ```
>
> **3. The agent's test**, and how it fails on the current code:
>
> ```ts
> it("a message just before midnight is yesterday", () => {
>   expect(messageTime(at(9, 4, 23, 50), earlyMonday, "en")).toMatch(/^Yesterday 11:50\sPM$/);
> });
> ```
>
> ```text
> AssertionError: expected 'Today 11:50 PM' to match /^Yesterday 11:50\sPM$/
> Tests  4 failed | 5 passed (9)
> ```
>
> **4. The fix**, then the project's checks, run in a container:
>
> ```diff
> -  const days = Math.floor((now - at) / DAY);
> +  // Calendar days in local time; Math.round absorbs 23 h / 25 h DST days.
> +  const days = Math.round((startOfDay(now) - startOfDay(at)) / DAY);
> ```
>
> ```text
> fix 1  agent $0.047 5 turns 10 s | repro:ok(1s) types:ok(5s) lint:ok(4s) test:ok(2s)
> ```
>
> **5. The review:**
>
> ```text
> review   approved ($0.053 4 turns 10 s)
>          Approve: the change counts local calendar days for both the Today/Yesterday
>          and weekday cutoffs, as the spec asks [...]
>          nit src/lib/chat/time.test.ts:31: Plan step 4 mentions DST-independent tests,
>          but no case crosses a DST change. [...]
> ```
>
> **6. The top of the draft PR body:**
>
> ```markdown
> Fixes #54.
>
> - **Risk: low** (chat)
> - **Review: approved.** Approve: the change counts local calendar days [...]
> - **Checks:** the test was red before the change; repro, types, lint, test green after
>
> ### Plan (approved before any code)
> ```
>
> </details>

## Why it is built this way

- **The dev stays in charge.** Autopilot only takes small, low-risk issues;
  the dev approves every plan before code exists and every PR before it is
  merged. One issue, one dev, one branch, one draft PR.
- **A claim isn't evidence, a failing test is.** In a timed review of 10
  draft PRs, 3 of them wrong, the AI reviewer had flagged all 3 and I still
  merged 2: after a few green PRs a prose warning stops registering. So a
  reviewer can only block a PR with a test that fails on it
  ([ADR-008](docs/adr/008-only-a-red-test-blocks.md)).
- **Safe on public repos.** Issue text is untrusted input. Code the agent
  writes runs only in a container without network
  ([ADR-007](docs/adr/007-agent-written-code-runs-only-in-a-sandbox.md)).
  The only write to GitHub is one branch and a draft PR, on an explicit
  `--apply`, with a scoped token.
- **A number per stage.** Every agent or retrieval change is measured
  ([evals](docs/evals.md)).

## Numbers

On the 10 planted logic bugs of the demo app:

| | |
| --- | --- |
| fixed, with the agent's own failing test first | 10/10, all on the first try |
| right fixes let through by the review gate | 10/10 |
| wrong fixes caught by the review gate and repaired before the PR | 8/8 |
| cost per bug, end to end | about $0.35 |

The wrong fixes were written wrong on purpose (an agent told to make a
plausible but wrong fix), since the loop itself made none. Early and easy: one-file bugs in a small repo, one model in every role.
Next: UI bugs and real issues. Details and caveats: [docs/evals.md](docs/evals.md).

## Triage (0.1)

Before any of that, an issue gets a triage card: type, component, a
duplicate verdict with a quote as evidence, missing info with a reply
draft for the reporter. Measured on real issues of
[microsoft/vscode](https://github.com/microsoft/vscode) and
[open-webui](https://github.com/open-webui/open-webui).

<img src="docs/demo/triage.gif" width="820" alt="cofabriq triage on two playground issues: a likely duplicate proven by a quote, routed to the dev with a reply draft for the missing version, then an issue with an injected instruction that the card ignores, routed ready">

How each part works: [docs/how-it-works.md](docs/how-it-works.md).

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Podman (or Docker) with compose,
an OpenAI key and a fine-grained GitHub token with "Public repositories
(read-only)" and no other permission. Without `run --apply`, cofabriq
never writes to GitHub.

```bash
make setup               # install deps, create .env (set OPENAI_API_KEY, GITHUB_TOKEN)
make up                  # Postgres + pgvector (5433), Neo4j (7474, 7687), test Neo4j (7688)
uv run cofabriq health   # OK per service
```

Triage an issue on the playground (a small local-first chat app with
seeded issues):

```bash
REPO=philippmossier/local-ai-chat-playground
git clone https://github.com/$REPO ../local-ai-chat-playground
uv run cofabriq fetch-issues --repo $REPO    # all issues -> data/issues/ (read-only, incremental)
uv run cofabriq embed-issues --repo $REPO    # title + body into pgvector: the duplicate candidates
uv run cofabriq fetch ../local-ai-chat-playground && uv run cofabriq embed   # docs, for "related docs"
uv run cofabriq triage 11 --repo $REPO       # add --json for the raw card
```

Run the loop on a playground issue (needs an Anthropic key, the
[`claude`](https://docs.anthropic.com/en/docs/claude-code) CLI on the PATH
and Podman; about $0.35 per issue):

```bash
uv run cofabriq refine 54 --repo $REPO     # the spec only: read-only agent, prints it
uv run cofabriq run 54 --repo $REPO        # refine, plan gate, repro, fix, review gate, PR body (dry run)
```

`--apply` opens the draft PR on GitHub (needs `GITHUB_WRITE_TOKEN`,
playground only).

Development: `make check` runs ruff, pyright (strict) and pytest.

## Roadmap

- [x] **0.1** triage: duplicates, missing info, a route per issue
- [x] **0.2** the core loop: plan, failing test, fix, review gate, draft PR
- [ ] **Next:** harder bugs (UI, real issues of a real repo), a different
      reviewer model, triage feeding the loop
- [ ] **Later:** planning mode for features, more target repos

0.x releases: the CLI may still change. Decisions are recorded in
[`docs/adr/`](docs/adr/) as they are made.

## How this is built

With Claude Code as a pair programmer. Architecture, scope and trade-offs are
mine and written down as ADRs; every change is reviewed before it lands.
