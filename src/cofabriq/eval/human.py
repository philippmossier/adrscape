"""Human minutes per merged PR: a dev reviews real draft PRs, timed.

The set: one draft PR per planted logic bug, `WRONG` of them the reviewer
eval's proven-wrong fix instead of the right one (picked at random, one PR
per issue, so two PRs for the same issue don't give the wrong one away).
The dev doesn't know which; the order is shuffled.

Per PR the dev opens it, reviews it as usual on GitHub, and records a
decision: merge, changes or close. Nothing is merged for real (a merge
would fix the planted bug and break the eval). Out of it:

    minutes per PR       median review time
    minutes per merge    all review time / PRs the dev would merge
    caught               wrong PRs not merged
    agreed               decisions that match the reviewer's verdict
"""

import random
from dataclasses import dataclass
from statistics import median
from typing import Literal

from pydantic import BaseModel

from cofabriq.eval.review import Kind

type Decision = Literal["merge", "changes", "close"]

WRONG = 3  # wrong PRs in a set


class Pick(BaseModel):
    key: str
    kind: Kind


def pick(
    right: list[str], wrong: list[str], seed: int, n_wrong: int = WRONG
) -> list[Pick]:
    """One PR per bug with a right fix; `n_wrong` of those with a proven
    wrong fix swapped for it. Shuffled."""
    rng = random.Random(seed)
    swap = set(rng.sample(sorted(set(right) & set(wrong)), n_wrong))
    picks = [Pick(key=k, kind="wrong" if k in swap else "right") for k in sorted(right)]
    rng.shuffle(picks)
    return picks


class Entry(BaseModel):
    """One published PR of the set (the manifest is the answer key: the dev
    doesn't open it before reviewing)."""

    key: str
    kind: Kind
    number: int  # the issue
    pr: int
    url: str
    branch: str
    approved: bool  # the reviewer's verdict in the body


class HumanSet(BaseModel):
    """`data/human/set.json`: what to publish and what was published."""

    picks: list[Pick]
    entries: list[Entry] = []


class Record(BaseModel):
    url: str
    seconds: float
    decision: Decision
    note: str = ""


@dataclass
class Summary:
    n: int = 0
    minutes: float = 0.0  # median per PR
    per_merge: float = 0.0  # all minutes / merged
    merged: int = 0
    wrong: int = 0
    caught: int = 0  # wrong PRs not merged
    right_merged: int = 0
    agreed: float = 0.0  # merge == the reviewer approved


def summarize(entries: list[Entry], records: list[Record]) -> Summary:
    by_url = {e.url: e for e in entries}
    done = [(by_url[r.url], r) for r in records if r.url in by_url]
    if not done:
        return Summary()
    merged = [r for _, r in done if r.decision == "merge"]
    total = sum(r.seconds for _, r in done) / 60
    wrong = [r for e, r in done if e.kind == "wrong"]
    return Summary(
        n=len(done),
        minutes=median(r.seconds for _, r in done) / 60,
        per_merge=total / len(merged) if merged else 0.0,
        merged=len(merged),
        wrong=len(wrong),
        caught=sum(r.decision != "merge" for r in wrong),
        right_merged=sum(e.kind == "right" and r.decision == "merge" for e, r in done),
        agreed=sum((r.decision == "merge") == e.approved for e, r in done) / len(done),
    )
