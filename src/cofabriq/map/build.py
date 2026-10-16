"""The repo map as numbers: components, their files, and what changes together.

Co-change is counted per commit: two files (or components) changed in the
same commit count once. Commits touching more than MAX_FILES files are
skipped, the usual rule in co-change mining: a mass rename or reformat
would link everything with everything. Only files present at the cut count;
history of deleted files would link to nothing a change can touch today.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import combinations

from cofabriq.map.components import ComponentRules, component_of
from cofabriq.map.history import Commit

MAX_FILES = 50
FILE_PARTNERS = 5  # per file, the strongest co-change partners kept
MIN_PAIR = 3  # commits two files must share to count as co-changing


@dataclass
class RepoMap:
    files: dict[str, str]  # path -> component
    commits: Counter[str] = field(default_factory=Counter[str])  # per component
    components_together: Counter[tuple[str, str]] = field(
        default_factory=Counter[tuple[str, str]]
    )
    files_together: list[tuple[str, str, int]] = field(
        default_factory=list[tuple[str, str, int]]
    )
    used: int = 0  # commits counted
    skipped: int = 0  # commits over MAX_FILES

    def popular(self) -> list[str]:
        """Components, most changed first (ties by name)."""
        names = set(self.files.values())
        return sorted(names, key=lambda c: (-self.commits[c], c))

    def partners(self, component: str, k: int) -> list[tuple[str, float]]:
        """The k components most often changed with `component`, with the
        share of its commits they shared (as `store.changes_with`)."""
        commits = self.commits[component]
        if not commits:
            return []
        found = [
            (b if a == component else a, n)
            for (a, b), n in self.components_together.items()
            if component in (a, b)
        ]
        found.sort(key=lambda x: (-x[1], x[0]))
        return [(name, n / commits) for name, n in found[:k]]


def build(
    files: Iterable[str], history: Iterable[Commit], rules: ComponentRules
) -> RepoMap:
    """Pairs are stored once, in sorted order: (a, b) with a < b."""
    m = RepoMap(files={f: component_of(f, rules) for f in files})
    pairs: Counter[tuple[str, str]] = Counter()
    for commit in history:
        touched = sorted({f for f in commit.files if f in m.files})
        if not touched:
            continue
        if len(touched) > MAX_FILES:
            m.skipped += 1
            continue
        m.used += 1
        components = sorted({m.files[f] for f in touched})
        m.commits.update(components)  # +1 per component this commit touched
        # +1 for every pair in this commit: files a, b, c -> (a,b) (a,c) (b,c)
        m.components_together.update(combinations(components, 2))
        pairs.update(combinations(touched, 2))
    m.files_together = _strongest(pairs)
    return m


def _strongest(pairs: Counter[tuple[str, str]]) -> list[tuple[str, str, int]]:
    """Each file's FILE_PARTNERS strongest pairs with at least MIN_PAIR shared
    commits; a pair kept by either side is kept once."""
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    for (a, b), n in pairs.items():
        if n >= MIN_PAIR:
            by_file.setdefault(a, []).append((n, a, b))
            by_file.setdefault(b, []).append((n, a, b))
    kept: set[tuple[str, str, int]] = set()
    for edges in by_file.values():
        for n, a, b in sorted(edges, key=lambda e: (-e[0], e[1], e[2]))[:FILE_PARTNERS]:
            kept.add((a, b, n))
    return sorted(kept)
