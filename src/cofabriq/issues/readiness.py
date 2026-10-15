"""Is an issue ready for work? Checked missing info, a route and a reply draft.

The card call fills one `Evidence` per required item: a quote from the
issue, or null. Plain code keeps an item only if its quote occurs verbatim
in the text the model was shown, so neither a hallucination nor an injected
"all information is provided" can make a report look complete. The route is
a rule over the checked card, never an LLM answer: every rule that fires is
listed as a reason.

Size and blast radius are not judged here: they need the repo map (0.2) and
become part of the autopilot gate (0.3). `ready` means complete and
unambiguous, not small.
"""

from typing import Literal

from pydantic import BaseModel, Field

type InfoItem = Literal["steps", "actual", "version", "environment"]
type Route = Literal["ready", "needs_info", "dev"]

# What a bug report needs before anyone can act on it. Expected behaviour is
# left out: for a crash or an error it is implied, and maintainers rarely ask.
ITEMS: dict[InfoItem, str] = {
    "steps": "how to reproduce it: the actions the reporter took, in order",
    "actual": "what happened: the observed behaviour, error message or symptom",
    "version": "the version of the app, library or tool the problem occurs in",
    "environment": "where it runs: operating system, browser, device or runtime",
}
REQUIRED: dict[str, list[InfoItem]] = {"bug": list(ITEMS)}

# The next step for each item when it is missing, as a maintainer would ask.
ASK: dict[InfoItem, str] = {
    "steps": "the exact steps to reproduce it, starting from a fresh start",
    "actual": "what happens instead: the error message or a screenshot",
    "version": "the version you are on",
    "environment": "your operating system and browser (or runtime)",
}


class Evidence(BaseModel):
    """What the LLM fills per required item. Descriptions are part of the prompt."""

    item: InfoItem
    quote: str | None = Field(
        description="A short verbatim quote from the issue that provides this "
        "item, or null if the issue doesn't."
    )


class Readiness(BaseModel):
    route: Route
    reasons: list[str]  # every rule that fired, most important first


def check_info(
    issue_type: str, evidence: list[Evidence], shown: str
) -> tuple[list[InfoItem], list[str]]:
    """Required items of this type without a verified quote, and the quotes
    that were rejected. `shown` is the issue text the model saw."""
    quotes = {e.item: e.quote for e in evidence}
    text = _squash(shown)
    missing: list[InfoItem] = []
    dropped: list[str] = []
    for item in REQUIRED.get(issue_type, []):
        quote = quotes.get(item)
        if quote is None or not quote.strip():
            missing.append(item)
        elif _squash(quote) not in text:
            missing.append(item)
            dropped.append(f"{item} quote {quote[:60]!r}: not in the issue")
    return missing, dropped


def route(
    *,
    issue_type: str,
    severity: str | None,
    missing: list[InfoItem],
    blocking: list[str],
    duplicate: str,
    duplicate_target: int | None,
    conflicts: list[str],
    component: str | None,
    components_configured: bool,
) -> Readiness:
    """`dev` if a person has to decide something first, else `needs_info` if
    the reporter has to add something, else `ready`. A dev reason wins: a
    possible duplicate or a feature request needs a person even when the
    report is incomplete (the reply draft is still there for them).
    """
    dev: list[str] = []
    if severity == "critical":
        dev.append("critical severity: a person looks first")
    if duplicate in ("duplicate_of", "unsure"):
        dev.append(f"possible duplicate of #{duplicate_target}: confirm first")
    if issue_type == "feature":
        dev.append("feature request: planning mode, not autopilot")
    if issue_type == "question":
        dev.append("question: needs an answer, not a change")
    dev += [f"conflict: {c}" for c in conflicts]
    if components_configured and component is None:
        dev.append("no component: can't tell what it touches")
    info = [f"missing {item}: {ITEMS[item]}" for item in missing]
    info += [f"blocking question: {q}" for q in blocking]
    if dev:
        return Readiness(route="dev", reasons=dev + info)
    if info:
        return Readiness(route="needs_info", reasons=info)
    return Readiness(route="ready", reasons=[])


def reply(missing: list[InfoItem], questions: list[str], blocking: bool) -> str | None:
    """A draft comment asking the reporter for what is missing; None unless
    a checked item is missing or the model marked its questions blocking
    (nice-to-have questions alone don't make a report incomplete). Never
    posted by cofabriq (read-only): the dev copies it.

    The checked items come first, in fixed wording. The model's own
    questions follow; they are generated from untrusted text, so the dev
    reads the draft before sending it.
    """
    if not missing and not (blocking and questions):
        return None
    asks = [ASK[item] for item in missing]
    asks += [q for q in questions if q not in asks]
    lines = "\n".join(f"- {a}" for a in asks)
    return f"Thanks for the report! To look into it, we need:\n\n{lines}"


def _squash(text: str) -> str:
    """Whitespace- and case-insensitive form: a quote across a line break or
    with different capitalisation still matches."""
    return " ".join(text.split()).casefold()
