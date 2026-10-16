# Evals

Every retrieval or agent change gets a number. The 0.2 loop is measured on
bugs planted in a demo app (truth by construction), triage on real issue
histories (frozen snapshots).

## 0.2: the loop on planted bugs

The [playground](https://github.com/philippmossier/local-ai-chat-playground)
(a small local-first chat app) has 22 planted bugs, each filed as a
user-style issue: 10 logic bugs with a vitest repro, 12 UI bugs with a
Playwright repro (15 small, 6 medium, 1 large). Each repro is red on the
planted code and green with only its own fix, so the truth is known by
construction and can't be in any model's training data. The loop runs on
the 10 logic bugs; UI repros aren't in the loop yet.

| Stage (Claude, one model in every role)                  | Result                                                     | Cost per bug      |
| -------------------------------------------------------- | ---------------------------------------------------------- | ----------------- |
| refine, all 22: spec names the fix's file                | hit@1 1.00, precision 0.88; size right 16/22               | $0.18, 34 s       |
| loop, 10 logic bugs: own failing test, fix               | reproduced 10/10, test valid 10/10, resolved 10/10, all first try | $0.105, 97 s |
| reviewer alone: 10 right fixes, 9 proven wrong           | right approved 10/10, wrong rejected 9/9                   | $0.057            |
| review gate: 10 right fixes, 8 proven wrong              | right published 10/10; wrong fixed 8/8, leaked 0; 10 proof tests, all valid | $0.137 |
| human round: 10 draft PRs, 3 wrong, before the gate      | 1.2 min per PR (median), 1.6 per merged PR; wrong merged 2/3 | -               |

End to end, a right fix costs about $0.34 (refine, loop, one review) and
about 2.5 minutes of agent time; the dev's part is reading the spec at the
plan gate and the draft PR. Human minutes per merged PR: 1.6 leaving out
the first PR, whose 9.5 minutes went into changing the body format; 2.5
with it.

"Proven wrong": an implementer told to make a plausible wrong fix (wrong on
purpose, green on its own test). The planted repro caught only 1 of 10,
since it covers the core case only, so a third agent writes a witness test
for the hole; a fix counts as wrong when the witness is red on it and green
with the reference fix (7 or 8 of 9 confirmed, the witness isn't
deterministic). "Valid" proof test: green with the reference fix, so it
encodes a right reading of the spec.

What it says: the mechanism works, the sets don't discriminate. Every
stage is saturated: one-file bugs in a small repo, a spec that names the
cause, the same model writing, sabotaging and reviewing, and wrong fixes
that are sabotage, not natural mistakes. The human round is one reviewer
(me, who also planted the bugs) on 10 PRs. Next difficulty bars: UI bugs,
real issues, a different reviewer model.

Reproduce: `cofabriq eval refine`, `eval loop`, `eval loop --wrong`,
`eval witness`, `eval review`, `eval gate` (all paid, results in `data/`).

## 0.2: impact from the repo map

A map of a repo from paths and git history only (components, co-change,
fix commits linked to issues). From the issue text alone, `impact` ranks
the components a fix will touch: on microsoft/vscode (405 components), a
component the fix touched is in the top 5 for 75% of issues on a held-out
year (2024, n = 300; hit@10 0.83). A popularity baseline got 22% on the 2023
split.
No LLM call. Kept as an MCP tool, not as the way the loop finds code: the
agent reads the code. Reproduce: `cofabriq eval impact [--test]`.

## 0.1: duplicates and missing info

Duplicate detection is measured on two real issue histories. Each query only
sees issues filed before it, and only comments written before it.

**microsoft/vscode, provable labels (0.1.2).** 62,467 issues (created
2022-2024, snapshot frozen 2026-10-15); queries are 2023-2024. A duplicate
counts only if a maintainer set vscode's `*duplicate` label and a human
comment names the target (`/duplicate #N`). A non-duplicate counts only if
it was closed as completed by a merged PR or a commit. 2025 on is left out:
maintainer accounts there post LLM-written triage comments, and labels
suggested by an LLM would make an LLM eval circular. The rules are in
[`targets/microsoft-vscode.toml`](../targets/microsoft-vscode.toml), written
before scoring; 200 pairs and 200 non-duplicates sampled with a fixed seed.

| Dedupe, vscode (n = 400), verdict 0.1.1      | Embedding threshold | LLM verdict      |
| -------------------------------------------- | ------------------- | ---------------- |
| `duplicate_of` precision                     | 0.212               | 0.346            |
| `duplicate_of` recall                        | 0.145               | 0.235            |
| precision / recall, any issue of the cluster | 0.372 / 0.255       | 0.515 / 0.350    |
| `unsure` (abstained), of which right         | -                   | 88, 11 (cluster) |
| cost / median latency per issue              | embedding only      | $0.00073 / 1.9 s |

What it says: about half of the `duplicate_of` calls are right, so the
verdict is a suggestion for the dev, never an action. Of the 136 calls, 32
hit a provable non-duplicate, and only 1 of those shares a fix with its
target. Most are a narrow follow-up called a duplicate of the broader
feature issue ("Fine tune `clear` alert" vs "Alert screen reader users that
something has occurred"). Retrieval caps the rest: in a 62k pool the target
is in the top 10 for 40% of duplicates (56% counting its cluster).

Known gaps of the set: 21% of otherwise usable pairs name a target from
before 2022 and are dropped, so long-lived issues are under-represented.
Non-duplicates closed by a PR lean towards team-filed work items, which are
harder than typical user reports.

Tried and dropped: hybrid retrieval (Postgres full-text search on the
query's rarest words, fused with the vector list by reciprocal rank). Top-10
recall moved within noise on vscode (0.40 to 0.42), dropped on open-webui
(0.568 to 0.561), and top-1 got worse on both.

**open-webui, maintainer marks (0.1.0, 0.1.1).**
[open-webui](https://github.com/open-webui/open-webui), 10,302 issues,
snapshot frozen 2026-10-13: 155 duplicate pairs from GitHub's duplicate
marks and human "duplicate of #N" comments, plus 155 issues nobody marked.

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
regression matched to the original feature request). That hand check was
an LLM's judgement plus a spot check, and the vscode set above corrected
it: on provable labels, the precision is about 0.5, not 0.8.

**Missing info, microsoft/vscode (0.1.3).** Does the card ask for more
exactly when a maintainer did? Positive: the maintainers labelled the
report `info-needed`. Negative: a `bug` fixed by a merged PR or a commit
that never carried that label (label history checked on GitHub). Both sides
filed through vscode's issue template (user reports, not team notes) with
an unedited body. 200 + 200 per split; the method was chosen on 2023-2024
and scored once on a held-out 2022 split.

| Needs info, vscode, held-out 2022 (n = 400) | Flagged | Precision | Recall |
| ------------------------------------------- | ------- | --------- | ------ |
| the model asks any question (0.1.2 card)    | 217     | 0.733     | 0.795  |
| required items without a verified quote     | 61      | 0.787     | 0.240  |
| questions the model marks blocking          | 70      | 0.871     | 0.305  |
| **route `needs_info` (either of the two)**  | 94      | 0.787     | 0.370  |

What it says: when the card routes to `needs_info`, a maintainer asked too
in about 4 of 5 cases, but it catches only about a third of the reports
maintainers asked about. Most maintainer asks are issue-specific ("a gif
would help", "does it happen on Insiders", "share a repo I can test
with"), which no checklist knows; the model's questions catch most of them
but also fire on 41% of bugs that were fixed as written, so they stay on
the card as questions, not as a route. On the dev split, blocking
precision was 0.868: the judgement held on unseen data. $0.0003 and 3.5 s
per issue.

Triage card, on the [playground](https://github.com/philippmossier/local-ai-chat-playground)
(24 seeded issues with 4 duplicate pairs and 4 injection attempts), three
runs of 0.1.1 (one run of 0.1.3: 24/24, 24/24, 4/4, 0/4): type 22 to 24/24, component 23 to 24/24, duplicates 3 to
4/4 (the miss is an `unsure` on the right issue) with no false
`duplicate_of`, injections followed 0/4, $0.0003 and 3.8 s median per
issue. When the card's type and the verdict disagree (typed `feature`, but
a duplicate of a bug), the card lists a conflict instead of picking a
winner: 1 conflict in 3 runs, on a wrongly typed card. This is a
sanity check, not a benchmark: the issues and their expected labels were
written together. Type accuracy on messy real issues is still open.

Reproduce: `cofabriq eval readiness --repo microsoft/vscode [--test]`,
`cofabriq eval dedupe --repo <repo> [--verdict]` (the sets are
committed in `evals/`; vscode needs `fetch-issues --repo microsoft/vscode
--created 2022-01-01..2024-12-31` first, about an hour) and
`cofabriq eval triage --repo philippmossier/local-ai-chat-playground`.
