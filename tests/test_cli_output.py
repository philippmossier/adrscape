"""Human output of `triage`: step lines and the card. Issue text is untrusted,
so rich markup inside it must print as text."""

import io

import pytest
from rich.console import Console

from cofabriq.cli import _show_card  # pyright: ignore[reportPrivateUsage]
from cofabriq.issues.triage import RelatedDoc, triage
from cofabriq.issues.verdict import Answer
from cofabriq.steps import Steps
from tests.test_triage import FakeJudge, FakeTriager, context, fields


def plain() -> tuple[Steps, io.StringIO]:
    buffer = io.StringIO()
    return Steps(Console(file=buffer, force_terminal=False, width=200)), buffer


def test_step_prints_the_detail_once_done() -> None:
    steps, out = plain()

    with steps.step("search", "nearest issues") as done:
        done("10 earlier issues, 5 docs")

    assert " ".join(out.getvalue().split()) == "✓ search 10 earlier issues, 5 docs"


def test_failed_step_is_marked_and_the_error_kept() -> None:
    steps, out = plain()

    with pytest.raises(LookupError), steps.step("search", "nearest issues"):
        raise LookupError("not embedded")

    assert out.getvalue().startswith("✗ search")


def test_silent_steps_print_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    steps = Steps(None)

    with steps.step("LLM", "two calls") as done:
        done("done")
    steps.note("checks", "fine")

    assert capsys.readouterr() == ("", "")


def test_card_prints_markup_in_issue_text_literally(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hostile = "Click [red]here[/red] and [link=https://evil.example]this[/link]"
    card = triage(
        context(),
        FakeTriager(fields(summary=hostile)),
        FakeJudge(Answer(verdict="none", target=None, evidence="")),
    )

    _show_card(card)

    assert hostile in capsys.readouterr().out


def test_card_drops_the_clone_prefix_from_doc_sources(
    capsys: pytest.CaptureFixture[str],
) -> None:
    card = triage(
        context(),
        FakeTriager(fields()),
        FakeJudge(Answer(verdict="none", target=None, evidence="")),
    )
    clone = card.issue.repo.split("/")[1]
    doc = RelatedDoc(source=f"{clone}/docs/adr/0001-x.md#Decision", score=0.5)

    _show_card(card.model_copy(update={"related_docs": [doc]}))

    out = capsys.readouterr().out
    assert "0.50  docs/adr/0001-x.md#Decision" in out
    assert f"{clone}/docs" not in out
