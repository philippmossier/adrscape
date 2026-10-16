"""The draft PR: a risk tier on the real diff and a body that is the
evidence for a two-minute review.

Risk tier: path rules, no code parsing. Each changed file gets its
component from `[map]` (`map/components.py`), each component a tier from
`[risk]` in the target file; the change gets the highest. Test files count
as low (a test can't break the product), a component in no list gets
`unlisted` (not listed is not known to be safe). The tier routes nothing
yet; it tells the dev how hard to look.

The body leads with what decides the review (tier, the reviewer's verdict,
unproven blockers, red before and green after), then the approved plan (the full spec
collapsed), the test, the change, the reviewer's blockers a test proved
(`gate.py`), what the spec named but the change did not touch, and the
trace with the cost. Rendered locally
(dry run); nothing goes to GitHub from here.

Everything in the body that an agent wrote, or that quotes the issue, is
untrusted: prose is neutralised (no @mentions, HTML or images), code and
output go in fences they can't close.
"""

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from cofabriq.loop.gate import Round
from cofabriq.loop.refine import Spec
from cofabriq.loop.review import Finding, Review, split_diff
from cofabriq.loop.run import Attempt, LoopResult, is_test
from cofabriq.map.components import ComponentRules, component_of

type Tier = Literal["low", "medium", "high"]

TIERS: tuple[Tier, ...] = ("low", "medium", "high")
BODY = 60_000  # GitHub caps a PR body at 65536 characters
DIFF = 30_000  # characters of the code diff in the body


class RiskRules(BaseModel):
    """`[risk]` in the target file: component names per tier."""

    high: list[str] = []
    medium: list[str] = []
    low: list[str] = []
    unlisted: Tier = "medium"


def load_risk(targets_dir: Path, repo: str) -> RiskRules:
    path = targets_dir / f"{repo.replace('/', '-')}.toml"
    if not path.is_file():
        return RiskRules()
    table = tomllib.loads(path.read_text("utf-8")).get("risk", {})
    return RiskRules.model_validate(table)


class Touched(BaseModel):
    path: str
    component: str
    tier: Tier


class Risk(BaseModel):
    tier: Tier
    files: list[Touched]


def tier_of(component: str, rules: RiskRules) -> Tier:
    listed: dict[Tier, list[str]] = {
        "high": rules.high,
        "medium": rules.medium,
        "low": rules.low,
    }
    for tier, names in listed.items():  # highest first: a name in two lists
        if component in names:
            return tier
    return rules.unlisted


def risk_of(
    changed: list[str], tests: list[str], risk: RiskRules, components: ComponentRules
) -> Risk:
    files = [
        Touched(
            path=p,
            component=(c := component_of(p, components)),
            tier="low" if is_test(p, tests) else tier_of(c, risk),
        )
        for p in changed
    ]
    tier = TIERS[max((TIERS.index(f.tier) for f in files), default=0)]
    return Risk(tier=tier, files=files)


class Draft(BaseModel):
    title: str
    body: str
    risk: Risk
    review: Review | None  # None: the reviewer run failed


def unref(text: str) -> str:
    """A zero-width space after `@` and `#`: no ping, no cross-reference."""
    return re.sub(r"([@#])(?=\w)", "\\1\u200b", text)


