"""The ADR domain: what the LLM extracts from one document (third seam).

A different domain (contracts, say) is a different module with its own
`Extraction`; the extraction pipeline only needs a pydantic model to fill.
Field descriptions end up in the JSON schema the model sees, so they are
part of the prompt.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class AdrStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    DEPRECATED = "deprecated"
    SUPERSEDED = "superseded"


class Link(BaseModel):
    """An edge to another entity, with the sentence that justifies it."""

    target: str = Field(description="An id copied exactly from the list given.")
    evidence: str = Field(
        description="A short verbatim quote from the document that states the "
        "relation. Copy it character for character, do not paraphrase."
    )


class Extraction(BaseModel):
    """What one document says beyond its headers."""

    status: AdrStatus | None = Field(
        description="ADRs only: the status, mapped to one of the values. "
        "null for any document that is not an ADR."
    )
    technologies: list[str] = Field(
        description="Technologies the document uses or decides on: languages, "
        "frameworks, libraries, protocols, services. Short canonical names "
        "('SSE', 'FastAPI', 'Postgres'), no versions, no explanations."
    )
    teams: list[str] = Field(
        description="Teams or roles named as owners or deciders. Empty if none "
        "is named; never invent one."
    )
    revises: list[Link] = Field(
        description="ADRs only: earlier ADRs this one revises, supersedes or "
        "replaces, so the earlier one no longer holds. Targets from the "
        "candidate ADR list. An ADR that is merely similar or mentioned is not "
        "revised."
    )
    revisits: list[Link] = Field(
        description="ADRs only: earlier ADRs whose question this one decides "
        "again, often for another project, while the earlier decision still "
        "holds where it was made. Targets from the candidate ADR list. Not for "
        "ADRs already in `revises`."
    )
    depends_on: list[Link] = Field(
        description="Projects this document's project depends on (shares a "
        "protocol, calls, reuses), targets from the project list. Not itself."
    )
    affects: list[str] = Field(
        description="Projects whose behaviour this document's decision changes, "
        "names from the project list. Usually its own project."
    )
