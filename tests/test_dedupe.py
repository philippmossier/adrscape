from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest

from cofabriq.context.store import postgres
from cofabriq.eval.dedupe import (
    Ranked,
    Rules,
    build,
    load_rules,
    named_targets,
    score,
    score_verdicts,
)
from cofabriq.issues import vectors
from cofabriq.issues.model import Comment, Issue, IssueRef
from tests.test_embed_store import FakeEmbedder

REPO = "acme/app"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
URL = f"https://github.com/{REPO}/issues"
OTHER = "https://github.com/acme/other/issues"


def issue(
    number: int,
    title: str = "t",
    body: str = "",
    comments: list[tuple[str, bool]] | None = None,
    reason: str | None = None,
    duplicate_of: list[int] | None = None,
    duplicates: list[int] | None = None,
    labels: list[str] | None = None,
    closed_by: Literal["pull_request", "commit"] | None = None,
) -> Issue:
    created = T0 + timedelta(days=number)
    return Issue(
        repo=REPO,
        number=number,
        title=title,
        body=body,
        state="closed" if reason else "open",
        state_reason=reason,
        author="someone",
        labels=labels or [],
        created_at=created,
        updated_at=created,
        closed_at=None,
        closed_by=closed_by,
        comment_count=len(comments or []),
        comments=[
            Comment(author="x", bot=bot, body=text, created_at=created)
            for text, bot in comments or []
        ],
        duplicate_of=[IssueRef(repo=REPO, number=n) for n in duplicate_of or []],
        duplicates=[IssueRef(repo=REPO, number=n) for n in duplicates or []],
    )


def test_build_labels_pairs_from_marks_and_human_comments() -> None:
    issues = [
        issue(1, duplicates=[5]),  # mark seen from the canonical side
        issue(2),
        issue(3),
        issue(4),
        issue(5, reason="duplicate"),
        issue(6, comments=[(f"Duplicate of {URL}/2", False)], reason="duplicate"),
        issue(7, comments=[("duplicate of #3", True)]),  # bot: ignored
        issue(8, comments=[("dup", False), ("duplicate of #9", False)]),  # later
        issue(9),
        issue(10, comments=[("duplicate of #1 or #2? duplicate of #2", False)]),
        issue(11),
        issue(12),
    ]

    evalset, report = build(issues, frozen_at=T0)

    assert [(p.query, p.target, p.signal) for p in evalset.pairs] == [
        (5, 1, "mark"),
        (6, 2, "comment"),
    ]
    assert (report.ambiguous, report.unusable) == (1, 1)
    # negatives: no signal, not the oldest issue, not a target
    assert len(evalset.negatives) == 2
    assert set(evalset.negatives) <= {3, 4, 7, 11, 12}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/duplicate #212090", {212090}),
        (f"/duplicate\n\n{URL}/177046\n\nTracked there", {177046}),
        (f"Duplicate {URL}/70768", {70768}),
        (f"Closing as duplicate of [#217987]({URL}/217987)", {217987}),
        (f"same root cause as {URL}/189161\n\n/duplicate ", {189161}),
        ("Closing as duplicate of #29036. Fix merged via PR #29037.", {29036}),
        ("/duplicate\n\nsee #1 and #2", {1, 2}),  # the builder drops it as ambiguous
        ("/duplicate", set[int]()),  # no target named
        ("is #51364 the one you mean?", set[int]()),  # not a duplicate comment
        (f"Duplicate of acme/other#5, {OTHER}/6", set[int]()),
        ("this duplicates the old flow, see #7", set[int]()),
    ],
)
def test_named_targets(body: str, expected: set[int]) -> None:
    assert named_targets(body, REPO) == expected


def test_rules_gate_comments_by_label_and_cut_the_window() -> None:
    rules = Rules(
        window=(date(2026, 1, 10), date(2026, 1, 20)),
        duplicate_label="*duplicate",
        negatives="closed_by_change",
    )
    dup = ["*duplicate"]
    issues = [
        issue(1),
        issue(2),
        issue(3, comments=[("/duplicate #1", False)], labels=dup),  # before window
        issue(11, comments=[("/duplicate #1", False)], labels=dup),
        issue(12, comments=[("/duplicate #2", False)]),  # no label: no pair
        issue(13, duplicate_of=[2]),  # GitHub's mark needs no label
        issue(14, comments=[("/duplicate #2", True)], labels=dup),  # bot
        issue(15, comments=[("/duplicate #2 🤖", False)], labels=dup),
        issue(16, reason="completed", closed_by="pull_request"),
        issue(17, reason="completed", closed_by="commit"),
        issue(18, reason="completed"),  # closed by a person: not provable
        issue(19, reason="completed", closed_by="pull_request", labels=dup),
        issue(25, reason="completed", closed_by="commit"),  # after window
    ]

    evalset, report = build(issues, frozen_at=T0, rules=rules)

    assert [(p.query, p.target) for p in evalset.pairs] == [(11, 1), (13, 2)]
    assert report.negative_pool == 2
    assert evalset.negatives == [16, 17]
    assert evalset.rules == rules


