from adrscape.ingest.chunk import chunk_doc
from adrscape.ingest.parse import ManifestParser, MarkdownParser, ParsedDoc, Section
from adrscape.ingest.source import DocKind, DocRef

ADR = """# 1. Use SSE

Date: 2026-10-05

## Status

Accepted

## Context

Browsers need streamed tokens.

## Decision

```python
# not a heading
```
"""


def ref(path: str, kind: DocKind) -> DocRef:
    return DocRef(repo="repo-a", path=path, sha="9f1c0aa", kind=kind)


def test_markdown_sections_follow_the_heading_trail() -> None:
    doc = MarkdownParser().parse(ref("docs/adr/0001-x.md", DocKind.ADR), ADR)

    assert [s.path for s in doc.sections] == [
        ("1. Use SSE",),
        ("1. Use SSE", "Status"),
        ("1. Use SSE", "Context"),
        ("1. Use SSE", "Decision"),
    ]
    assert "# not a heading" in doc.sections[-1].text
    assert doc.title == "1. Use SSE"
    assert doc.meta == {"date": "2026-10-05", "status": "Accepted"}


def test_an_adr_stays_one_chunk_with_identity_and_meta() -> None:
    doc = MarkdownParser().parse(ref("docs/adr/0001-x.md", DocKind.ADR), ADR)

    [chunk] = chunk_doc(doc)

    assert (chunk.repo, chunk.path, chunk.sha, chunk.ord) == (
        "repo-a",
        "docs/adr/0001-x.md",
        "9f1c0aa",
        0,
    )
    assert chunk.heading == "1. Use SSE"
    assert "## Context" in chunk.text and "## Decision" in chunk.text
    assert chunk.meta["status"] == "Accepted"


def test_non_adr_markdown_has_no_adr_meta() -> None:
    doc = MarkdownParser().parse(ref("README.md", DocKind.README), ADR)
    assert doc.meta == {}


def test_small_sections_merge_and_big_ones_split_at_paragraphs() -> None:
    para = "word " * 19  # 94 chars once stripped, two fit in 200
    doc = ParsedDoc(
        ref=ref("README.md", DocKind.README),
        title="Repo A",
        sections=(
            Section(("Repo A",), "intro"),
            Section(("Repo A", "Setup"), "setup"),
            Section(("Repo A", "Arch"), "\n\n".join([para] * 4)),
        ),
    )

    chunks = chunk_doc(doc, max_chars=200)

    assert [c.heading for c in chunks] == [
        "Repo A",
        "Repo A > Arch",
        "Repo A > Arch",
    ]
    assert chunks[0].text == "intro\n\nsetup"
    assert all(len(c.text) <= 200 for c in chunks)
    assert [c.ord for c in chunks] == [0, 1, 2]


def test_one_oversized_paragraph_is_cut_hard() -> None:
    doc = ParsedDoc(
        ref=ref("docs/x.md", DocKind.DOC),
        title="x",
        sections=(Section(("x",), "a" * 450),),
    )
    assert [len(c.text) for c in chunk_doc(doc, max_chars=200)] == [200, 200, 50]


def test_package_json_renders_name_and_dependencies() -> None:
    content = '{"name": "web", "description": "UI", "dependencies": {"react": "^19"},'
    content += ' "devDependencies": {"vitest": "^3"}, "scripts": {"dev": "vite"}}'

    doc = ManifestParser().parse(ref("package.json", DocKind.MANIFEST), content)

    expected = "name: web\ndescription: UI\ndependencies: react, vitest"
    assert doc.sections[0].text == expected
    assert doc.meta == {"name": "web"}
    assert doc.title == "package.json (web)"


def test_pyproject_strips_version_specifiers() -> None:
    content = '[project]\nname = "api"\n'
    content += 'dependencies = ["uvicorn[standard]>=0.54", "fastapi"]\n'
    manifest = ref("backend/pyproject.toml", DocKind.MANIFEST)

    doc = ManifestParser().parse(manifest, content)

    assert "dependencies: uvicorn, fastapi" in doc.sections[0].text


def test_malformed_manifest_falls_back_to_raw_text() -> None:
    doc = ManifestParser().parse(ref("package.json", DocKind.MANIFEST), "{broken")
    assert doc.sections[0].text == "{broken"
    assert doc.meta == {}
