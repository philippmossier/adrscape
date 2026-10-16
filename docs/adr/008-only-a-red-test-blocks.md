# ADR-008: Only a red test blocks a PR

## Status

Accepted, 2026-10-16.

## Context

Step 8 of 0.2 adds a reviewer after the green loop: a read-only agent with
its own context gets the spec, the test diff and the code diff, and
returns findings and a verdict. The first version only informed: the
verdict and its findings went into the draft PR body, the dev decided.

The human round (`eval human`) tested that. Ten draft PRs on the
playground, three of them proven wrong (a witness test is red on the fix,
green with the reference fix), shuffled. The reviewer had rejected all
three, with the blockers at the top of each body. The dev reviewed in
1.2 minutes per PR (median) and merged two of the three wrong ones. After
a few green PRs the verdict line no longer registered, and a dev can't
check a claim like "this rounds instead of floors across DST" without
running the code, which a two-minute review doesn't do.

A prose verdict next to green checks loses. The open question was what
may block instead.

## Decision

- **A reviewer blocker blocks only once a test proves it.** Per blocker,
  an agent writes a proof test (test files only). Code accepts it only if
  it quotes a line of the spec the dev approved, is red on the change,
  and passes types and lint; max 3 tries.
- **A proven blocker goes back to the implementer**, with the proof tests
  as extra repro it can't change (max 3 tries), then a second review. A
  proven blocker left after that means **blocked**: no PR, a human takes
  over.
- **An unproven blocker doesn't block.** It is shown at the top of the PR
  body as "unproven" and the PR is opened.
- **Code owns every rule** (`loop/gate.py`): the quote check, red, the
  check run, the round and try caps. The agents only write tests and
  findings.

## Consequences

- Every block comes with evidence a dev can check in a minute: a red test
  that ships in the PR and names the spec line it encodes.
- On the gate eval (10 right fixes, 8 proven wrong ones, same model in
  every role): no right fix blocked, all 8 wrong fixes fixed by the round,
  none leaked; the 10 proof tests were all green with the reference fix,
  so none encoded a wrong reading. The set is saturated and the wrong
  fixes are planted, so this shows the mechanism works, not how often it
  will matter on real issues.
- A proof proves the claim, not that the claim is right. Without a
  reference fix nothing checks the expectation itself; the spec quote and
  the dev reading the test are the guard. A spec that is wrong produces
  proofs that are wrong in the same way.
- Blockers that resist a unit test (races, layout, security smells) only
  ever show as unproven. Watching how often a real one lands there is the
  measure of this decision's blind spot.
- Cost: a right fix pays one review ($0.05 mean in the eval); a wrong one
  pays proofs, a fix round and a second review ($0.25 mean).

## Alternatives considered

- **Verdict in the PR body only.** The first version; the human round
  showed it doesn't stop a merge.
- **The reviewer's "reject" blocks directly.** No evidence for the dev,
  and a false block costs a dev investigation each time. The reviewer was
  right 19 of 19 times in the eval, but on planted bugs with the cause
  named in the spec; that doesn't carry to real issues.
- **An LLM judge over reviewer and diff.** A second opinion is still
  prose; it moves the trust problem, it doesn't remove it.
