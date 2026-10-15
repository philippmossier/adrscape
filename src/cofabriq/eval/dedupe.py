"""Duplicate detection eval: a frozen set of labelled issues and its metrics."""

import random
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from cofabriq.issues.model import Issue

type Signal = Literal["mark", "comment"]
type Negatives = Literal["unlabelled", "closed_by_change"]


class Rules(BaseModel):
    """How one repo's maintainers mark duplicates, written down before any
    scoring (`[dedupe]` in `targets/<owner>-<name>.toml`). The defaults are
    the open-webui set's rules.

    `window`: queries (pairs and negatives) created in it, both days included;
    targets may be older. `duplicate_label`: a comment names a target only on
    an issue carrying this label (or closed / marked as duplicate by GitHub):
    the label is the maintainer's decision, the comment says which issue.
    `negatives`: `unlabelled` samples issues without any duplicate signal;
    `closed_by_change` takes only issues closed as completed by a merged pull
    request or a commit, which a duplicate almost never is. `sample`: pairs
    kept (fixed seed), negatives match the pair count.
    """

    window: tuple[date, date] | None = None
    duplicate_label: str | None = None
    negatives: Negatives = "unlabelled"
    sample: int | None = None


def load_rules(targets_dir: Path, repo: str) -> Rules:
    path = targets_dir / f"{repo.replace('/', '-')}.toml"
    if not path.is_file():
        return Rules()
    table = tomllib.loads(path.read_text("utf-8")).get("dedupe", {})
    return Rules.model_validate(table)


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
    rules: Rules = Rules()
    pairs: list[Pair]
    negatives: list[int]


@dataclass(frozen=True)
class BuildReport:
    pairs: int
    by_signal: dict[str, int]
    ambiguous: int  # more than one target named
    unusable: int  # target missing from the snapshot or not earlier
    found: int  # usable pairs in the window, before sampling
    negative_pool: int


# A comment that says "duplicate": vscode's `/duplicate <ref>` command,
# "duplicate of #12", a line starting with "Duplicate <url>", "closing as
# duplicate of [#12](<url>)".
DUPLICATE = re.compile(
    r"(?:^|\s)/duplicate\b|\bduplicate\s+of\b|^\s*duplicate\b"
    r"|\bclos(?:e|ed|ing)\s+as\s+(?:a\s+)?duplicate\b",
    re.IGNORECASE | re.MULTILINE,
)


def named_targets(body: str, repo: str) -> set[int]:
    """In-repo issues a duplicate comment names: `#12` or the issue URL.

    The ref right after the phrase wins ("duplicate of #12. Fixed by PR #13"
    names #12). Without one ("same root cause as #12 /duplicate"), every ref
    in the comment counts, so two of them make the query ambiguous. Empty if
    the comment doesn't say duplicate. `other/repo#12` and other repos' URLs
    are skipped.
    """
    if not DUPLICATE.search(body):
        return set()
    url = re.escape(f"https://github.com/{repo}/issues/")
    ref = rf"(?:#(\d+)\b|{url}(\d+)\b)"
    flags = re.IGNORECASE | re.MULTILINE
    after = re.findall(rf"(?:{DUPLICATE.pattern})[\s:]*\[?{ref}", body, flags)
    anywhere = re.findall(rf"(?<![\w/-]){ref}", body, flags)
    return {int(a or b) for a, b in after or anywhere}


