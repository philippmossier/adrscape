from pathlib import Path

from cofabriq.eval.loop import Judged
from cofabriq.eval.review import Kind, is_case, review_case
from cofabriq.eval.review import summarize as summarize_review
from cofabriq.eval.witness import witness
from cofabriq.loop.engine import AgentRun
from cofabriq.loop.pr import (
    RiskRules,
    code,
    draft,
    inline,
    load_risk,
    risk_of,
    safe,
    tier_of,
)
from cofabriq.loop.review import Reviewed, approves, review, split_diff
from cofabriq.loop.run import Attempt, CheckResult, LoopResult
from cofabriq.map.components import ComponentRules
from cofabriq.map.components import load_rules as load_components
from tests.gitrepo import git, init_repo
from tests.test_loop import (
    AGENT_RULES,
    REPRO,
    EditingAgent,
    HostSandbox,
    both,
    loop,
    write,
)
from tests.test_refine import spec

PLAYGROUND = "philippmossier/local-ai-chat-playground"

DIFF = """\
diff --git a/src/a.test.sh b/src/a.test.sh
new file mode 100644
+grep -q fixed
diff --git a/src/a.ts b/src/a.ts
-bug
+fixed
"""


def reviewed(**changes: object) -> Reviewed:
    fields: dict[str, object] = {
        "test_matches_spec": True,
        "test_note": "checks the fixed word",
        "fixes_cause": True,
        "cause_note": "replaces the bug",
        "findings": [],
        "verdict": "approve",
        "summary": "Looks right.",
    }
    return Reviewed.model_validate(fields | changes)


class Reviewer:
    """Answers with a fixed review and keeps the prompt."""

    def __init__(self, answer: Reviewed) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        assert schema["additionalProperties"] is False
        self.prompts.append(prompt)
        return AgentRun(
            output=self.answer.model_dump(),
            model="fake",
            cost_usd=0.03,
            turns=4,
            latency_ms=3000,
        )


def green(tmp_path: Path) -> tuple[LoopResult, Path]:
    """A real loop on the tmp repo: the agent's test, then the fix."""
    agent = EditingAgent(
        write("src/a.test.sh", REPRO),
        both(write("src/a.ts", "fixed\n"), write("src/b.ts", "extra\n")),
    )
    result, worktree, _ = loop(tmp_path, agent, planted=False)
    assert result.status == "green"
    return result, worktree


def test_split_diff_puts_the_test_blocks_apart() -> None:
    tests, code_part = split_diff(DIFF, ["src/a.test.sh"])
    assert tests.startswith("diff --git a/src/a.test.sh") and "+fixed" not in tests
    assert code_part.startswith("diff --git a/src/a.ts") and "+fixed" in code_part
    assert split_diff("", ["x"]) == ("", "")


def test_approve_counts_only_with_test_cause_and_no_blocker() -> None:
    assert approves(reviewed())
    assert not approves(reviewed(verdict="request_changes"))
    assert not approves(reviewed(test_matches_spec=False))
    assert not approves(reviewed(fixes_cause=False))
    blocker = {"path": "src/a.ts", "line": 1, "severity": "blocker", "text": "t"}
    assert not approves(reviewed(findings=[blocker]))
    nit = blocker | {"severity": "nit"}
    assert approves(reviewed(findings=[nit]))


def test_reviewer_sees_test_and_code_apart_and_unknown_paths(tmp_path: Path) -> None:
    result, worktree = green(tmp_path)
    finding = {"path": "src/nope.ts", "line": None, "severity": "concern", "text": "t"}
    agent = Reviewer(reviewed(findings=[finding]))
    got = review(
        spec=spec("src/a.ts"),
        rules=AGENT_RULES,
        result=result,
        worktree=worktree,
        agent=agent,
    )
    text = agent.prompts[0]
    tests = text.split("<test_diff>\n")[1].split("\n</test_diff>")[0]
    code_part = text.split("<code_diff>\n")[1].split("\n</code_diff>")[0]
    assert "src/a.test.sh" in tests and "src/a.ts" not in tests
    assert "+fixed" in code_part and "a.test.sh" not in code_part
    assert "try " not in text  # never the implementer's summary
    assert got.approved and got.unknown_paths == ["src/nope.ts"]


