"""Which component a file belongs to, from its path alone (no code parsing).

Rules live in `[map]` of `targets/<owner>-<name>.toml`, checked in order:

- `named`: a component name and its globs (`**` for any depth), for repos
  whose components are written by hand (the playground's triage list).
- `patterns`: path patterns where the last segment names the component:
  `src/vs/workbench/contrib/*` puts `.../contrib/terminal/browser/x.ts` in
  `src/vs/workbench/contrib/terminal`. A pattern ending in a literal names a
  single component (`src/vs/workbench/api`). A pattern only matches a
  folder: `extensions/*` does not turn `extensions/package.json` into a
  component.
- Otherwise the file's top-level folder (`build`, `test`, `.github`), or
  `(root)` for files at the top.
"""

import tomllib
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

ROOT = "(root)"


class ComponentRules(BaseModel):
    named: dict[str, list[str]] = {}
    patterns: list[str] = []


def load_rules(targets_dir: Path, repo: str) -> ComponentRules:
    path = targets_dir / f"{repo.replace('/', '-')}.toml"
    if not path.is_file():
        return ComponentRules()
    table = tomllib.loads(path.read_text("utf-8")).get("map", {})
    return ComponentRules.model_validate(table)


def component_of(path: str, rules: ComponentRules) -> str:
    for name, globs in rules.named.items():
        if any(PurePosixPath(path).full_match(g) for g in globs):
            return name
    parts = path.split("/")
    for pattern in rules.patterns:
        segments = pattern.split("/")
        # `<` and not `<=`: the pattern must name a folder above the file
        if len(segments) < len(parts) and all(
            fnmatchcase(p, s) for p, s in zip(parts, segments, strict=False)
        ):
            return "/".join(parts[: len(segments)])
    return parts[0] if len(parts) > 1 else ROOT