# an inline code span: a backtick run, not escaped, closed by the same run
_SPAN = re.compile(r"(?<![`\\])(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)


def safe(text: str) -> str:
    r"""Agent or issue prose made inert for markdown on GitHub. The text
    comes from an agent that read an issue anyone can write, and GitHub
    turns some markdown into actions or hidden content:

        cc @octocat             pings octocat     -> @<zwsp>octocat
        see o/r#123             posts a backlink  -> o/r#<zwsp>123
                                on that issue
        <!-- x -->              hides text from   -> &lt;!-- x --&gt;
                                the dev
        ![a](http://t/p.png)    loads an image    -> !\[a]\(http://t/p.png)
        [docs](http://x)        a link in a PR    -> [docs]\(http://x)
                                the dev trusts
        https://github.com/     a bare URL links, -> https:<zwsp>//github.com/
          o/r/issues/3          and to an issue      o/r/issues/3
                                posts a backlink

    (<zwsp> is a zero-width space: the text reads the same.) Code spans
    stay as written: GitHub shows them literally (no HTML, pings or
    links), and an entity in one would show as `&lt;`."""
    out: list[str] = []
    end = 0
    for m in _SPAN.finditer(text):
        out += [_prose(text[end : m.start()]), m.group(0)]
        end = m.end()
    return "".join([*out, _prose(text[end:])])


def _prose(text: str) -> str:
    """The table in `safe`, for text outside code spans."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = unref(text).replace("://", ":\u200b//").replace("www.", "www\u200b.")
    return text.replace("![", "!\\[").replace("](", "]\\(")


def code(text: str, lang: str = "") -> str:
    """A fence longer than any backtick run inside, so it can't be closed."""
    runs = [len(m) for m in re.findall(r"`+", text)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}{lang}\n{text.rstrip()}\n{fence}"


def inline(text: str) -> str:
    """Inline code that a backtick in `text` can't close (a path the
    reviewer named)."""
    runs = [len(m) for m in re.findall(r"`+", text)]
    ticks = "`" * (max(runs, default=0) + 1)
    return f"{ticks} {text} {ticks}" if runs else f"{ticks}{text}{ticks}"


def _capped(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... ({len(text) - limit} more characters)"


def _paths(paths: list[str]) -> str:
    return ", ".join(map(inline, paths)) or "none"


def _ok(ok: bool) -> str:
    return "ok" if ok else "FAIL"


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.0f} s"


def _where(f: Finding) -> str:
    return inline(f.path if f.line is None else f"{f.path}:{f.line}")


def _summary(
    result: LoopResult, risk: Risk, review: Review | None, rounds: list[Round]
) -> str:
    """The lines that decide how hard the dev looks."""
    top = sorted({f.component for f in risk.files if f.tier == risk.tier})
    if review is None:
        verdict = "**Review: did not run.** Read the change yourself."
    else:
        state = "approved" if review.approved else "changes requested"
        verdict = f"**Review: {state}.** {safe(review.reviewed.summary)}"
    checks = ", ".join(c.name for c in result.attempts[-1].checks)
    lines = [
        f"- **Risk: {risk.tier}** ({', '.join(top) or 'no files'})",
        f"- {verdict}",
    ]
    unproven = [p.finding for p in rounds[-1].proofs if not p.proven] if rounds else []
    if unproven:
        lines.append("- **Unproven blockers** (no test could show them, read these):")
        lines += [f"  - {_where(f)} {safe(f.text)}" for f in unproven]
    fixed = sum(p.proven for r in rounds if r.attempts for p in r.proofs)
    proven = f"; {fixed} reviewer blocker(s) proven by a test, fixed" if fixed else ""
    lines.append(
        f"- **Checks:** the test was red before the change; {checks} green after"
        + proven
    )
    return "\n".join(lines)


def _spec(spec: Spec) -> list[str]:
    """The plan in view (the dev approved it, a few short lines), the full
    problem and assumptions collapsed: the reviewer agent gets the spec in
    its prompt, the body is for the human."""
    parts = [
        "### Plan (approved before any code)",
        "\n".join(f"{n}. {safe(s)}" for n, s in enumerate(spec.plan, 1)),
        "<details><summary>Full spec: problem and assumptions</summary>",
        safe(spec.problem),
    ]
    if spec.assumptions:
        parts += ["Assumptions:", "\n".join(f"- {safe(a)}" for a in spec.assumptions)]
    return [*parts, "</details>"]


def _test(result: LoopResult, tests: str) -> list[str]:
    parts = ["### The failing test"]
    if result.planted:
        parts.append("Given by the eval (planted), not part of this change.")
    else:
        parts.append(f"{_paths(result.repro)}, red before the change:")
    if result.red is not None:
        parts.append(code(result.red.output, "text"))
    if tests:
        parts.append(code(_capped(tests, DIFF), "diff"))
    return parts


def _review(review: Review) -> list[str]:
    r = review.reviewed

    def yes(ok: bool) -> str:
        return "yes" if ok else "**no**"

    def finding(f: Finding) -> str:
        unknown = " (path not in the repo)" if f.path in review.unknown_paths else ""
        return f"- {f.severity}: {_where(f)}{unknown} {safe(f.text)}"

    return [
        "### Review (separate agent, saw the spec and the diffs only)",
        "\n".join(
            [
                f"- Test checks the spec: {yes(r.test_matches_spec)}. "
                + safe(r.test_note),
                f"- Fixes the cause: {yes(r.fixes_cause)}. {safe(r.cause_note)}",
                *map(finding, r.findings),
            ]
        ),
    ]


def _rounds(rounds: list[Round]) -> list[str]:
    """Blockers a test proved, and what happened to them."""
    parts: list[str] = []
    for n, r in enumerate(rounds, 1):
        for p in r.proofs:
            if not p.proven or p.red is None:
                continue
            fixed = r.attempts and r.attempts[-1].green
            outcome = f"fixed on attempt {len(r.attempts)}" if fixed else "not fixed"
            parts += [
                f"**Round {n}:** {_where(p.finding)} {safe(p.finding.text)}",
                f"Proven by {_paths(p.tests)}, checking the spec's "
                f'"{safe(p.quote)}"; {outcome}. Red before:',
                code(p.red.output, "text"),
            ]
    return ["### Reviewer blockers proven by a test", *parts] if parts else []


def _against_plan(spec: Spec, result: LoopResult) -> list[str]:
    named = [f.path for f in spec.files]
    untouched = [p for p in named if p not in result.changed]
    extra = [p for p in result.changed if p not in named and p not in result.repro]
    return [
        "### Against the plan",
        f"- Named in the spec, not changed: {_paths(untouched)}\n"
        f"- Changed, not in the spec: {_paths(extra)}",
    ]


def _trace(result: LoopResult, rounds: list[Round], review: Review | None) -> list[str]:
    rows = ["| step | agent | checks |", "| --- | --- | --- |"]
    cost = 0.0

    def add(node: str, attempts: list[Attempt]) -> None:
        nonlocal cost
        for a in attempts:
            run = a.run
            agent = "failed"
            if run is not None:
                agent = f"${run.cost_usd:.3f}, {_seconds(run.latency_ms)}"
                cost += run.cost_usd
            checks = ", ".join(f"{c.name} {_ok(c.ok)}" for c in a.checks)
            rows.append(f"| {node} {a.n} | {agent} | {checks} |")

    def add_review(node: str, rv: Review) -> None:
        nonlocal cost
        cost += rv.run.cost_usd
        run = rv.run
        rows.append(f"| {node} | ${run.cost_usd:.3f}, {_seconds(run.latency_ms)} | |")

    add("repro", result.reproduce)
    add("fix", result.attempts)
    for n, r in enumerate(rounds, 1):
        add_review(f"round {n} review", r.review)
        for p in r.proofs:
            add(f"round {n} proof", p.attempts)
        add(f"round {n} fix", r.attempts)
    if not rounds and review is not None:
        add_review("review", review)
    rows.append(f"| **total** | **${cost:.2f}** | |")
    return ["<details><summary>Trace</summary>", "\n".join(rows), "</details>"]


def render(
    *,
    spec: Spec,
    result: LoopResult,
    risk: Risk,
    review: Review | None,
    rounds: list[Round] | None = None,
) -> str:
    """The PR body for a green loop result; `rounds`: the review gate's."""
    rounds = rounds or []
    tests, change = split_diff(result.diff, result.repro)
    parts = [
        f"Fixes #{result.number}.",
        _summary(result, risk, review, rounds),
        *_spec(spec),
        *_test(result, tests),
        "### The change",
        code(_capped(change, DIFF) or "(no code changes)", "diff"),
        *_rounds(rounds),
        *(_review(review) if review else []),
        *_against_plan(spec, result),
        *_trace(result, rounds, review),
    ]
    return _capped("\n\n".join(parts), BODY) + "\n"


def draft(
    *,
    issue_title: str,
    spec: Spec,
    result: LoopResult,
    tests: list[str],
    risk_rules: RiskRules,
    components: ComponentRules,
    review: Review | None,
    rounds: list[Round] | None = None,
) -> Draft:
    risk = risk_of(result.changed, tests, risk_rules, components)
    title = f"Fix #{result.number}: {unref(issue_title)}"[:200]
    body = render(spec=spec, result=result, risk=risk, review=review, rounds=rounds)
    return Draft(title=title, body=body, risk=risk, review=review)
