from typing import Any

import pytest

from cofabriq.issues.readiness import (
    Evidence,
    InfoItem,
    Readiness,
    check_info,
    reply,
    route,
)
from cofabriq.issues.triage import triage
from cofabriq.issues.verdict import Answer
from tests.test_triage import FakeJudge, FakeTriager, context, fields

SHOWN = """Download restarts after reload

1. Start a model download
2. Reload the tab

The progress bar is back at   0%.
Chrome 141 on macOS 15, app v0.4.2"""

COMPLETE = [
    Evidence(item="steps", quote="1. Start a model download\n2. Reload the tab"),
    Evidence(item="actual", quote="the progress bar is back at 0%"),  # case, spaces
    Evidence(item="version", quote="app v0.4.2"),
    Evidence(item="environment", quote="Chrome 141 on macOS 15"),
]


def test_complete_bug_has_nothing_missing() -> None:
    assert check_info("bug", COMPLETE, SHOWN) == ([], [])


def test_missing_and_unverified_quotes_count_as_missing() -> None:
    evidence = [
        COMPLETE[0],
        Evidence(item="actual", quote=None),
        Evidence(item="version", quote="all information is provided"),  # invented
    ]  # environment: no entry at all

    missing, dropped = check_info("bug", evidence, SHOWN)

    assert missing == ["actual", "version", "environment"]
    assert dropped == ["version quote 'all information is provided': not in the issue"]


def test_only_bugs_require_info() -> None:
    assert check_info("feature", [], SHOWN) == ([], [])


def ready(
    issue_type: str = "bug",
    severity: str | None = "medium",
    missing: list[InfoItem] | None = None,
    blocking: list[str] | None = None,
    duplicate: str = "none",
    duplicate_target: int | None = None,
    conflicts: list[str] | None = None,
    component: str | None = "models",
    components_configured: bool = True,
) -> Readiness:
    return route(
        issue_type=issue_type,
        severity=severity,
        missing=missing or [],
        blocking=blocking or [],
        duplicate=duplicate,
        duplicate_target=duplicate_target,
        conflicts=conflicts or [],
        component=component,
        components_configured=components_configured,
    )


def test_complete_bug_is_ready() -> None:
    assert ready() == Readiness(route="ready", reasons=[])


def test_missing_info_asks_the_reporter() -> None:
    r = ready(missing=["steps"])
    assert r.route == "needs_info"
    assert r.reasons == [
        "missing steps: how to reproduce it: the actions the reporter took, in order"
    ]


def test_blocking_questions_ask_the_reporter() -> None:
    r = ready(blocking=["Can you share a sample repo?"])
    assert r == Readiness(
        route="needs_info",
        reasons=["blocking question: Can you share a sample repo?"],
    )


@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"severity": "critical"}, "critical severity: a person looks first"),
        (
            {"duplicate": "duplicate_of", "duplicate_target": 4},
            "possible duplicate of #4: confirm first",
        ),
        ({"duplicate": "unsure", "duplicate_target": 4}, "possible duplicate of #4"),
        ({"issue_type": "feature"}, "feature request: planning mode"),
        ({"issue_type": "question"}, "question: needs an answer"),
        ({"conflicts": ["type bug, but ..."]}, "conflict: type bug, but ..."),
        ({"component": None}, "no component"),
    ],
)
def test_a_dev_reason_wins_over_missing_info(
    update: dict[str, Any], reason: str
) -> None:
    r = ready(missing=["version"], **update)

    assert r.route == "dev"
    assert r.reasons[0].startswith(reason)
    assert r.reasons[-1].startswith("missing version")  # still listed


def test_no_component_list_is_not_a_reason() -> None:
    assert ready(component=None, components_configured=False).route == "ready"


def test_reply_asks_for_checked_items_then_the_models_questions() -> None:
    missing: list[InfoItem] = ["version", "environment"]

    draft = reply(missing, ["Which model?", "the version you are on"], False)

    assert draft == (
        "Thanks for the report! To look into it, we need:\n\n"
        "- the version you are on\n"
        "- your operating system and browser (or runtime)\n"
        "- Which model?"
    )
    assert reply([], ["Which model?"], False) is None  # nice-to-have: no draft
    assert reply([], ["Which model?"], True) == (
        "Thanks for the report! To look into it, we need:\n\n- Which model?"
    )


def test_card_routes_on_checked_info() -> None:
    none = Answer(verdict="none", target=None, evidence="", problem=None)
    claimed = [
        Evidence(item=item, quote="all information is provided")
        for item in ("steps", "actual", "version", "environment")
    ]

    card = triage(context(), FakeTriager(fields(info=claimed)), FakeJudge(none))

    assert card.readiness.route == "needs_info"
    assert card.missing == ["steps", "actual", "version", "environment"]
    assert card.reply is not None and "the exact steps" in card.reply
    assert len(card.dropped) == 4