def build(
    issues: Sequence[Issue],
    frozen_at: datetime,
    rules: Rules | None = None,
    seed: int = 0,
) -> tuple[EvalSet, BuildReport]:
    """Label pairs from GitHub's duplicate marks and human comments.

    Bot comments are ignored: some repos run a bot that lists likely
    duplicates on every issue, which would turn the eval into "can you read
    the bot". So are comments with "🤖" (maintainer accounts posting LLM
    output). An issue naming more than one target is dropped as ambiguous.
    Negatives follow `rules.negatives`; with `unlabelled`, some are
    unlabelled duplicates, a noise that costs every method the same.
    """
    rules = rules or Rules()
    [repo] = {i.repo for i in issues}
    by_number = {i.number: i for i in issues}

    targets: dict[int, dict[int, Signal]] = {}  # query -> target -> signal
    for issue in issues:
        for ref in issue.duplicate_of:
            if ref.repo == repo:
                targets.setdefault(issue.number, {})[ref.number] = "mark"
        for ref in issue.duplicates:
            if ref.repo == repo:
                targets.setdefault(ref.number, {})[issue.number] = "mark"
        if not _labelled_duplicate(issue, rules):
            continue
        for comment in issue.comments:
            if comment.bot or "🤖" in comment.body:
                continue
            for number in named_targets(comment.body, repo) - {issue.number}:
                targets.setdefault(issue.number, {}).setdefault(number, "comment")

    # one target per query; ambiguous ones join no cluster either
    clear = {q: next(iter(n.items())) for q, n in targets.items() if len(n) == 1}
    clusters = _clusters([(q, t) for q, (t, _) in clear.items()])

    pairs: list[Pair] = []
    unusable = ambiguous = 0
    for query, (target, signal) in sorted(clear.items()):
        q, t = by_number.get(query), by_number.get(target)
        if q is not None and not _in_window(q, rules):
            continue
        if q is None or t is None or t.created_at >= q.created_at:
            unusable += 1
            continue
        siblings = sorted(clusters[target] - {query, target})
        pairs.append(Pair(query=query, target=target, signal=signal, siblings=siblings))
    for query in targets.keys() - clear.keys():
        q = by_number.get(query)
        ambiguous += q is None or _in_window(q, rules)
    found = len(pairs)
    if rules.sample is not None and found > rules.sample:
        kept = random.Random(seed).sample(pairs, rules.sample)
        pairs = sorted(kept, key=lambda p: p.query)

    labelled = targets.keys() | {t for named in targets.values() for t in named}
    oldest = min(i.created_at for i in issues)
    pool = sorted(
        i.number
        for i in issues
        if i.number not in labelled
        and i.state_reason != "duplicate"
        and i.created_at > oldest
        and _in_window(i, rules)
        and not (rules.duplicate_label and rules.duplicate_label in i.labels)
        and (
            rules.negatives == "unlabelled"
            or (i.state_reason == "completed" and i.closed_by is not None)
        )
    )
    negatives = sorted(random.Random(seed).sample(pool, min(len(pairs), len(pool))))

    by_signal = {s: sum(p.signal == s for p in pairs) for s in ("mark", "comment")}
    report = BuildReport(len(pairs), by_signal, ambiguous, unusable, found, len(pool))
    evalset = EvalSet(
        repo=repo,
        frozen_at=frozen_at,
        issues=len(issues),
        rules=rules,
        pairs=pairs,
        negatives=negatives,
    )
    return evalset, report


def _labelled_duplicate(issue: Issue, rules: Rules) -> bool:
    if rules.duplicate_label is None:
        return True
    return rules.duplicate_label in issue.labels or issue.state_reason == "duplicate"


def _in_window(issue: Issue, rules: Rules) -> bool:
    if rules.window is None:
        return True
    start, end = rules.window
    return start <= issue.created_at.date() <= end


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


@dataclass(frozen=True)
class VerdictScore:
    predicted: int  # `duplicate_of` verdicts, over positives and negatives
    unsure: int  # abstentions with a target
    unsure_right: int  # ... whose target would have been a hit
    precision: float
    recall: float
    f1: float


def score_verdicts(
    ranked: Sequence[Ranked],
    verdicts: Mapping[int, tuple[str, int | None]],
    cluster: bool = False,
) -> VerdictScore:
    """Precision and recall of `duplicate_of`, comparable to the threshold
    baseline: a prediction is right only on a positive whose named target is
    the label (or, with `cluster=True`, one of its siblings). `unsure`,
    `related` and `none` count as no prediction; `unsure` is counted apart,
    with the share that would have been right (the recall it gives up).
    `verdicts` maps query number to (verdict, target).
    """
    positives = sum(r.target is not None for r in ranked)
    predicted = tp = unsure = unsure_right = 0
    for r in ranked:
        verdict, target = verdicts[r.query]
        if verdict == "unsure" and target is not None:
            unsure += 1
            unsure_right += r.target is not None and r.is_hit(target, cluster)
        if verdict != "duplicate_of" or target is None:
            continue
        predicted += 1
        tp += r.target is not None and r.is_hit(target, cluster)
    precision = tp / predicted if predicted else 0.0
    recall = tp / positives if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if tp else 0.0
    return VerdictScore(predicted, unsure, unsure_right, precision, recall, f1)