def test_risk_is_the_highest_tier_tests_low_unlisted_medium() -> None:
    components = ComponentRules(named={"privacy": ["src/privacy/**"]})
    rules = RiskRules(high=["privacy"], low=["src"])
    assert tier_of("docs", rules) == "medium"  # in no list
    tests = ["src/**/*.test.ts"]
    risk = risk_of(["src/a.ts", "src/a.test.ts"], tests, rules, components)
    assert risk.tier == "low"
    assert [f.tier for f in risk.files] == ["low", "low"]
    risk = risk_of(["src/a.ts", "src/privacy/csp.ts"], tests, rules, components)
    assert risk.tier == "high"
    assert risk.files[1].component == "privacy"
    assert risk_of([], [], rules, components).tier == "low"


def test_playground_tiers_cover_every_named_component() -> None:
    targets = Path("targets")
    risk = load_risk(targets, PLAYGROUND)
    named = load_components(targets, PLAYGROUND).named
    listed = {*risk.high, *risk.medium, *risk.low}
    assert listed == set(named)
    assert tier_of("privacy", risk) == "high" and tier_of("chat", risk) == "low"


def test_safe_neutralises_mentions_refs_html_and_images() -> None:
    text = safe("cc @octocat, see #12 and o/r#3 <img src=x> ![p](http://t/x.png)")
    assert "@octocat" not in text and "@​octocat" in text
    assert "#12" not in text and "o/r#3" not in text
    assert "<img" not in text and "&lt;img" in text
    assert "![" not in text and "](" not in text
    url = safe("https://github.com/o/r/issues/3 or www.x.org")
    assert "://" not in url and "www." not in url


def test_safe_leaves_code_spans_as_written() -> None:
    assert safe("use `a < b` not `<img>`, @x") == "use `a < b` not `<img>`, @\u200bx"
    assert safe("``a ` <b>``") == "``a ` <b>``"
    # an escaped or unclosed backtick is no span: the HTML is escaped
    assert safe("\\`<img>\\`") == "\\`&lt;img&gt;\\`"
    assert safe("`<img>") == "`&lt;img&gt;"


def test_fences_cannot_be_closed_from_inside() -> None:
    fenced = code("a\n```\nb", "diff")
    assert fenced.startswith("````diff\n") and fenced.endswith("\n````")
    assert inline("a`b") == "`` a`b ``" and inline("a") == "`a`"


def test_draft_body_carries_the_evidence(tmp_path: Path) -> None:
    result, worktree = green(tmp_path)
    finding = {
        "path": "src/b.ts",
        "line": 1,
        "severity": "concern",
        "text": "not asked for, ping @octocat",
    }
    agent = Reviewer(reviewed(findings=[finding], summary="Fine but <b>extra</b>."))
    got = review(
        spec=spec("src/a.ts", "src/c.ts"),
        rules=AGENT_RULES,
        result=result,
        worktree=worktree,
        agent=agent,
    )
    pr = draft(
        issue_title="Title says @someone #4",
        spec=spec("src/a.ts", "src/c.ts"),
        result=result,
        tests=AGENT_RULES.tests,
        risk_rules=RiskRules(high=["src"]),
        components=ComponentRules(),
        review=got,
    )
    body = pr.body
    assert pr.title == "Fix #1: Title says @​someone #​4"
    assert body.startswith("Fixes #1.\n")
    assert "**Risk: high** (src)" in body
    assert "**Review: approved.** Fine but &lt;b&gt;extra&lt;/b&gt;." in body
    assert "types, test green after" in body
    assert "| **total** | **$0.23** | |" in body  # two loop runs at 0.1, review 0.03
    # the plan in view, the long problem text collapsed
    assert body.index("### Plan (approved before any code)") < body.index("<details>")
    # the red run, the test diff, then the code diff
    assert body.index("### The failing test") < body.index("+grep -q fixed")
    assert body.index("+grep -q fixed") < body.index("+fixed\n")
    assert "- concern: `src/b.ts:1` not asked for, ping @​octocat" in body
    assert "- Named in the spec, not changed: `src/c.ts`" in body
    assert "- Changed, not in the spec: `src/b.ts`" in body
    assert "| review | $0.030, 3 s | |" in body


