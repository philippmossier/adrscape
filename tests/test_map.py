import os
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from neo4j import Driver

from cofabriq.map import store
from cofabriq.map.build import MAX_FILES, build
from cofabriq.map.components import ROOT, ComponentRules, component_of, load_rules
from cofabriq.map.fixes import Fix
from cofabriq.map.history import Commit, commits, files_at, rev_at
from tests.gitrepo import init_repo

AT = datetime(2022, 1, 1, tzinfo=UTC)
RULES = ComponentRules(
    named={"models": ["src/lib/models/**", "src/components/model-picker.tsx"]},
    patterns=["src/vs/contrib/*", "src/vs/api", "extensions/*"],
)


@pytest.mark.parametrize(
    ("path", "component"),
    [
        ("src/lib/models/cache/store.ts", "models"),  # named, any depth
        ("src/components/model-picker.tsx", "models"),
        ("src/vs/contrib/terminal/browser/x.ts", "src/vs/contrib/terminal"),
        ("src/vs/api/common/y.ts", "src/vs/api"),  # literal pattern
        ("extensions/git/package.json", "extensions/git"),
        ("extensions/package.json", "extensions"),  # a pattern names folders only
        ("build/gulpfile.js", "build"),  # fallback: top-level folder
        ("package.json", ROOT),
    ],
)
def test_component_of(path: str, component: str) -> None:
    assert component_of(path, RULES) == component


def test_load_rules(tmp_path: Path) -> None:
    (tmp_path / "acme-app.toml").write_text(
        '[map]\npatterns = ["src/*"]\n[map.named]\nui = ["web/**"]\n'
    )

    assert load_rules(tmp_path, "acme/app") == ComponentRules(
        named={"ui": ["web/**"]}, patterns=["src/*"]
    )
    assert load_rules(tmp_path, "acme/none") == ComponentRules()


def dated_commit(
    repo: Path, day: str, files: dict[str, str], message: str = "change"
) -> None:
    """A commit with author and committer date on `day` (`--before` reads the
    committer date)."""
    for path, content in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
    stamp = f"{day}T12:00:00+00:00"
    env = os.environ | {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            message,
        ],
        check=True,
        env=env,
    )


def test_history_as_of_a_date(tmp_path: Path) -> None:
    repo = init_repo(tmp_path, {"a.txt": "1"})
    dated_commit(repo, "2022-06-01", {"a.txt": "2", "b/c.txt": "1"})
    dated_commit(repo, "2023-02-01", {"d.txt": "1"})

    rev = rev_at(repo, date(2022, 12, 31))
    history = list(commits(repo, rev))

    assert sorted(files_at(repo, rev)) == ["a.txt", "b/c.txt"]
    assert history[0].files == ("a.txt", "b/c.txt")
    assert history[0].at.year == 2022
    assert rev_at(repo, None) != rev  # head includes the 2023 commit


def test_build_counts_co_change_on_present_files() -> None:
    files = ["src/vs/contrib/a/x.ts", "src/vs/contrib/a/y.ts", "src/vs/contrib/b/z.ts"]
    big = (*(f"gen/{n}.ts" for n in range(MAX_FILES)), files[0], files[2])
    history = [
        Commit("1", AT, (files[0], files[2], "deleted/old.ts")),
        Commit("2", AT, (files[0], files[1])),
        Commit("3", AT, (files[0], files[1])),
        Commit("4", AT, (files[0], files[1])),
        Commit("5", AT, ("deleted/old.ts",)),  # nothing present: not counted
        Commit("6", AT, big),  # gen/ files are not present: 2 left, counted
    ]

    m = build(files, history, RULES)

    assert m.files[files[2]] == "src/vs/contrib/b"
    assert m.commits == {"src/vs/contrib/a": 5, "src/vs/contrib/b": 2}
    assert m.components_together == {("src/vs/contrib/a", "src/vs/contrib/b"): 2}
    assert m.files_together == [(files[0], files[1], 3)]  # 2 shared: below MIN_PAIR
    assert (m.used, m.skipped) == (5, 0)


def test_build_skips_commits_over_the_file_cap() -> None:
    files = [f"src/{n}.ts" for n in range(MAX_FILES + 1)]

    m = build(files, [Commit("1", AT, tuple(files))], RULES)

    assert (m.used, m.skipped) == (0, 1)
    assert not m.components_together


def test_store_writes_and_replaces_a_map(driver: Driver) -> None:
    files = ["src/vs/contrib/a/x.ts", "src/vs/contrib/b/z.ts", "build/g.js"]
    history = [Commit(str(n), AT, (files[0], files[1])) for n in range(3)]
    m = build(files, history, RULES)

    report = store.write(driver, "acme/app", "abc", date(2022, 12, 31), m)
    again = store.write(driver, "acme/app", "abc", date(2022, 12, 31), m)

    assert report == again
    assert (report.components, report.files, report.component_pairs) == (3, 3, 1)
    assert [c.name for c in store.components(driver, "acme/app")] == [
        "src/vs/contrib/a",
        "src/vs/contrib/b",
        "build",
    ]
    [partner] = store.changes_with(driver, "acme/app", "src/vs/contrib/b", 5)
    assert (partner.name, partner.n, partner.share) == ("src/vs/contrib/a", 3, 1.0)
    records, _, _ = driver.execute_query("MATCH (f:File) RETURN count(f) AS n")
    assert records[0]["n"] == 3  # replaced, not doubled


def test_popular_and_partners_in_memory() -> None:
    files = ["src/vs/contrib/a/x.ts", "src/vs/contrib/b/z.ts", "build/g.js"]
    history = [
        Commit("1", AT, (files[0], files[1])),
        Commit("2", AT, (files[0], files[2])),
        Commit("3", AT, (files[0],)),
    ]

    m = build(files, history, RULES)

    assert m.popular() == ["src/vs/contrib/a", "build", "src/vs/contrib/b"]
    assert m.partners("src/vs/contrib/a", 5) == [
        ("build", 1 / 3),
        ("src/vs/contrib/b", 1 / 3),
    ]
    assert m.partners("unknown", 5) == []


def test_store_keeps_fixes_and_answers_by_date(driver: Driver) -> None:
    files = ["src/vs/contrib/a/x.ts", "src/vs/contrib/b/z.ts"]
    m = build(files, [], RULES)
    early = datetime(2022, 3, 1, tzinfo=UTC)
    late = datetime(2022, 9, 1, tzinfo=UTC)
    fixed = {
        7: [Fix("s1", early, (files[0], "deleted/old.ts"))],
        8: [Fix("s1", early, (files[0],)), Fix("s2", late, (files[1],))],
    }
    rows = store.fix_rows(fixed, RULES, set(m.files.values()))

    report = store.write(driver, "acme/app", "abc", None, m, rows)

    assert report.fixes == 2  # s1 fixes both issues: one row
    assert rows[0].issues == [7, 8]
    assert store.fixed(driver, "acme/app", [7, 8, 9], None) == {
        7: ["src/vs/contrib/a"],
        8: ["src/vs/contrib/a", "src/vs/contrib/b"],
    }
    june = datetime(2022, 6, 1, tzinfo=UTC)
    assert store.fixed(driver, "acme/app", [8], june) == {8: ["src/vs/contrib/a"]}
    assert store.fixed_issues(driver, "acme/app", june) == [7, 8]
    assert store.rev(driver, "acme/app") == "abc"
    assert store.cut(driver, "acme/app") is None
