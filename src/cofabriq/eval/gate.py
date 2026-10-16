"""Does the review gate keep wrong fixes away from the dev, without blocking
right ones?

The same cases as the reviewer eval (right: judge green; wrong: judge red
or a confirmed witness), each replayed in a fresh worktree with an install,
then the whole gate (`loop/gate.py`). The answer key judges the tree the
gate leaves:

    right   published (approved or unproven) / blocked (a false block)
    wrong   blocked: no PR
            fixed: published, and the planted repro and the witness test
                   are green on the final tree
            leaked: published and still wrong: what the gate is for

Proofs are checked as the reproducer's test is: on the planted code with
the reference fix they must pass (valid), else the proof encoded a wrong
expectation (the risk the gate's docstring names).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from cofabriq.eval.loop import Judged, judge, validate
from cofabriq.eval.review import Kind, replay
from cofabriq.eval.witness import Witness
from cofabriq.loop.engine import Agent
from cofabriq.loop.gate import Gated, review_gate
from cofabriq.loop.refine import Spec
from cofabriq.loop.run import CheckResult, Rules, git, repro_command
from cofabriq.loop.sandbox import Sandbox

type Outcome = Literal["published", "false_block", "blocked", "fixed", "leaked"]


class GateCase(BaseModel):
    key: str
    kind: Kind
    gated: Gated | None  # None: setup failed
    judge: CheckResult | None = None  # the planted repro on the final tree
    witness: CheckResult | None = None  # the witness test on the final tree
    proofs_valid: CheckResult | None = None  # proofs with the reference fix
    error: str = ""

    @property
    def outcome(self) -> Outcome | None:
        if self.gated is None:
            return None
        blocked = self.gated.status == "blocked"
        if self.kind == "right":
            return "false_block" if blocked else "published"
        if blocked:
            return "blocked"
        right = [c for c in (self.judge, self.witness) if c is not None]
        return "fixed" if right and all(c.ok for c in right) else "leaked"

    @property
    def proven(self) -> int:
        if self.gated is None:
            return 0
        return sum(p.proven for r in self.gated.rounds for p in r.proofs)


def _rerun(
    worktree: Path, rules: Rules, files: dict[str, str], sandbox: Sandbox
) -> CheckResult:
    """The witness test, as written, on the tree the gate left."""
    for p, text in files.items():
        (worktree / p).parent.mkdir(parents=True, exist_ok=True)
        (worktree / p).write_text(text, "utf-8")
    ran = sandbox(repro_command(rules, list(files)), worktree, network=False)
    return CheckResult(
        name="witness", ok=ran.ok, seconds=ran.seconds, output=ran.output
    )


def gate_case(
    *,
    key: str,
    kind: Kind,
    judged: Judged,
    spec: Spec,
    rules: Rules,
    clone: Path,
    worktree: Path,
    reader: Agent,
    editor: Agent,
    sandbox: Sandbox,
    planted: Path,
    patch: Path,
    witness: Witness | None,
) -> GateCase:
    """One case: replay, install, the gate, then the answer key. Leaves the
    worktree (the caller removes it)."""
    result = judged.result
    replay(clone, worktree, result)
    installed = sandbox(rules.install, worktree, network=True)
    if not installed.ok:
        return GateCase(key=key, kind=kind, gated=None, error=installed.output[-300:])
    gated = review_gate(
        spec=spec,
        rules=rules,
        result=result,
        worktree=worktree,
        reader=reader,
        editor=editor,
        sandbox=sandbox,
    )
    case = GateCase(key=key, kind=kind, gated=gated)
    case.judge = judge(worktree, rules, planted, sandbox)
    if kind == "wrong" and witness is not None and witness.confirmed:
        case.witness = _rerun(worktree, rules, witness.files, sandbox)
    proofs = [t for r in gated.rounds for p in r.proofs if p.proven for t in p.tests]
    if proofs:  # last: it resets the code
        tests = list(dict.fromkeys(proofs))
        # proofs are committed layers: undo the witness rerun's writes first
        git(worktree, "checkout", "-q", "HEAD", "--", *tests)
        case.proofs_valid = validate(
            worktree, rules, tests, patch, sandbox, result.commit
        )
    return case


@dataclass
class Summary:
    right: int = 0
    published: float = 0.0  # right ones that reach the dev
    wrong: int = 0
    blocked: float = 0.0
    fixed: float = 0.0
    leaked: float = 0.0  # wrong ones that reach the dev still wrong
    proofs: int = 0
    proofs_valid: float = 0.0  # cases whose proofs pass with the reference fix
    cost_usd: float = 0.0  # mean gate cost per case


def summarize(cases: list[GateCase]) -> Summary:
    right = [c for c in cases if c.kind == "right" and c.gated is not None]
    wrong = [c for c in cases if c.kind == "wrong" and c.gated is not None]
    checked = [c.proofs_valid.ok for c in cases if c.proofs_valid is not None]
    gated = [c.gated for c in cases if c.gated is not None]

    def share(group: list[GateCase], outcome: Outcome) -> float:
        return sum(c.outcome == outcome for c in group) / len(group) if group else 0.0

    return Summary(
        right=len(right),
        published=share(right, "published"),
        wrong=len(wrong),
        blocked=share(wrong, "blocked"),
        fixed=share(wrong, "fixed"),
        leaked=share(wrong, "leaked"),
        proofs=sum(c.proven for c in cases),
        proofs_valid=sum(checked) / len(checked) if checked else 0.0,
        cost_usd=sum(g.cost_usd for g in gated) / len(gated) if gated else 0.0,
    )
