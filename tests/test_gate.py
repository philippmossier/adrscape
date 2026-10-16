from collections.abc import Mapping
from pathlib import Path

from cofabriq.eval.gate import GateCase
from cofabriq.eval.review import Kind
from cofabriq.loop.engine import AgentRun
from cofabriq.loop.gate import Gated, quoted, review_gate
from cofabriq.loop.pr import Risk, render
from cofabriq.loop.refine import Spec
from cofabriq.loop.run import CheckResult, LoopResult, run_loop
from tests.gitrepo import git
from tests.test_loop import (
    AGENT_RULES,
    REPRO,
    Edit,
    HostSandbox,
    both,
    setup,
    write,
)
from tests.test_pr import Reviewer, reviewed

SPEC = Spec.model_validate(
    {
        "problem": "src/a.ts must say exactly fixed, nothing more.",
        "files": [{"path": "src/a.ts", "why": "the word"}],
        "tests": [],
        "plan": ["Write fixed into src/a.ts."],
        "assumptions": [],
        "open_questions": [],
        "size": "S",
    }
)
EXACT = 'grep -qx fixed "$(dirname "$0")/a.ts"\n'  # the proof: exact match
BLOCKER = {"path": "src/a.ts", "line": 1, "severity": "blocker", "text": "not exact"}


class Script:
    """An editing agent: one (edit, answer) per call."""

    def __init__(self, *steps: tuple[Edit | None, Mapping[str, object]]) -> None:
        self.steps = list(steps)
        self.prompts: list[str] = []

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        self.prompts.append(prompt)
        edit, answer = self.steps.pop(0)
        if edit is not None:
            edit(cwd)
        output = dict(answer)
        return AgentRun(output=output, model="f", cost_usd=0.1, turns=1, latency_ms=1)


class Reviews(Reviewer):
    """One review per call, in order."""

    def __init__(self, *answers: Mapping[str, object]) -> None:
        super().__init__(reviewed())
        self.answers = [reviewed(**dict(a)) for a in answers]

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        self.answer = self.answers.pop(0)
        return super().__call__(prompt, schema, cwd)


def half_fixed(tmp_path: Path) -> tuple[LoopResult, Path]:
    """A green loop whose fix is wrong: `fixed-ish` passes `grep -q fixed`."""
    clone, _, commit = setup(tmp_path)
    worktree = tmp_path / "work" / "w"
    agent = Script(
        (write("src/a.test.sh", REPRO), {"summary": "test"}),
        (write("src/a.ts", "fixed-ish\n"), {"summary": "fix"}),
    )
    result = run_loop(
        repo="o/r",
        number=1,
        commit=commit,
        spec=SPEC,
        clone=clone,
        worktree=worktree,
        repro_file=None,
        rules=AGENT_RULES,
        agent=agent,
        sandbox=HostSandbox(),
    )
    assert result.status == "green"
    return result, worktree


def gate(result: LoopResult, worktree: Path, reader: Reviews, editor: Script) -> Gated:
    return review_gate(
        spec=SPEC,
        rules=AGENT_RULES,
        result=result,
        worktree=worktree,
        reader=reader,
        editor=editor,
        sandbox=HostSandbox(),
    )


def test_proven_blocker_goes_back_to_the_implementer(tmp_path: Path) -> None:
    result, worktree = half_fixed(tmp_path)
    reader = Reviews(
        {"verdict": "request_changes", "fixes_cause": False, "findings": [BLOCKER]},
        {},  # round 2: approved
    )
    editor = Script(
        # proof, attempt 1: a quote that isn't in the spec -> sent back
        (write("src/p.test.sh", EXACT), {"summary": "s", "spec_quote": "made up"}),
        # attempt 2: the same test, a real quote
        (None, {"summary": "s", "spec_quote": "must say exactly fixed"}),
        # the implementer against the proof (it also tries to weaken it)
        (
            both(write("src/a.ts", "fixed\n"), write("src/p.test.sh", "true\n")),
            {"summary": "exact now"},
        ),
    )
    got = gate(result, worktree, reader, editor)
    assert got.status == "approved"
    first = got.rounds[0]
    proof = first.proofs[0]
    assert proof.proven and proof.tests == ["src/p.test.sh"]
    assert proof.quote == "must say exactly fixed"
    assert [c.name for c in proof.attempts[0].checks if not c.ok] == ["quote"]
    assert first.attempts[-1].green and first.attempts[0].reverted == ["src/p.test.sh"]
    assert "<findings>\n- src/a.ts:1: not exact\n</findings>" in editor.prompts[2]
    # the PR's change: against the spec's commit, the proof ships with it
    final = got.result
    assert final.changed == ["src/a.test.sh", "src/a.ts", "src/p.test.sh"]
    assert "+fixed\n" in final.diff and "fixed-ish" not in final.diff
    assert final.repro == ["src/a.test.sh", "src/p.test.sh"]
    assert got.review is not None and got.review.approved
    assert round(got.cost_usd, 2) == 0.36  # 2 reviews 0.03, 3 editor runs 0.1