def right_and_wrong(
    right: Judged, wrong: Judged
) -> list[tuple[Kind, Judged, Reviewed]]:
    # both approved: the wrong one is a miss
    return [("right", right, reviewed()), ("wrong", wrong, reviewed())]


def test_review_eval_replays_the_diff_and_scores_both_kinds(tmp_path: Path) -> None:
    result, worktree = green(tmp_path)
    clone = tmp_path / "clone"
    right = Judged(
        result=result, judge=CheckResult(name="judge", ok=True, seconds=1, output="")
    )
    wrong = right.model_copy(
        update={"judge": CheckResult(name="judge", ok=False, seconds=1, output="x")}
    )
    assert is_case(right, "right") and not is_case(right, "wrong")
    assert is_case(wrong, "wrong") and not is_case(wrong, "right")
    given = result.model_copy(update={"planted": True})
    planted = right.model_copy(update={"result": given})
    assert not is_case(planted, "right")  # a given repro: no agent test to review

    replayed: list[str] = []

    class Seeing(Reviewer):
        def __call__(
            self, prompt: str, schema: dict[str, object], cwd: Path
        ) -> AgentRun:
            replayed.append((cwd / "src/a.ts").read_text())
            return super().__call__(prompt, schema, cwd)

    cases = [
        review_case(
            key="k",
            kind=kind,
            judged=judged,
            spec=spec("src/a.ts"),
            rules=AGENT_RULES,
            clone=clone,
            worktree=tmp_path / "work" / f"r-{kind}",
            agent=Seeing(answer),
        )
        for kind, judged, answer in right_and_wrong(right, wrong)
    ]
    assert replayed == ["fixed\n", "fixed\n"]  # the run's diff, in a fresh tree
    assert not (tmp_path / "work" / "r-right").exists()
    s = summarize_review(cases)
    assert (s.right, s.wrong, s.passed, s.caught) == (1, 1, 1.0, 0.0)
    assert worktree.exists()  # the loop's own worktree is not touched


def test_witness_proves_a_wrong_fix_the_judge_missed(tmp_path: Path) -> None:
    clone = init_repo(tmp_path / "clone", {"src/a.ts": "bug\n"})
    commit = git(clone, "rev-parse", "HEAD")

    def diff_for(text: str) -> str:
        (clone / "src/a.ts").write_text(text)
        out = git(clone, "diff") + "\n"
        git(clone, "checkout", "-q", "--", ".")
        return out

    patch = tmp_path / "ref.patch"
    patch.write_text(diff_for("fixed\n"))
    wrong = diff_for("fixed-ish\n")  # passes `grep -q fixed`, still wrong
    half = Attempt(n=1, summary="only half", run=None, reverted=[], checks=[])
    result = _loop_result(commit, wrong, [half])
    judged = Judged(
        result=result, judge=CheckResult(name="judge", ok=True, seconds=0, output="")
    )
    exact = 'grep -qx fixed "$(dirname "$0")/a.ts"\n'
    agent = EditingAgent(write("src/w.test.sh", exact))
    got = witness(
        judged=judged,
        spec=spec("src/a.ts"),
        rules=AGENT_RULES,
        clone=clone,
        worktree=tmp_path / "work" / "w",
        agent=agent,
        sandbox=HostSandbox(),
        patch=patch,
    )
    assert got.tests == ["src/w.test.sh"]  # the wrong fix itself not reverted
    assert got.red is not None and not got.red.ok
    assert got.valid is not None and got.valid.ok and got.confirmed
    assert "<broken>\nonly half\n</broken>" in agent.prompts[0]


def _loop_result(commit: str, diff: str, attempts: list[Attempt]) -> LoopResult:
    return LoopResult(
        repo="o/r",
        number=1,
        commit=commit,
        status="green",
        setup_s=0,
        repro=[],
        planted=False,
        reproduce=[],
        red=None,
        attempts=attempts,
        changed=["src/a.ts"],
        diff=diff,
    )
