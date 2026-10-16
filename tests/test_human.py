from cofabriq.eval.human import Entry, Record, pick, summarize


def test_pick_one_pr_per_bug_with_three_wrong_swapped_in() -> None:
    right = [f"b{i}" for i in range(10)]
    wrong = ["b1", "b2", "b3", "b4", "x"]  # x has no right fix: never picked
    picks = pick(right, wrong, seed=1)
    assert sorted(p.key for p in picks) == sorted(right)  # one per bug
    swapped = [p.key for p in picks if p.kind == "wrong"]
    assert len(swapped) == 3 and set(swapped) <= {"b1", "b2", "b3", "b4"}
    assert pick(right, wrong, seed=1) == picks  # the seed fixes the set
    assert [p.key for p in picks] != sorted(right)  # shuffled


def entry(n: int, kind: str, approved: bool) -> Entry:
    return Entry.model_validate(
        {
            "key": f"b{n}",
            "kind": kind,
            "number": n,
            "pr": 100 + n,
            "url": f"u{n}",
            "branch": f"cofabriq/{n}",
            "approved": approved,
        }
    )


def test_minutes_per_merged_pr_counts_every_review() -> None:
    entries = [
        entry(1, "right", True),
        entry(2, "right", True),
        entry(3, "wrong", False),
    ]
    records = [
        Record(url="u1", seconds=60, decision="merge"),
        Record(url="u2", seconds=180, decision="changes"),  # a right one held back
        Record(url="u3", seconds=120, decision="close"),  # the wrong one caught
    ]
    t = summarize(entries, records)
    assert (t.n, t.merged, t.wrong, t.caught, t.right_merged) == (3, 1, 1, 1, 1)
    assert t.minutes == 2.0  # median of 1, 3, 2
    assert t.per_merge == 6.0  # 6 minutes of review for one merge
    assert round(t.agreed, 2) == 0.67  # u2: reviewer approved, dev did not
    assert summarize(entries, []).n == 0
