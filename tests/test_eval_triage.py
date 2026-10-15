from pathlib import Path

import pytest

from cofabriq.eval.triage import Injected, followed, load_seed, match, score
from cofabriq.issues.readiness import Readiness
from cofabriq.issues.triage import (
    Duplicate,
    IssueInfo,
    Run,
    TriageCard,
    load_components,
)
from tests.test_dedupe import issue

SEED = Path("playground/seed.toml")


def card(**update: object) -> TriageCard:
    base = TriageCard(
        issue=IssueInfo(repo="acme/app", number=1, url="u", state="open", labels=[]),
        type="bug",
        severity="high",
        component="models",
        summary="Reload restarts the download.",
        repro_steps=[],
        missing_info=[],
        blocking=False,
        missing=[],
        readiness=Readiness(route="ready", reasons=[]),
        reply=None,
        duplicate=Duplicate(verdict="none", target=None, evidence="", problem=None),
        related_issues=[],
        related_docs=[],
        dropped=[],
        conflicts=[],
        run=Run(model="m", prompt_version="p", cost_usd=0.001, latency_ms=1000),
    )
    return base.model_copy(update=update)


def test_the_committed_seed_is_consistent() -> None:
    seed = load_seed(SEED)
    components = load_components(
        Path("targets"), "philippmossier/local-ai-chat-playground"
    )

    assert 15 <= len(seed) <= 30
    assert 3 <= sum(s.injected is not None for s in seed) <= 5
    assert sum(s.duplicate_of is not None for s in seed) >= 3
    assert {s.component for s in seed} - {None} <= set(components)


def test_seed_rejects_a_forward_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "seed.toml"
    path.write_text(
        '[[issue]]\nkey = "a"\ntitle = "A"\nbody = ""\ntype = "bug"\n'
        'duplicate_of = "b"\n'
        '[[issue]]\nkey = "b"\ntitle = "B"\nbody = ""\ntype = "bug"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="earlier key"):
        load_seed(path)


def test_match_by_title_newest_wins() -> None:
    seed = load_seed(SEED)[:1]
    title = seed[0].title

    found = match(seed, [issue(3, title), issue(7, title), issue(9, "other")])

    assert found[seed[0].key].number == 7
    with pytest.raises(LookupError, match="not fetched"):
        match(seed, [issue(9, "other")])


def test_followed_checks_fields_and_text() -> None:
    injected = Injected(severity="critical", text="APPROVED FOR MERGE")

    assert followed(injected, card()) == []
    assert followed(injected, card(summary="Approved for merge.")) == [
        "text 'APPROVED FOR MERGE'"
    ]
    assert followed(injected, card(severity="critical")) == ["severity=critical"]


def test_score_counts_hits_misses_and_false_duplicates() -> None:
    seed = [
        s
        for s in load_seed(SEED)
        if s.key in {"model-download-reload", "model-download-refresh"}
    ]
    numbers = {"model-download-reload": 1, "model-download-refresh": 11}
    cards = {
        "model-download-reload": card(
            duplicate=Duplicate(
                verdict="duplicate_of", target=5, evidence="x", problem=None
            )
        ),
        "model-download-refresh": card(
            component="inference",
            duplicate=Duplicate(
                verdict="duplicate_of", target=1, evidence="x", problem=None
            ),
        ),
    }

    s = score(seed, cards, numbers)

    assert (s.n, s.type_ok, s.component_ok) == (2, 2, 1)
    assert (s.duplicates, s.duplicates_found, s.false_duplicates) == (1, 1, 1)
    assert s.misses == [
        "model-download-reload: duplicate_of #5, expected none",
        "model-download-refresh: component inference, expected models",
    ]


def test_score_counts_conflicts_and_whether_the_type_was_wrong() -> None:
    seed = [
        s
        for s in load_seed(SEED)
        if s.key in {"model-download-reload", "model-download-refresh"}
    ]
    numbers = {"model-download-reload": 1, "model-download-refresh": 11}
    flagged = ["type feature, but duplicate_of #1, which the verdict reads as a bug"]
    cards = {
        "model-download-reload": card(),
        "model-download-refresh": card(
            type="feature",
            severity=None,
            duplicate=Duplicate(
                verdict="duplicate_of", target=1, evidence="x", problem="bug"
            ),
            conflicts=flagged,
        ),
    }

    s = score(seed, cards, numbers)

    assert (s.conflicts, s.conflicts_on_type_miss) == (1, 1)
    assert f"model-download-refresh: conflict ({flagged[0]})" in s.misses
