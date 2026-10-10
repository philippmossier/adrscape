import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from adrscape.domain import AdrStatus, Extraction, Link
from adrscape.ingest.extract import VERSION, Candidate, check, load, sync
from adrscape.ingest.parse import MarkdownParser, ParsedDoc
from adrscape.ingest.source import DocKind, DocRef

FANOUT_TS = """# 3. Fan-out in the browser

Date: 2026-10-05

## Status

Accepted

## Decision

The browser sends one request per field.
"""

FANOUT_PY = """# 2. Fan-out on the server

Date: 2026-10-06

## Status

Accepted. Reviewed later.

## Context

In the TypeScript playground the browser orchestrates the fan-out.
Its ADR says so and
points at a server-side variant.
"""

TS_ID = "repo-ts:docs/adr/0003-fan-out.md"
PY_ID = "repo-py:docs/adr/0002-fan-out.md"


def parse(repo: str, path: str, content: str, sha: str = "aaa") -> ParsedDoc:
    kind = DocKind.ADR if "/adr/" in path else DocKind.README
    return MarkdownParser().parse(DocRef(repo, path, sha, kind), content)


def ts_adr(sha: str = "aaa") -> ParsedDoc:
    return parse("repo-ts", "docs/adr/0003-fan-out.md", FANOUT_TS, sha)


def py_adr(sha: str = "bbb") -> ParsedDoc:
    return parse("repo-py", "docs/adr/0002-fan-out.md", FANOUT_PY, sha)


def extraction(**fields: object) -> Extraction:
    base: dict[str, object] = {
        "status": AdrStatus.ACCEPTED,
        "technologies": [],
        "teams": [],
        "revises": [],
        "revisits": [],
        "depends_on": [],
        "affects": [],
    }
    return Extraction.model_validate(base | fields)


class FakeExtractor:
    """Returns the same extraction for every document; records what it saw."""

    def __init__(self, result: Extraction | None = None) -> None:
        self.result = result or extraction()
        self.seen: list[str] = []
        self.offered: list[list[str]] = []

    def extract(
        self, doc: ParsedDoc, candidates: Sequence[Candidate], projects: Sequence[str]
    ) -> Extraction:
        self.seen.append(doc.ref.path)
        self.offered.append([c.id for c in candidates])
        return self.result


def similar_to_everything(ref: DocRef) -> list[tuple[str, str]]:
    """Every fixture ADR except the document itself, like the vector store."""
    adrs = [tuple(i.split(":")) for i in (TS_ID, PY_ID)]
    return [(repo, path) for repo, path in adrs if (repo, path) != (ref.repo, ref.path)]


CANDIDATE = Candidate(id=TS_ID, title="3. Fan-out in the browser", date="2026-10-05")
PROJECTS = ["repo-py", "repo-ts"]


def test_a_quoted_link_to_an_offered_older_adr_is_kept() -> None:
    # The quote spans a line break in the file: matching ignores whitespace.
    quote = "Its ADR says so and points at a server-side variant."
    link = Link(target=TS_ID, evidence=quote)

    record = check(py_adr(), extraction(revisits=[link]), [CANDIDATE], PROJECTS)

    assert record.extraction.revisits == [link]
    assert record.dropped == []


def test_links_to_unknown_targets_or_with_invented_quotes_are_dropped() -> None:
    made_up = Link(target="repo-x:docs/adr/9.md", evidence="Its ADR says so")
    paraphrase = Link(target=TS_ID, evidence="It replaces the browser fan-out.")
    own_repo = Link(target="repo-py", evidence="Its ADR says so")

    record = check(
        py_adr(),
        extraction(
            revises=[made_up],
            revisits=[paraphrase],
            depends_on=[own_repo],
            affects=["repo-py", "nowhere"],
        ),
        [CANDIDATE],
        PROJECTS,
    )

    e = record.extraction
    assert (e.revises, e.revisits, e.depends_on) == ([], [], [])
    assert e.affects == ["repo-py"]
    assert record.dropped == [
        "affects -> nowhere: not a project",
        "revises -> repo-x:docs/adr/9.md: not on the list",
        f"revisits -> {TS_ID}: evidence not in document",
        "depends_on -> repo-py: not on the list",
    ]


