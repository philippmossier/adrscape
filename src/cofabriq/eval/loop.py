"""The core loop against the planted bugs: how many go from red to green?

Each bug runs `run_loop` on the spec refine already wrote for it (step 5's
eval, `data/refine/`), so this measures reproduce and fix, not the spec
again, and costs only those agents' runs.

With the agent's own repro (the default) the planted repro is the hidden
judge: resolved = the loop went green on the agent's test and the planted
repro passes on the final tree. The agent's test is also checked against
the reference fix (valid = it was red on the planted code and is green with
the reference patch: it tests this bug, not something else). With
`planted` the planted repro is handed to the agent and green is resolved
(the step 6 baseline).

Also kept: whether the fix touched the reference fix's files (a green fix
in another place may be a workaround), cost, attempts and time per node.
Rates per bug size, as for refine.
"""

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from pydantic import BaseModel, TypeAdapter

from cofabriq.eval.bugs import PlantedBug
from cofabriq.loop.refine import Refined, git_env
from cofabriq.loop.run import CheckResult, LoopResult, Rules, repro_command, reset
from cofabriq.loop.sandbox import Sandbox

_refined_adapter = TypeAdapter(dict[str, Refined])


def load_specs(refine_dir: Path) -> dict[str, Refined]:
    """Bug key -> its newest stored refine result, over all `eval-*.json`
    (a run resumed with `--only` writes a second file)."""
    specs: dict[str, Refined] = {}
    for path in sorted(refine_dir.glob("eval-*.json")):  # names sort by time
        specs |= _refined_adapter.validate_python(json.loads(path.read_text("utf-8")))
    return specs


class Judged(BaseModel):
    """One loop run plus what only the answer key can say about it."""

    result: LoopResult
    judge: CheckResult | None = None  # the planted repro on the final tree
    valid: CheckResult | None = None  # the agent's repro with the reference fix


def judge(worktree: Path, rules: Rules, planted: Path, sandbox: Sandbox) -> CheckResult:
    """The hidden planted repro on the tree the loop left: does the fix fix
    the bug the issue is about, not only the agent's test?"""
    dest = f"{rules.repro_dir}/{planted.name}"
    (worktree / dest).parent.mkdir(parents=True, exist_ok=True)
    (worktree / dest).write_bytes(planted.read_bytes())
    ran = sandbox(repro_command(rules, [dest]), worktree, network=False)
    (worktree / dest).unlink()
    return CheckResult(name="judge", ok=ran.ok, seconds=ran.seconds, output=ran.output)


def validate(
    worktree: Path, rules: Rules, repro: list[str], patch: Path, sandbox: Sandbox
) -> CheckResult:
    """The agent's repro on the planted code with the reference fix applied:
    green = it tests this bug (it was red without the fix). Resets the
    worktree's code, so it runs last."""
    # 1. keep the agent's test, drop everything else it or the fix changed
    tests = {p: (worktree / p).read_bytes() for p in repro}
    reset(worktree)
    for p, data in tests.items():
        (worktree / p).parent.mkdir(parents=True, exist_ok=True)
        (worktree / p).write_bytes(data)
    # 2. the reference fix on top, then the agent's test
    applied = subprocess.run(
        ["git", "-C", str(worktree), "apply", str(patch.resolve())],
        capture_output=True,
        text=True,
        check=False,
        env=git_env(),
    )
    if applied.returncode != 0:
        return CheckResult(name="valid", ok=False, seconds=0, output=applied.stderr)
    ran = sandbox(repro_command(rules, repro), worktree, network=False)
    return CheckResult(name="valid", ok=ran.ok, seconds=ran.seconds, output=ran.output)


def against_key(
    result: LoopResult,
    worktree: Path,
    rules: Rules,
    planted: Path,
    patch: Path,
    sandbox: Sandbox,
) -> Judged:
    """Judge and validity for a run on the agent's own repro (nothing to
    add when the planted repro was handed in). Needs the worktree the loop
    left; resets its code."""
    if result.planted:
        return Judged(result=result)
    verdict, valid = None, None
    if result.status == "green":
        verdict = judge(worktree, rules, planted, sandbox)
    if result.repro:  # last: it resets the code
        valid = validate(worktree, rules, result.repro, patch, sandbox)
    return Judged(result=result, judge=verdict, valid=valid)


@dataclass(frozen=True)
class BugResult:
    key: str
    size: str
    status: str
    planted: bool  # the planted repro was handed to the agent
    reproduced: bool  # a red repro exists (always, when planted)
    valid: bool | None  # agent's repro green with the reference fix
    resolved: bool  # green, and the hidden judge green (agent's repro)
    attempts: int  # the implementer's
    on_target: bool  # the diff touches a file the reference fix changes
    cost_usd: float
    setup_s: float
    agent_s: float  # all attempts, both agents
    checks_s: float  # all attempts, both agents


def score_bug(bug: PlantedBug, judged: Judged) -> BugResult:
    result = judged.result
    attempts = [*result.reproduce, *result.attempts]
    runs = [a.run for a in attempts if a.run is not None]
    green = result.status == "green"
    return BugResult(
        key=bug.key,
        size=bug.size,
        status=result.status,
        planted=result.planted,
        reproduced=result.red is not None and not result.red.ok,
        valid=None if judged.valid is None else judged.valid.ok,
        resolved=green and (judged.judge is None or judged.judge.ok),
        attempts=len(result.attempts),
        on_target=bool(set(bug.files) & set(result.changed)),
        cost_usd=result.cost_usd,
        setup_s=result.setup_s,
        agent_s=sum(r.latency_ms for r in runs) / 1000,
        checks_s=sum(c.seconds for a in attempts for c in a.checks),
    )


@dataclass
class Summary:
    n: int = 0
    reproduced: float = 0.0  # share with a red repro
    valid: float = 0.0  # share of agent repros green with the reference fix
    green: float = 0.0  # share green on the loop's own repro
    resolved: float = 0.0  # share green and judged green
    first_try: float = 0.0  # share resolved on attempt 1
    on_target: float = 0.0  # share of resolved fixes in the reference's files
    cost_usd: float = 0.0  # mean per bug
    attempts: float = 0.0  # median
    seconds: float = 0.0  # median per bug, setup + agent + checks


def summarize(scores: Iterable[BugResult]) -> Summary:
    items = list(scores)
    n = len(items)
    if n == 0:
        return Summary()
    resolved = [s for s in items if s.resolved]
    checked = [s.valid for s in items if s.valid is not None]
    return Summary(
        n=n,
        reproduced=sum(s.reproduced for s in items) / n,
        valid=sum(checked) / len(checked) if checked else 0.0,
        green=sum(s.status == "green" for s in items) / n,
        resolved=len(resolved) / n,
        first_try=sum(s.attempts == 1 for s in resolved) / n,
        on_target=sum(s.on_target for s in resolved) / len(resolved)
        if resolved
        else 0.0,
        cost_usd=sum(s.cost_usd for s in items) / n,
        attempts=median(s.attempts for s in items),
        seconds=median(s.setup_s + s.agent_s + s.checks_s for s in items),
    )
