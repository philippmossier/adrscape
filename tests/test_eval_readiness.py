import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

import httpx

from cofabriq.eval.readiness import (
    FieldsRecord,
    InfoSet,
    Pools,
    Rules,
    build,
    pools,
    run_fields,
    score,
)
from cofabriq.issues.github import History
from cofabriq.issues.model import Issue
from cofabriq.issues.triage import PROMPT_VERSION, Fields
from cofabriq.issues.verdict import Usage
from tests.test_dedupe import T0, issue
from tests.test_issues import source
from tests.test_triage import fields

TEMPLATE = "Does this issue occur when all extensions are disabled"
RULES = Rules(
    window=(date(2026, 1, 3), date(2026, 1, 30)),
    template=TEMPLATE,
    needs_info_label="info-needed",
    bug_label="bug",
    sample=2,
)


def reported(number: int, labels: list[str], **update: object) -> Issue:
    base = issue(number, body=f"{TEMPLATE}: Yes", labels=labels)
    return base.model_copy(update=update)


def test_pools_take_template_reports_in_the_window() -> None:
    fixed = {"state_reason": "completed", "closed_by": "pull_request"}
    issues = [
        reported(1, ["info-needed"]),  # before the window
        reported(2, ["info-needed"]),
        issue(3, labels=["info-needed"]),  # no template
        reported(4, ["bug"], **fixed),
        reported(5, ["bug"], state_reason="completed"),  # closed by a person
        reported(6, ["bug", "info-needed"], **fixed),  # asked, then fixed: positive
        reported(7, ["feature-request"], **fixed),
    ]

    assert pools(issues, RULES) == Pools(needs_info=[2, 6], complete=[4])


def test_build_keeps_issues_whose_history_passes() -> None:
    histories = {
        10: History(frozenset({"info-needed"}), body_edited=False),
        11: History(frozenset({"info-needed"}), body_edited=True),  # edited
        12: History(frozenset({"info-needed"}), body_edited=False),
        20: History(frozenset({"bug", "info-needed"}), body_edited=False),  # removed
        21: History(frozenset({"bug"}), body_edited=False),
        22: History(frozenset({"bug"}), body_edited=False),
    }
    asked: list[list[int]] = []

    def history(numbers: Sequence[int]) -> Mapping[int, History]:
        asked.append(list(numbers))
        return {n: histories[n] for n in numbers if n in histories}

    infoset = build(
        "acme/app", Pools([10, 11, 12, 13], [20, 21, 22]), RULES, history, T0
    )

    assert infoset.needs_info == [10, 12]
    assert infoset.complete == [21, 22]
    assert all(len(batch) <= RULES.sample for batch in asked)


def test_score_against_the_label() -> None:
    infoset = InfoSet(
        repo="acme/app", frozen_at=T0, rules=RULES, needs_info=[1, 2], complete=[3, 4]
    )

    s = score(infoset, {1: True, 2: False, 3: True, 4: False})

    assert (s.predicted, s.precision, s.recall) == (2, 0.5, 0.5)


class CountingTriager:
    def __init__(self) -> None:
        self.calls = 0

    def fields(self, issue: Issue, components: dict[str, str]) -> tuple[Fields, Usage]:
        self.calls += 1
        return fields(), Usage(1000, 100, 900)


def test_run_fields_caches_per_prompt_version(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    old = {"number": 2, "version": "triage-v0", "fields": {"type": "bug"}}
    path.write_text(json.dumps([old]))  # an older version, other fields
    issues = [issue(1), issue(2)]
    triager = CountingTriager()

    first = run_fields(issues, triager, path)
    again = run_fields(issues, triager, path)

    assert triager.calls == 2
    assert first == again
    assert {r.version for r in first.values()} == {PROMPT_VERSION}
    stored = [FieldsRecord.model_validate(r) for r in json.loads(path.read_text())]
    assert [r.number for r in stored] == [1, 2]


def test_history_reads_labels_ever_and_body_edits() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert "i7: issue(number: 7)" in json.loads(request.content)["query"]
        node = {
            "lastEditedAt": None,
            "timelineItems": {"nodes": [{"label": {"name": "info-needed"}}, {}]},
        }
        rate = {"cost": 1, "remaining": 5000, "resetAt": "2026-10-13T10:20:17Z"}
        data = {"rateLimit": rate, "repository": {"i7": node, "i8": None}}
        return httpx.Response(200, json={"data": data})

    found = source(handle, []).history([7, 8])

    assert found == {7: History(frozenset({"info-needed"}), body_edited=False)}
