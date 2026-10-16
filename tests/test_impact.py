from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from cofabriq.eval.impact import (
    Case,
    Rules,
    build,
    fill,
    knn,
    score,
    touched,
)
from cofabriq.map.components import ComponentRules
from cofabriq.map.fixes import Fix, closing_pattern, fixes
from cofabriq.map.history import rev_at
from cofabriq.map.impact import assemble, spread
from tests.gitrepo import init_repo
from tests.test_dedupe import REPO, T0, issue
from tests.test_map import dated_commit

RULES = ComponentRules(patterns=["src/*"])
URL = f"https://github.com/{REPO}/issues"


@pytest.mark.parametrize(
    ("message", "numbers"),
    [
        ("Fixes #12", ["12"]),
        ("terminal: fix #12, closes #13", ["12", "13"]),
        (f"resolved: {URL}/14", ["14"]),
        ("Fixed #15 (#16)", ["15"]),  # (#16) is the PR, a mention
        ("see #17", []),
        ("fixes acme/other#18", []),
    ],
)
def test_closing_pattern(message: str, numbers: list[str]) -> None:
    assert closing_pattern(REPO).findall(message) == numbers


def test_fixes_from_commit_messages(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, {"src/a/x.ts": "1"})
    dated_commit(repo, "2023-01-02", {"src/a/x.ts": "2"}, "Fix #5")
    dated_commit(repo, "2023-01-03", {"src/b/y.ts": "1"}, "follow-up, fixes #5")
    dated_commit(repo, "2023-01-04", {"src/b/y.ts": "2"}, "mention #6")

    found = fixes(repo, rev_at(repo, None), REPO)

    assert list(found) == [5]
    assert sorted(f.files for f in found[5]) == [("src/a/x.ts",), ("src/b/y.ts",)]
    assert touched(found[5], RULES) == ["src/a", "src/b"]


def fix(day: int, *files: str) -> Fix:
    return Fix(f"sha{day}", datetime(2026, 1, day, tzinfo=UTC), files)


def test_build_takes_closed_fixed_issues_in_the_window() -> None:
    rules = Rules(
        map_until=date(2025, 12, 31),
        window=(date(2026, 1, 3), date(2026, 1, 20)),
        test_window=(date(2026, 2, 1), date(2026, 2, 28)),
        sample=10,
    )
    closed = {"state_reason": "completed", "closed_by": "commit"}
    issues = [
        issue(1).model_copy(update=closed),  # before the window
        issue(3).model_copy(update=closed),
        issue(4).model_copy(update=closed),  # no fix commit
        issue(5).model_copy(update={"state_reason": "completed"}),  # by a person
        issue(6).model_copy(update=closed),  # fix too big
    ]
    found = {
        1: [fix(2, "src/a/x.ts")],
        3: [fix(4, "src/a/x.ts"), fix(5, "src/b/y.ts")],
        5: [fix(6, "src/a/x.ts")],
        6: [fix(7, *(f"src/c/{n}.ts" for n in range(51)))],
    }

    impact, pool = build(issues, found, RULES, rules, rules.window, T0)

    assert pool == 1
    assert impact.cases == [Case(issue=3, components=["src/a", "src/b"])]


def test_knn_votes_with_fixes_known_when_the_issue_was_filed() -> None:
    found = {
        1: [fix(2, "src/a/x.ts")],
        2: [fix(3, "src/b/y.ts"), fix(20, "src/c/z.ts")],  # #2's 2nd fix: later
        3: [fix(4, "src/a/x.ts")],
    }

    ranked = knn(
        [(1, 0.9), (2, 0.8), (3, 0.5)], datetime(2026, 1, 10, tzinfo=UTC), found, RULES
    )

    assert ranked == [("src/a", pytest.approx(1.4)), ("src/b", 0.8)]


def test_spread_adds_co_change_partners_of_the_top() -> None:
    partners = {"a": [("x", 0.5)], "b": [("a", 0.25)]}

    ranked = spread([("a", 1.0), ("b", 0.4)], lambda c: partners.get(c, []))

    assert ranked == [("a", 1.1), ("x", 0.5), ("b", 0.4)]


def test_fill_and_score() -> None:
    predicted = {1: fill([("a", 1.0)], ["b", "a", "c"]), 2: fill([], ["b", "a"])}
    assert predicted[1] == ["a", "b", "c"]

    s = score(
        [Case(issue=1, components=["a", "c"]), Case(issue=2, components=["z"])],
        predicted,
        (1, 3),
    )

    assert s.hit_at == {1: 0.5, 3: 0.5}
    assert s.recall_at == {1: 0.25, 3: 0.5}


def test_assemble_ranks_by_votes_and_lists_co_change_apart() -> None:
    fixed = {1: ["a"], 2: ["a", "b"]}

    result = assemble(
        "acme/app",
        "abc",
        [(1, 0.9), (2, 0.5), (3, 0.4)],  # #3 has no fix: no vote
        fixed,
        lambda c: [("c", 0.5), ("b", 0.5)] if c == "a" else [],
        k=5,
    )

    assert [(t.component, t.via) for t in result.components] == [
        ("a", [1, 2]),
        ("b", [2]),
    ]
    assert [t.component for t in result.blast] == ["c"]  # b was voted for
