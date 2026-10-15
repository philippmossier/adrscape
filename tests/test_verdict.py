from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from cofabriq.issues.model import Comment, Issue
from cofabriq.issues.verdict import (
    VERSION,
    Answer,
    Candidate,
    Job,
    Usage,
    check,
    judge_all,
    prompt,
)
from tests.test_dedupe import issue

USAGE = Usage(input_tokens=1000, output_tokens=200, latency_ms=1500)

OLD = issue(1, "Download restarts", "The model download starts over\nafter refresh.")
OTHER = issue(2, "Dark mode", "Colours are off.")
NEW = issue(9, "Reload kills download", "Reloading the page restarts the download.")


def candidates() -> list[Candidate]:
    return [Candidate.of(OLD, NEW), Candidate.of(OTHER, NEW)]


def test_candidate_hides_later_and_bot_comments() -> None:
    t, later = OLD.created_at, NEW.created_at + timedelta(days=1)
    old = OLD.model_copy(
        update={
            "comments": [
                Comment(author="a", bot=False, body="same here", created_at=t),
                Comment(author="b", bot=True, body="Related: #5", created_at=t),
                Comment(author="c", bot=False, body="dup of #9", created_at=later),
            ]
        }
    )

    c = Candidate.of(old, NEW)

    assert [x.body for x in c.comments] == ["same here"]
    assert "dup of" not in prompt(NEW, [c])


def test_check_keeps_a_quoted_duplicate() -> None:
    answer = Answer(
        verdict="duplicate_of",
        target=1,
        evidence="download starts over after refresh",  # across a line break
        problem="bug",
    )

    record = check(NEW, candidates(), answer, USAGE)

    assert (record.verdict, record.target, record.dropped) == ("duplicate_of", 1, [])
    assert record.candidates == [1, 2]
    assert record.cost_usd == pytest.approx(1000 * 0.10e-6 + 200 * 0.50e-6)


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            Answer(verdict="duplicate_of", target=7, evidence="x", problem=None),
            "not a candidate",
        ),
        (
            Answer(verdict="duplicate_of", target=None, evidence="x", problem=None),
            "not a candidate",
        ),
        # a quote from the query, not the target
        (
            Answer(
                verdict="duplicate_of",
                target=1,
                evidence="Reloading the page",
                problem=None,
            ),
            "evidence not in the candidate",
        ),
        # quote from the wrong candidate
        (
            Answer(
                verdict="related", target=1, evidence="Colours are off", problem=None
            ),
            "evidence not in the candidate",
        ),
        (
            Answer(verdict="duplicate_of", target=1, evidence=" ", problem=None),
            "evidence not in",
        ),
    ],
)
def test_check_drops_unsupported_verdicts(answer: Answer, reason: str) -> None:
    record = check(NEW, candidates(), answer, USAGE)

    assert (record.verdict, record.target, record.evidence) == ("none", None, "")
    assert len(record.dropped) == 1 and reason in record.dropped[0]


def test_check_clears_target_on_none() -> None:
    answer = Answer(
        verdict="none", target=1, evidence="Download restarts", problem=None
    )

    record = check(NEW, candidates(), answer, USAGE)

    assert (record.target, record.evidence, record.dropped) == (None, "", [])


def test_check_keeps_a_quoted_unsure_with_its_target() -> None:
    answer = Answer(
        verdict="unsure", target=1, evidence="Download restarts", problem="bug"
    )

    record = check(NEW, candidates(), answer, USAGE)

    assert (record.verdict, record.target, record.problem) == ("unsure", 1, "bug")


def test_check_drops_an_unquoted_unsure() -> None:
    answer = Answer(verdict="unsure", target=2, evidence="restarts", problem="bug")

    record = check(NEW, candidates(), answer, USAGE)

    assert (record.verdict, record.target, record.problem) == ("none", None, None)


class FakeJudge:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def judge(
        self, query: Issue, candidates: Sequence[Candidate]
    ) -> tuple[Answer, Usage]:
        self.calls.append(query.number)
        top = candidates[0]
        answer = Answer(
            verdict="duplicate_of", target=top.number, evidence=top.title, problem=None
        )
        return answer, USAGE


def test_judge_all_caches_by_version_and_candidates(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    third = issue(10, "Download again", "Still restarts.")
    jobs = [
        Job(NEW, candidates()),
        Job(third, [Candidate.of(OLD, third)]),
    ]
    judge = FakeJudge()

    records, report = judge_all(jobs, path, judge, workers=2)
    assert (report.judged, report.cached) == (2, 0)
    assert records[9].target == 1 and records[9].version == VERSION

    _, report = judge_all(jobs, path, judge)
    assert (report.judged, report.cached) == (0, 2)

    # new candidates for #9 (a re-embed): judged again, #10 still cached
    changed = [Job(NEW, candidates()[::-1]), jobs[1]]
    records, report = judge_all(changed, path, judge)
    assert (report.judged, report.cached) == (1, 1)
    assert records[9].target == 2
    assert sorted(judge.calls) == [9, 9, 10]