def test_a_finding_the_spec_does_not_back_does_not_block(tmp_path: Path) -> None:
    result, worktree = half_fixed(tmp_path)
    reader = Reviews({"verdict": "request_changes", "findings": [BLOCKER]})
    editor = Script((write("src/p.test.sh", EXACT), {"summary": "s", "spec_quote": ""}))
    got = gate(result, worktree, reader, editor)
    assert got.status == "unproven" and len(got.rounds) == 1
    assert not got.rounds[0].proofs[0].proven
    assert not (worktree / "src/p.test.sh").exists()  # reset
    assert got.result.diff == result.diff


def test_still_proven_after_the_second_review_is_blocked(tmp_path: Path) -> None:
    result, worktree = half_fixed(tmp_path)
    blocked = {"verdict": "request_changes", "findings": [BLOCKER]}
    reader = Reviews(blocked, blocked)
    quote = {"summary": "s", "spec_quote": "nothing more"}
    editor = Script(
        (write("src/p.test.sh", EXACT), quote),
        (write("src/a.ts", "fixed\n"), {"summary": "exact"}),
        # round 2: a second proof, red again
        (write("src/q.test.sh", 'grep -q never "$(dirname "$0")/a.ts"\n'), quote),
    )
    got = gate(result, worktree, reader, editor)
    assert got.status == "blocked" and len(got.rounds) == 2
    assert got.rounds[1].proofs[0].proven and got.rounds[1].attempts == []
    assert git(worktree, "log", "--format=%s", "-1") == "proof: src/a.ts"


def test_quote_must_be_in_the_spec() -> None:
    assert quoted("Must  say EXACTLY fixed.", SPEC)
    assert not quoted("fixed", SPEC)  # too short to tie to anything
    assert not quoted("must say roughly fixed", SPEC)


def test_body_shows_proven_and_unproven_blockers(tmp_path: Path) -> None:
    result, worktree = half_fixed(tmp_path)
    reader = Reviews(
        {"verdict": "request_changes", "fixes_cause": False, "findings": [BLOCKER]},
        {"verdict": "request_changes", "findings": [BLOCKER | {"text": "a hunch"}]},
    )
    quote = {"summary": "s", "spec_quote": "must say exactly fixed"}
    editor = Script(
        (write("src/p.test.sh", EXACT), quote),
        (write("src/a.ts", "fixed\n"), {"summary": "exact"}),
        (None, {"summary": "s", "spec_quote": ""}),  # round 2: not backed
    )
    got = gate(result, worktree, reader, editor)
    assert got.status == "unproven"
    body = render(
        spec=SPEC,
        result=got.result,
        risk=Risk(tier="low", files=[]),
        review=got.review,
        rounds=got.rounds,
    )
    assert "- **Unproven blockers** (no test could show them, read these):" in body
    assert "  - `src/a.ts:1` a hunch" in body
    assert "1 reviewer blocker(s) proven by a test, fixed" in body
    assert "**Round 1:** `src/a.ts:1` not exact" in body
    assert 'checking the spec\'s "must say exactly fixed"; fixed on attempt 1' in body
    assert "| round 1 proof 1 |" in body and "| round 1 fix 1 |" in body
    assert "| round 2 review |" in body


def test_gate_eval_outcomes() -> None:
    def case(kind: Kind, status: str, judge: bool, witness: bool | None) -> GateCase:
        g = Gated.model_construct(status=status, result=None, rounds=[], error="")
        ok = CheckResult(name="judge", ok=judge, seconds=0, output="")
        w = None
        if witness is not None:
            w = CheckResult(name="witness", ok=witness, seconds=0, output="")
        return GateCase.model_construct(
            key="k", kind=kind, gated=g, judge=ok, witness=w, proofs_valid=None
        )

    assert case("right", "approved", True, None).outcome == "published"
    assert case("right", "blocked", True, None).outcome == "false_block"
    assert case("wrong", "blocked", False, True).outcome == "blocked"
    assert case("wrong", "unproven", True, True).outcome == "fixed"
    assert case("wrong", "approved", True, False).outcome == "leaked"  # witness red
