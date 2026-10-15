from datetime import UTC, datetime, timedelta

import pytest

from cofabriq.context.store import postgres
from cofabriq.eval.dedupe import Ranked, build, score, score_verdicts
from cofabriq.issues import vectors
from cofabriq.issues.model import Comment, Issue, IssueRef
from tests.test_embed_store import FakeEmbedder

REPO = "acme/app"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
URL = f"https://github.com/{REPO}/issues"


def issue(
    number: int,
    title: str = "t",
    body: str = "",
    comments: list[tuple[str, bool]] | None = None,
    reason: str | None = None,
    duplicate_of: list[int] | None = None,
    duplicates: list[int] | None = None,
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
        labels=[],
        created_at=created,
        updated_at=created,
        closed_at=None,
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
