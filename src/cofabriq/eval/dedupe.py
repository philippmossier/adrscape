"""Duplicate detection eval: a frozen set of labelled issues and its metrics."""

import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from cofabriq.issues.model import Issue

type Signal = Literal["mark", "comment"]


class Pair(BaseModel):
    query: int  # the issue closed as duplicate
    target: int  # the earlier issue it duplicates
    signal: Signal
    # Other issues labelled as the same problem (the target's cluster): a hit
    # on one of them counts for cluster recall, not for strict recall.
    siblings: list[int] = []


class EvalSet(BaseModel):
    """Issue numbers only, no text: safe to commit, reproducible against any
    later fetch of the same repo (candidates are cut by `created_at`).
    """

    repo: str
    frozen_at: datetime
    issues: int  # issues in the snapshot it was built from
    pairs: list[Pair]
    negatives: list[int]


@dataclass(frozen=True)
class BuildReport:
    pairs: int
    by_signal: dict[str, int]
    ambiguous: int  # more than one target named
    unusable: int  # target missing from the snapshot or not earlier


def comment_pattern(repo: str) -> re.Pattern[str]:
    """`duplicate of #12`, `Duplicate of https://github.com/<repo>/issues/12`."""
    url = re.escape(f"https://github.com/{repo}/issues/")
    return re.compile(rf"\bduplicate\s+of\s+(?:#|{url})(\d+)\b", re.IGNORECASE)


def build(
    issues: Sequence[Issue], frozen_at: datetime, seed: int = 0
) -> tuple[EvalSet, BuildReport]:
    """Label pairs from GitHub's duplicate marks and human comments.

    Bot comments are ignored: some repos run a bot that lists likely
    duplicates on every issue, which would turn the eval into "can you read
    the bot". An issue naming more than one target is dropped as ambiguous.
    Negatives: as many issues with no duplicate signal at all, sampled with
    a fixed seed. Some are unlabelled duplicates; that noise costs every
    method the same.
    """
    [repo] = {i.repo for i in issues}
    by_number = {i.number: i for i in issues}
    pattern = comment_pattern(repo)

    targets: dict[int, dict[int, Signal]] = {}  # query -> target -> signal
    for issue in issues:
        for ref in issue.duplicate_of:
            if ref.repo == repo:
                targets.setdefault(issue.number, {})[ref.number] = "mark"
        for ref in issue.duplicates:
            if ref.repo == repo:
                targets.setdefault(ref.number, {})[issue.number] = "mark"
        for comment in issue.comments:
            if comment.bot:
                continue
            for match in pattern.finditer(comment.body):
                named = targets.setdefault(issue.number, {})
                named.setdefault(int(match.group(1)), "comment")

    # one target per query; ambiguous ones join no cluster either
    clear = {q: next(iter(n.items())) for q, n in targets.items() if len(n) == 1}
    clusters = _clusters([(q, t) for q, (t, _) in clear.items()])

    pairs: list[Pair] = []
    unusable = 0
    for query, (target, signal) in sorted(clear.items()):
        q, t = by_number.get(query), by_number.get(target)
        if q is None or t is None or t.created_at >= q.created_at:
            unusable += 1
            continue
        siblings = sorted(clusters[target] - {query, target})
        pairs.append(Pair(query=query, target=target, signal=signal, siblings=siblings))
    ambiguous = len(targets) - len(clear)

    labelled = targets.keys() | {t for named in targets.values() for t in named}
    oldest = min(i.created_at for i in issues)
    pool = sorted(
        i.number
        for i in issues
        if i.number not in labelled
        and i.state_reason != "duplicate"
        and i.created_at > oldest
    )
    negatives = sorted(random.Random(seed).sample(pool, min(len(pairs), len(pool))))

    by_signal = {s: sum(p.signal == s for p in pairs) for s in ("mark", "comment")}
    report = BuildReport(len(pairs), by_signal, ambiguous, unusable)
    evalset = EvalSet(
        repo=repo,
        frozen_at=frozen_at,
        issues=len(issues),
        pairs=pairs,
        negatives=negatives,
    )
    return evalset, report


def _clusters(edges: Sequence[tuple[int, int]]) -> dict[int, set[int]]:
    """Connected components: #3 dup of #1 and #2 dup of #3 put all three in
    one cluster. Maps every issue on an edge to its (shared) component.
    """
    cluster: dict[int, set[int]] = {}
    for a, b in edges:
        merged = cluster.get(a, {a}) | cluster.get(b, {b})
        for n in merged:
            cluster[n] = merged
    return cluster


@dataclass(frozen=True)
class Ranked:
    """What retrieval returned for one eval issue, best first."""

    query: int
    target: int | None  # None for a negative
    candidates: list[tuple[int, float]]  # (number, score)
    siblings: frozenset[int] = frozenset()

    def is_hit(self, number: int, cluster: bool) -> bool:
        return number == self.target or (cluster and number in self.siblings)


@dataclass(frozen=True)
class Threshold:
    value: float
    precision: float
    recall: float
    f1: float


@dataclass(frozen=True)
class Metrics:
    positives: int
    negatives: int
    recall_at: dict[int, float]  # k -> share of positives with target in top k
    best: Threshold  # embedding-threshold baseline at its best F1


type Rank = Callable[[int], list[tuple[int, float]]]


def rank_all(evalset: EvalSet, rank: Rank) -> list[Ranked]:
    out = [
        Ranked(p.query, p.target, rank(p.query), frozenset(p.siblings))
        for p in evalset.pairs
    ]
    out += [Ranked(n, None, rank(n)) for n in evalset.negatives]
    return out


def score(
    ranked: Sequence[Ranked], ks: Sequence[int] = (1, 5, 10), cluster: bool = False
) -> Metrics:
    """Recall@k over positives, and the threshold baseline over everything.

    Strict (default): only the labelled target counts. `cluster=True` also
    accepts the target's siblings: another duplicate of the same bug is a
    useful answer for whoever triages, but the label names only one issue.

    Baseline: "duplicate of the top-1 candidate if its score >= t". A
    prediction is right only on a positive whose top-1 is the target. The
    threshold is swept over every observed top-1 score and the best F1 is
    reported: tuned on the eval set itself, so an upper bound for the
    baseline, which keeps any claim that something beats it conservative.
    """
    positives = [r for r in ranked if r.target is not None]
    recall_at = {
        k: _share(positives, lambda r, k=k: _in_top(r, k, cluster)) for k in ks
    }
    tops = [
        (r.candidates[0][1], r.is_hit(r.candidates[0][0], cluster))
        for r in ranked
        if r.candidates
    ]
    best = Threshold(1.0, 0.0, 0.0, 0.0)
    for t in sorted({s for s, _ in tops}):
        predicted = [hit for s, hit in tops if s >= t]
        tp = sum(predicted)
        precision = tp / len(predicted)
        recall = tp / len(positives) if positives else 0.0
        f1 = 2 * precision * recall / (precision + recall) if tp else 0.0
        if f1 > best.f1:
            best = Threshold(t, precision, recall, f1)
    return Metrics(len(positives), len(ranked) - len(positives), recall_at, best)


def _in_top(r: Ranked, k: int, cluster: bool) -> bool:
    return any(r.is_hit(n, cluster) for n, _ in r.candidates[:k])


def _share(rows: Sequence[Ranked], hit: Callable[[Ranked], bool]) -> float:
    return sum(hit(r) for r in rows) / len(rows) if rows else 0.0
