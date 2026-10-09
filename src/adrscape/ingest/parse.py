"""Parse markdown and manifests into sections plus metadata: the `Parser` seam."""

import re
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import BaseModel

from adrscape.ingest.fetch import load_index, raw_path
from adrscape.ingest.source import DocKind, DocRef


@dataclass(frozen=True)
class Section:
    """A run of text under one heading. `path` is the heading trail from the top."""

    path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class ParsedDoc:
    """One document, format-independent: what the chunker and later steps see."""

    ref: DocRef
    title: str
    sections: tuple[Section, ...]
    meta: Mapping[str, str] = field(default_factory=dict[str, str])


class Parser(Protocol):
    """Turns one file's content into sections. One implementation per format."""

    def parse(self, ref: DocRef, content: str) -> ParsedDoc: ...


HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
ADR_DATE = re.compile(r"^date:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


class MarkdownParser:
    """Splits at ATX headings (`#` .. `######`), ignoring `#` inside code fences.

    ADRs get `status` (first line under `## Status`) and `date` (`Date:` line)
    as metadata when present; a missing field is left out, not guessed.
    """

    def parse(self, ref: DocRef, content: str) -> ParsedDoc:
        sections = _split_markdown(content)
        title = next((s.path[0] for s in sections if s.path), _file_title(ref))
        meta = _adr_meta(content, sections) if ref.kind is DocKind.ADR else {}
        return ParsedDoc(ref=ref, title=title, sections=tuple(sections), meta=meta)


class ManifestParser:
    """Renders a manifest as plain text: name, description, dependencies.

    The raw file is mostly noise for search (scripts, build config); the
    rendered summary is what a question like "who uses fastapi" needs.
    Unknown or malformed manifests fall back to the raw text.
    """

    def parse(self, ref: DocRef, content: str) -> ParsedDoc:
        name = PurePosixPath(ref.path).name
        try:
            fields = _manifest_fields(name, content)
        except ValueError:  # TOMLDecodeError and ValidationError subclass it
            fields = None
        if fields is None:
            text, meta = content, {}
        else:
            text = "\n".join(f"{k}: {v}" for k, v in fields.items() if v)
            meta = {"name": fields["name"]} if fields["name"] else {}
        title = f"{name} ({meta['name']})" if meta else name
        section = Section(path=(title,), text=text)
        return ParsedDoc(ref=ref, title=title, sections=(section,), meta=meta)


_markdown = MarkdownParser()
_manifest = ManifestParser()


def parser_for(ref: DocRef) -> Parser:
    return _manifest if ref.kind is DocKind.MANIFEST else _markdown


def load_docs(raw_dir: Path) -> Iterator[ParsedDoc]:
    """Parse every document listed in `raw_dir/index.json`."""
    root = raw_dir.resolve()
    for ref in load_index(root):
        content = raw_path(root, ref).read_text(encoding="utf-8")
        yield parser_for(ref).parse(ref, content)


def _split_markdown(content: str) -> list[Section]:
    sections: list[Section] = []
    trail: list[tuple[int, str]] = []  # (level, heading) from the top down
    lines: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(lines).strip()
        if text:
            sections.append(Section(path=tuple(h for _, h in trail), text=text))
        lines.clear()

    for line in content.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        heading = None if in_fence else HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            while trail and trail[-1][0] >= level:
                trail.pop()
            trail.append((level, heading.group(2)))
        lines.append(line)
    flush()
    return sections


def _adr_meta(content: str, sections: list[Section]) -> dict[str, str]:
    meta: dict[str, str] = {}
    if date := ADR_DATE.search(content):
        meta["date"] = date.group(1)
    for s in sections:
        if s.path and s.path[-1].lower() == "status":
            body = s.text.splitlines()[1:]  # drop the heading line
            status = next((ln.strip() for ln in body if ln.strip()), "")
            if status:
                meta["status"] = status
            break
    return meta


class _PackageJson(BaseModel):
    name: str = ""
    description: str = ""
    dependencies: dict[str, str] = {}
    devDependencies: dict[str, str] = {}


class _Project(BaseModel):
    name: str = ""
    description: str = ""
    dependencies: list[str] = []


class _Pyproject(BaseModel):
    project: _Project = _Project()


def _manifest_fields(name: str, content: str) -> dict[str, str] | None:
    """Only the fields search needs; other keys are ignored, wrong types raise."""
    if name == "package.json":
        pkg = _PackageJson.model_validate_json(content)
        deps = [*pkg.dependencies, *pkg.devDependencies]
        name_, description = pkg.name, pkg.description
    elif name == "pyproject.toml":
        project = _Pyproject.model_validate(tomllib.loads(content)).project
        deps = [_pep508_name(d) for d in project.dependencies]
        name_, description = project.name, project.description
    else:
        return None  # compose files etc.: raw text until a corpus needs more
    return {"name": name_, "description": description, "dependencies": ", ".join(deps)}


def _pep508_name(spec: str) -> str:
    """`uvicorn[standard]>=0.54.0` -> `uvicorn`."""
    return re.split(r"[\s\[<>=!~;]", spec, maxsplit=1)[0]


def _file_title(ref: DocRef) -> str:
    return PurePosixPath(ref.path).name
