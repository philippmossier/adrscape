"""The core loop against the planted bugs: how many go from red to green?

Each bug runs `run_loop` on the spec refine already wrote for it (step 5's
eval, `data/refine/`), so this measures the fix, not the spec again, and
costs only the implementer's runs. Resolved = repro and every check green
within MAX_ATTEMPTS. Also kept: whether the fix touched the reference fix's
files (a green fix in another place passes the repro but may be a
workaround), cost, attempts and time per node. Rates per bug size, as for
refine.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from statistics import median

from pydantic import TypeAdapter

from cofabriq.eval.bugs import PlantedBug
from cofabriq.loop.refine import Refined
from cofabriq.loop.run import LoopResult

_refined_adapter = TypeAdapter(dict[str, Refined])


def load_specs(refine_dir: Path) -> dict[str, Refined]:
    """Bug key -> its newest stored refine result, over all `eval-*.json`
    (a run resumed with `--only` writes a second file)."""
    specs: dict[str, Refined] = {}
    for path in sorted(refine_dir.glob("eval-*.json")):  # names sort by time
        specs |= _refined_adapter.validate_python(json.loads(path.read_text("utf-8")))
    return specs


@dataclass(frozen=True)
class BugResult:
    key: str
    size: str
    status: str
    attempts: int
    on_target: bool  # the diff touches a file the reference fix changes
    cost_usd: float
    setup_s: float
    agent_s: float  # all attempts
    checks_s: float  # all attempts


def score_bug(bug: PlantedBug, result: LoopResult) -> BugResult:
    runs = [a.run for a in result.attempts if a.run is not None]
    return BugResult(
        key=bug.key,
        size=bug.size,
        status=result.status,
        attempts=len(result.attempts),
        on_target=bool(set(bug.files) & set(result.changed)),
        cost_usd=result.cost_usd,
        setup_s=result.setup_s,
        agent_s=sum(r.latency_ms for r in runs) / 1000,
        checks_s=sum(c.seconds for a in result.attempts for c in a.checks),
    )


@dataclass
class Summary:
    n: int = 0
    resolved: float = 0.0  # share green
    first_try: float = 0.0  # share green on attempt 1
    on_target: float = 0.0  # share of green fixes in the reference's files
    cost_usd: float = 0.0  # mean per bug
    attempts: float = 0.0  # median
    seconds: float = 0.0  # median per bug, setup + agent + checks


def summarize(scores: Iterable[BugResult]) -> Summary:
    items = list(scores)
    n = len(items)
    if n == 0:
        return Summary()
    green = [s for s in items if s.status == "green"]
    return Summary(
        n=n,
        resolved=len(green) / n,
        first_try=sum(s.attempts == 1 for s in green) / n,
        on_target=sum(s.on_target for s in green) / len(green) if green else 0.0,
        cost_usd=sum(s.cost_usd for s in items) / n,
        attempts=median(s.attempts for s in items),
        seconds=median(s.setup_s + s.agent_s + s.checks_s for s in items),
    )
