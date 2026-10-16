"""The planted bugs in the playground: the answer key for refine and the loop.

`playground/bugs.toml` says per bug which files the fix changes and which
test reproduces it; the issue a user would file is the seed entry with the
same key. See the header of bugs.toml for how the bugs were planted.
"""

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter

from cofabriq.eval.triage import load_seed


class PlantedBug(BaseModel):
    key: str
    kind: Literal["logic", "ui"]
    size: Literal["S", "M", "L"]
    files: list[str]
    repro: str  # file name in bugs/repro/


_bugs_adapter = TypeAdapter(list[PlantedBug])


def load_bugs(path: Path, seed: Path) -> list[PlantedBug]:
    """The `[[bug]]` list, checked against the seed issues and the repro
    and fix files next to it."""
    bugs = _bugs_adapter.validate_python(tomllib.loads(path.read_text("utf-8"))["bug"])
    keys = [b.key for b in bugs]
    if len(set(keys)) != len(keys):
        raise ValueError(f"{path}: keys must be unique")
    issues = {s.key: s for s in load_seed(seed)}
    folder = path.parent / "bugs"
    for b in bugs:
        if b.key not in issues or issues[b.key].type != "bug":
            raise ValueError(f"{b.key}: no seed issue of type bug with this key")
        if not (folder / "repro" / b.repro).is_file():
            raise ValueError(f"{b.key}: repro {b.repro} is missing")
        if not (folder / "fixes" / f"{b.key}.patch").is_file():
            raise ValueError(f"{b.key}: fix patch is missing")
        suffix = ".test.ts" if b.kind == "logic" else ".spec.ts"
        if not b.repro.endswith(suffix):
            raise ValueError(f"{b.key}: a {b.kind} repro ends in {suffix}")
    return bugs


def planted_for(title: str, bugs: list[PlantedBug], seed: Path) -> PlantedBug | None:
    """The planted bug whose seed issue has this title (seed keys carry no
    GitHub numbers, see `triage.match`)."""
    keys = {s.title: s.key for s in load_seed(seed)}
    return next((b for b in bugs if b.key == keys.get(title)), None)


def repro_path(bugs_file: Path, bug: PlantedBug) -> Path:
    return bugs_file.parent / "bugs" / "repro" / bug.repro