def test_an_adr_cannot_revise_a_newer_one() -> None:
    newer = Candidate(id=PY_ID, title="2. Fan-out on the server", date="2026-10-06")
    link = Link(target=newer.id, evidence="The browser sends one request per field.")

    record = check(ts_adr(), extraction(revisits=[link]), [newer], PROJECTS)

    assert record.extraction.revisits == []
    assert record.dropped == [f"revisits -> {newer.id}: newer than this ADR"]


def test_non_adrs_get_no_status_and_names_are_deduplicated() -> None:
    readme = parse("repo-ts", "README.md", "# TS\n\nUses SSE.\n")

    record = check(
        readme,
        extraction(technologies=["SSE", " SSE ", "React"]),
        [],
        PROJECTS,
    )

    assert record.extraction.status is None
    assert record.extraction.technologies == ["SSE", "React"]


def test_sync_calls_the_llm_only_for_new_documents(tmp_path: Path) -> None:
    out = tmp_path / "extracted.json"
    first, second = FakeExtractor(), FakeExtractor()

    r1 = sync([ts_adr(), py_adr()], out, first, similar_to_everything)
    r2 = sync([ts_adr(), py_adr()], out, second, similar_to_everything)

    assert (r1.extracted, r1.unchanged) == (2, 0)
    assert (r2.extracted, r2.unchanged) == (0, 2)
    assert second.seen == []
    assert first.offered == [[PY_ID], [TS_ID]]


def test_sync_replaces_changed_and_drops_missing(tmp_path: Path) -> None:
    out = tmp_path / "extracted.json"
    sync([ts_adr(), py_adr()], out, FakeExtractor(), similar_to_everything)

    # The TS ADR was edited (new sha), the Py ADR was deleted.
    fake = FakeExtractor()
    report = sync([ts_adr(sha="ccc")], out, fake, similar_to_everything)

    assert [(r.ref.path, r.ref.sha) for r in load(out)] == [
        ("docs/adr/0003-fan-out.md", "ccc")
    ]
    assert (report.extracted, report.removed) == (1, 2)


def test_records_of_another_version_are_re_extracted(tmp_path: Path) -> None:
    out = tmp_path / "extracted.json"
    sync([ts_adr()], out, FakeExtractor(), similar_to_everything)
    # An older schema: a field is missing, so it would not even validate.
    stale = json.loads(out.read_text())
    stale[0]["version"] = "old-model/0"
    del stale[0]["extraction"]["revisits"]
    out.write_text(json.dumps(stale))

    fake = FakeExtractor()
    sync([ts_adr()], out, fake, similar_to_everything)

    assert fake.seen == ["docs/adr/0003-fan-out.md"]
    assert [r.version for r in load(out)] == [VERSION]


def test_a_failed_call_keeps_what_was_extracted_before_it(tmp_path: Path) -> None:
    out = tmp_path / "extracted.json"

    class FailsOnPy(FakeExtractor):
        def extract(
            self,
            doc: ParsedDoc,
            candidates: Sequence[Candidate],
            projects: Sequence[str],
        ) -> Extraction:
            if doc.ref.repo == "repo-py":
                raise RuntimeError("API down")
            return super().extract(doc, candidates, projects)

    with pytest.raises(RuntimeError):
        sync([ts_adr(), py_adr()], out, FailsOnPy(), similar_to_everything)

    assert [r.ref.repo for r in load(out)] == ["repo-ts"]


def test_an_adr_that_was_not_embedded_stops_the_run(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="run `embed` first"):
        sync([ts_adr()], tmp_path / "x.json", FakeExtractor(), lambda ref: None)
