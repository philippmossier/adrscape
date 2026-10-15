"""Missing-info eval: does the card ask for more exactly when a maintainer did?

Ground truth from maintainer actions, not from us. Positive (`needs_info`):
the maintainers labelled the report as lacking information (vscode:
`info-needed`). Negative (`complete`): a bug fixed by a merged pull request
or a commit that never carried that label, so someone could act on it as
written. Both come from the same issue template, so both sides are user
reports, not team members' notes to each other. Both bodies are unedited:
the text scored is the text the maintainer reacted to.
"""

import json
import random
import tomllib
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, TypeAdapter

from cofabriq.issues.github import History
from cofabriq.issues.model import Issue
from cofabriq.issues.readiness import InfoItem, check_info
from cofabriq.issues.triage import PROMPT_VERSION, Fields, Triager, shown
from cofabriq.issues.verdict import PRICE_IN, PRICE_OUT


class Rules(BaseModel):
    """`[readiness]` in `targets/<owner>-<name>.toml`, fixed before scoring."""

    window: tuple[date, date]
    # Held out: the method is chosen on `window` and scored once on this.
    test_window: tuple[date, date] | None = None
    template: str  # text every report from the issue template contains
    needs_info_label: str
    bug_label: str
    sample: int


def load_rules(targets_dir: Path, repo: str) -> Rules | None:
    path = targets_dir / f"{repo.replace('/', '-')}.toml"
    if not path.is_file():
        return None
    table = tomllib.loads(path.read_text("utf-8")).get("readiness")
    return None if table is None else Rules.model_validate(table)


class InfoSet(BaseModel):
    """Issue numbers only, like the dedupe set."""

    repo: str
    frozen_at: datetime
    rules: Rules
    needs_info: list[int]
    complete: list[int]


@dataclass(frozen=True)
class Pools:
    needs_info: list[int]
    complete: list[int]


def pools(issues: Sequence[Issue], rules: Rules) -> Pools:
    """Candidates from the stored snapshot; `verify` then checks each one's
    history on GitHub (labels removed since, edited bodies)."""
    start, end = rules.window
    window = [
        i
        for i in issues
        if start <= i.created_at.date() <= end and rules.template in i.body
    ]
    return Pools(
        needs_info=sorted(
            i.number for i in window if rules.needs_info_label in i.labels
        ),
        complete=sorted(
            i.number
            for i in window
            if rules.bug_label in i.labels
            and rules.needs_info_label not in i.labels
            and i.state_reason == "completed"
            and i.closed_by is not None
        ),
    )


type HistoryOf = Callable[[Sequence[int]], Mapping[int, History]]


def build(
    repo: str,
    pool: Pools,
    rules: Rules,
    history: HistoryOf,
    frozen_at: datetime,
    seed: int = 0,
) -> InfoSet:
    """Sample each side in a fixed shuffled order and keep the first
    `rules.sample` issues whose history passes, so only those few are looked
    up on GitHub."""
    label = rules.needs_info_label

    def keep(numbers: list[int], ok: Callable[[History], bool]) -> list[int]:
        order = list(numbers)
        random.Random(seed).shuffle(order)
        kept: list[int] = []
        for start in range(0, len(order), rules.sample):
            batch = order[start : start + rules.sample]
            found = history(batch)
            kept += [n for n in batch if n in found and ok(found[n])]
            if len(kept) >= rules.sample:
                break
        return sorted(kept[: rules.sample])

    return InfoSet(
        repo=repo,
        frozen_at=frozen_at,
        rules=rules,
        needs_info=keep(pool.needs_info, lambda h: not h.body_edited),
        complete=keep(
            pool.complete, lambda h: not h.body_edited and label not in h.labels_ever
        ),
    )


@dataclass(frozen=True)
class Score:
    predicted: int  # issues the method says need info
    precision: float
    recall: float
    f1: float


def score(infoset: InfoSet, asks: Mapping[int, bool]) -> Score:
    """Precision and recall of "needs info" against the maintainers' label.
    `asks` maps every issue of the set to the method's answer."""
    positives = set(infoset.needs_info)
    predicted = {n for n, ask in asks.items() if ask}
    tp = len(predicted & positives)
    precision = tp / len(predicted) if predicted else 0.0
    recall = tp / len(positives) if positives else 0.0
    f1 = 2 * precision * recall / (precision + recall) if tp else 0.0
    return Score(len(predicted), precision, recall, f1)


class FieldsRecord(BaseModel):
    """One card-fields answer, cached per issue and prompt version."""

    number: int
    version: str
    fields: Fields
    cost_usd: float
    latency_ms: int


_records = TypeAdapter(list[FieldsRecord])  # for saving


def run_fields(
    issues: Sequence[Issue], triager: Triager, path: Path, workers: int = 8
) -> dict[int, FieldsRecord]:
    """The card call for every issue (no components: the eval scores info
    only), reusing cached answers of the current prompt version. Saved even
    if a call fails halfway, so a rerun only pays for what is missing."""
    raw: list[dict[str, object]] = (
        json.loads(path.read_bytes()) if path.is_file() else []
    )
    # Other prompt versions are skipped unvalidated: their fields may differ.
    stored = {
        r.number: r
        for r in (
            FieldsRecord.model_validate(x)
            for x in raw
            if x["version"] == PROMPT_VERSION
        )
    }
    done = {i.number: stored[i.number] for i in issues if i.number in stored}
    todo = [i for i in issues if i.number not in done]

    def run(issue: Issue) -> FieldsRecord:
        fields, usage = triager.fields(issue, {})
        cost = usage.input_tokens * PRICE_IN + usage.output_tokens * PRICE_OUT
        return FieldsRecord(
            number=issue.number,
            version=PROMPT_VERSION,
            fields=fields,
            cost_usd=cost,
            latency_ms=usage.latency_ms,
        )

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for record in pool.map(run, todo):
                done[record.number] = record
    finally:
        kept = sorted((stored | done).values(), key=lambda r: r.number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_records.dump_json(kept, indent=2))
    return done


def missing(record: FieldsRecord, issue: Issue) -> list[InfoItem]:
    """Required info without a verified quote, as on the card."""
    return check_info(record.fields.type, record.fields.info, shown(issue))[0]