def test_sample_keeps_pairs_and_matches_negatives() -> None:
    rules = Rules(sample=3)
    issues = [issue(1)]
    issues += [issue(n, duplicate_of=[1]) for n in range(2, 12)]
    issues += [issue(n) for n in range(12, 30)]

    a, report = build(issues, frozen_at=T0, rules=rules)
    b, _ = build(issues, frozen_at=T0, rules=rules)

    assert (report.found, report.pairs, len(a.negatives)) == (10, 3, 3)
    assert a == b
    # siblings still come from the whole cluster, not the sample
    assert all(len(p.siblings) == 9 for p in a.pairs)


def test_load_rules(tmp_path: Path) -> None:
    (tmp_path / "acme-app.toml").write_text(
        '[dedupe]\nwindow = ["2023-01-01", "2024-12-31"]\nsample = 200\n'
    )

    assert load_rules(tmp_path, REPO) == Rules(
        window=(date(2023, 1, 1), date(2024, 12, 31)), sample=200
    )
    assert load_rules(tmp_path, "acme/none") == Rules()


def test_siblings_come_from_the_whole_cluster() -> None:
    issues = [
        issue(1),
        issue(2, duplicate_of=[1]),
        issue(3, duplicate_of=[1]),
        issue(4, comments=[("duplicate of #3", False)]),  # chained via #3
        issue(5),
    ]

    evalset, _ = build(issues, frozen_at=T0)

    assert {p.query: p.siblings for p in evalset.pairs} == {
        2: [3, 4],
        3: [2, 4],
        4: [1, 2],
    }


def test_build_is_deterministic() -> None:
    issues = [issue(n) for n in range(1, 30)] + [issue(30, duplicate_of=[1])]
    a, _ = build(issues, frozen_at=T0)
    b, _ = build(issues, frozen_at=T0)
    assert a == b


def test_score_recall_and_threshold_baseline() -> None:
    ranked = [
        Ranked(10, 1, [(1, 0.9), (2, 0.5)]),  # top-1 right
        Ranked(11, 2, [(3, 0.8), (2, 0.7)]),  # in top 5, not top-1
        Ranked(12, 4, [(5, 0.6)]),  # missed
        Ranked(20, None, [(6, 0.85)]),  # negative, scores high
    ]

    m = score(ranked, ks=(1, 5))

    assert m.recall_at == {1: pytest.approx(1 / 3), 5: pytest.approx(2 / 3)}
    # t=0.9 predicts only #10: precision 1, recall 1/3, best F1 0.5
    assert m.best.value == 0.9
    assert (m.best.precision, m.best.f1) == (1.0, 0.5)


def test_cluster_recall_accepts_a_sibling() -> None:
    ranked = [
        Ranked(10, 1, [(7, 0.9), (1, 0.5)], frozenset({7})),  # top-1 is a sibling
        Ranked(11, 2, [(3, 0.8)], frozenset({9})),
    ]

    strict = score(ranked, ks=(1, 5))
    cluster = score(ranked, ks=(1, 5), cluster=True)

    assert strict.recall_at == {1: 0.0, 5: 0.5}
    assert cluster.recall_at == {1: 0.5, 5: 0.5}
    assert cluster.best.precision == 1.0


def test_score_verdicts_counts_only_duplicate_of() -> None:
    ranked = [
        Ranked(10, 1, [(1, 0.9)]),  # right target
        Ranked(11, 2, [(7, 0.8)], frozenset({7})),  # names a sibling
        Ranked(12, 4, [(5, 0.6)]),  # says related: no prediction
        Ranked(13, 3, [(3, 0.7)]),  # unsure on the right target: no prediction
        Ranked(20, None, [(6, 0.85)]),  # negative called a duplicate
        Ranked(21, None, [(8, 0.8)]),  # unsure on a negative
    ]
    verdicts = {
        10: ("duplicate_of", 1),
        11: ("duplicate_of", 7),
        12: ("related", 5),
        13: ("unsure", 3),
        20: ("duplicate_of", 6),
        21: ("unsure", 8),
    }

    strict = score_verdicts(ranked, verdicts)
    cluster = score_verdicts(ranked, verdicts, cluster=True)

    assert (strict.predicted, strict.precision, strict.recall) == (
        3,
        pytest.approx(1 / 3),
        pytest.approx(1 / 4),
    )
    assert (strict.unsure, strict.unsure_right) == (2, 1)
    assert (cluster.precision, cluster.recall) == (
        pytest.approx(2 / 3),
        pytest.approx(2 / 4),
    )


def test_issue_vectors_skip_unchanged_and_cut_by_time(conn: postgres.Conn) -> None:
    embed = FakeEmbedder()
    issues = [
        issue(1, "login button broken", "safari", [("duplicate of #9", False)]),
        issue(2, "dark mode", "colors"),
        issue(3, "login button broken again", "safari"),
        issue(4, "login button", "chrome"),
    ]

    report = vectors.sync(conn, issues, embed)
    edited = issue(4, "login button", "firefox")
    again = vectors.sync(conn, [*issues[:3], edited], embed)

    assert (report.embedded, again.embedded, again.unchanged) == (4, 1, 3)
    hits = vectors.nearest(conn, REPO, 3, k=5, before=issues[2].created_at)
    assert [h.number for h in hits] == [1, 2]  # #4 is later: never a candidate
    assert hits[0].score > hits[1].score
    assert all("duplicate" not in t for t in embed.seen)  # comments never embedded
