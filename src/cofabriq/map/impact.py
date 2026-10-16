"""Impact: which components a change to an issue (or a plan) will touch.

No code is read. The earlier issues most similar to this one, among those
with a fix on the map (pgvector), vote with the components their fixes
touched ("fixes #N" commits), weighted by similarity. Separately, what
usually changes with the top components (co-change) is listed as the blast
radius: on vscode, mixing it into the ranking did not help. Measured by
`cofabriq eval impact`, which calls `vote` and `spread` from here.
"""

from collections.abc import Callable, Mapping, Sequence

from pydantic import BaseModel

SPREAD = 3  # top components whose co-change partners are added

type Ranked = list[tuple[str, float]]  # component, score; best first


def vote(
    neighbours: Sequence[tuple[int, float]], fixed: Callable[[int], list[str]]
) -> tuple[Ranked, dict[str, list[int]]]:
    """Each neighbour (number, similarity) adds its similarity to every
    component its fix touched. Also returns which issues voted for what."""
    votes: dict[str, float] = {}
    via: dict[str, list[int]] = {}
    for number, score in neighbours:
        for component in fixed(number):
            votes[component] = votes.get(component, 0.0) + score
            via.setdefault(component, []).append(number)
    return sorted(votes.items(), key=lambda v: (-v[1], v[0])), via


def spread(
    ranked: Ranked, partners: Callable[[str], list[tuple[str, float]]]
) -> Ranked:
    """Add the co-change partners of the SPREAD best components, each worth
    its parent's score times the share of the parent's commits it shares."""
    scores = dict(ranked)
    for component, score in ranked[:SPREAD]:
        for partner, share in partners(component):
            scores[partner] = scores.get(partner, 0.0) + score * share
    return sorted(scores.items(), key=lambda v: (-v[1], v[0]))


class Touch(BaseModel):
    component: str
    score: float  # summed similarity of the issues that voted (relative only)
    via: list[int]  # earlier issues whose fix touched it; empty: co-change only


class Impact(BaseModel):
    repo: str
    map_rev: str  # the map's commit: co-change and fixes known up to it
    components: list[Touch]  # likely touched, best first
    blast: list[Touch]  # not voted for, but usually changes with the top ones
    neighbours: list[tuple[int, float]]  # earlier fixed issues looked at


def assemble(
    repo: str,
    map_rev: str,
    neighbours: Sequence[tuple[int, float]],
    fixed: Mapping[int, list[str]],
    partners: Callable[[str], list[tuple[str, float]]],
    k: int,
) -> Impact:
    ranked, via = vote(neighbours, lambda n: fixed.get(n, []))
    voted = dict(ranked)
    blast = [(c, s) for c, s in spread(ranked, partners) if c not in voted]
    return Impact(
        repo=repo,
        map_rev=map_rev,
        components=[
            Touch(component=c, score=round(s, 3), via=via[c]) for c, s in ranked[:k]
        ],
        blast=[Touch(component=c, score=round(s, 3), via=[]) for c, s in blast[:k]],
        neighbours=[(n, round(s, 3)) for n, s in neighbours],
    )
