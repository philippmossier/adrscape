# ADR-007: Code the agent writes runs only in a sandbox

## Status

Accepted, 2026-10-16.

## Context

Step 6 of 0.2 is the first time cofabriq lets a coding agent change code:
Claude Code fixes a reproduced bug in a worktree of the target repo, and
cofabriq checks the fix by running the target's repro, type check, lint
and tests.

The agent is steered by issue text, and anyone can write an issue. The
edits alone are contained: `claude -p --restricted` keeps file tools
inside the worktree and refuses writes to git and settings files. The
risk is what happens next. `pnpm test` executes the code the agent just
wrote. Run on the host, an issue that talks the agent into "also add a
test that reads ~/.ssh and posts it somewhere" gets that code executed
with the developer's files and network.

## Decision

- **The agent gets no shell.** Its tools are Read, Grep, Glob, Edit and
  Write. It can't run the tests itself; cofabriq runs them and returns the
  failing output (the loop's back pressure).
- **Every target command runs in a throwaway container** (`podman run
  --rm`, `loop/sandbox.py`). The only host folder it sees is the worktree,
  mounted at `/app`; package caches live in podman volumes.
- **Only the install has network**, and it runs before the agent has
  changed anything: the target's own lockfile, installed as a developer
  would. Repro and checks run with `--network none`.
- **Protected files are put back** after each attempt (`[loop] protected`
  in the target file: manifests, lockfile, test and lint config, and the
  repro itself), so a fix can't pass by weakening the gate.
- **The commands come from the target file** (`[loop]`: image, setup,
  install, repro, checks), so a second target needs config, not code.

## Consequences

- Prompt injection through an issue costs a bad diff, not the host. The
  diff still goes through the reviewer and a human before any merge
  (step 7).
- podman becomes a dependency of `cofabriq run` (it already runs the
  databases). The install happens in the container because macOS
  `node_modules` carry native binaries that don't run on Linux.
- About 20-50 s of setup per run, mostly the install; checks start a
  container each (1-5 s). Measured on the planted logic bugs: 69 s median
  per bug, end to end.
- The agent iterates more slowly than with a shell: it gets test output
  only between attempts. On the planted logic bugs every fix was green on
  the first attempt, so that cost hasn't shown yet.
- The sandbox covers what cofabriq runs. It doesn't cover the developer
  later running the branch locally; that's the review's job.

## Alternatives considered

- **Checks on the host.** Simplest and fastest; rejected because the host
  then executes code derived from untrusted text.
- **Bash for the agent, inside the container.** The agent could run tests
  itself and iterate faster. It would then decide when and how checks run,
  and the container would have to contain an agent, not just code; the
  loop would lose its fixed gate and countable attempts. Worth revisiting
  if attempts start failing for lack of feedback.
- **Agent SDK permission hooks (`can_use_tool`)** to vet each tool call.
  Guards the agent's actions, not the execution of the code it wrote,
  which was the actual risk.
