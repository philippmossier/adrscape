"""Impact eval: from an issue's text alone, which components will its fix touch?

Ground truth from the target's own history: the components touched by the
commits whose message says they fix the issue ("fixes #N"), for issues
GitHub also shows as closed by a pull request or a commit. The map is cut
before the dev split starts, so its co-change counts never contain a fix
being predicted; past fixes are used only if committed before the query
issue was filed.
"""

import random
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel

from cofabriq.issues.model import Issue
from cofabriq.map.build import MAX_FILES
from cofabriq.map.components import ComponentRules, component_of
from cofabriq.map.fixes import Fix
from cofabriq.map.impact import Ranked, vote

NEIGHBOURS = 30  # earlier issues looked at by kNN


class Rules(BaseModel):
    """`[impact]` in `targets/<owner>-<name>.toml`, fixed before scoring."""

    map_until: date  # the map's cut; before the dev window
    window: tuple[date, date]  # dev: methods compared here
    test_window: tuple[date, date]  # held out: scored once
    sample: int


def load_rules(targets_dir: Path, repo: str) -> Rules | None:
    path = targets_dir / f"{repo.replace('/', '-')}.toml"
    if not path.is_file():
        return None
    table = tomllib.loads(path.read_text("utf-8")).get("impact")
    return None if table is None else Rules.model_validate(table)


class Case(BaseModel):
    issue: int
    components: list[str]  # touched by its fix


class ImpactSet(BaseModel):
    repo: str
    frozen_at: datetime
    rules: Rules
    window: tuple[date, date]  # the split this set covers
    cases: list[Case]


def touched(fix: Sequence[Fix], rules: ComponentRules) -> list[str]:
    return sorted({component_of(f, rules) for c in fix for f in c.files})


def build(
    issues: Sequence[Issue],
    fixes: Mapping[int, list[Fix]],
    components: ComponentRules,
    rules: Rules,
    window: tuple[date, date],
    frozen_at: datetime,
    seed: int = 0,
) -> tuple[ImpactSet, int]:
    """Cases from issues filed in `window`, closed as completed by a PR or a
    commit, with fix commits touching 1 to MAX_FILES files. Returns the set
    and the pool size it was sampled from."""
    start, end = window
    pool = sorted(
        i.number
        for i in issues
        if start <= i.created_at.date() <= end
        and i.state_reason == "completed"
        and i.closed_by is not None
        and i.number in fixes
        and 0 < len({f for c in fixes[i.number] for f in c.files}) <= MAX_FILES
    )
    chosen = sorted(random.Random(seed).sample(pool, min(rules.sample, len(pool))))
    cases = [Case(issue=n, components=touched(fixes[n], components)) for n in chosen]
    impact = ImpactSet(
        repo=issues[0].repo if issues else "",
        frozen_at=frozen_at,
        rules=rules,
        window=window,
        cases=cases,
    )
    return impact, len(pool)


def knn(
    neighbours: Sequence[tuple[int, float]],
    filed: datetime,
    fixes: Mapping[int, list[Fix]],
    components: ComponentRules,
) -> Ranked:
    """`impact.vote` with the fixes known when the issue was filed: commits
    after `filed` were unknown then."""
    ranked, _ = vote(
        neighbours,
        lambda n: touched([c for c in fixes.get(n, []) if c.at < filed], components),
    )
    return ranked


def fill(ranked: Ranked, popular: Sequence[str]) -> list[str]:
    """The ranking, then the most changed components it lacks: every method
    names k components at every k."""
    names = [c for c, _ in ranked]
    return names + [c for c in popular if c not in set(names)]


@dataclass(frozen=True)
class Score:
    hit_at: dict[int, float]  # share of issues with any touched component in top k
    recall_at: dict[int, float]  # mean share of touched components in top k


def score(
    cases: Sequence[Case], predicted: Mapping[int, list[str]], ks: Sequence[int]
) -> Score:
    hit = {k: 0.0 for k in ks}
    recall = {k: 0.0 for k in ks}
    for case in cases:
        truth = set(case.components)
        for k in ks:
            found = truth & set(predicted[case.issue][:k])
            hit[k] += bool(found)
            recall[k] += len(found) / len(truth)
    n = len(cases) or 1
    return Score(
        {k: v / n for k, v in hit.items()}, {k: v / n for k, v in recall.items()}
    )
