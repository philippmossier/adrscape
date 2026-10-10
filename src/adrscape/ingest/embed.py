"""Turn chunks into embedding vectors."""

from collections.abc import Callable, Sequence

from openai import OpenAI

from adrscape.ingest.chunk import Chunk

# The dimension is baked into the `chunks.embedding` column: changing the
# model means a new column and a full re-embed (ADR-003).
MODEL = "text-embedding-3-small"
DIM = 1536
BATCH = 128  # far below the API's 2048 inputs / 300k tokens per request

type Embed = Callable[[Sequence[str]], list[list[float]]]


def embed_input(chunk: Chunk) -> str:
    """The text that gets embedded: heading trail first, then the chunk.

    A chunk split out of a long section has lost its document title; the
    trail puts it back, so "## Consequences" of an ADR still embeds as part
    of that ADR. For whole-document chunks the trail repeats the title once.
    """
    return f"{chunk.heading}\n\n{chunk.text}"


def openai_embedder(api_key: str) -> Embed:
    client = OpenAI(api_key=api_key)

    def embed(texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            batch = list(texts[i : i + BATCH])
            response = client.embeddings.create(model=MODEL, input=batch)
            vectors.extend(d.embedding for d in response.data)
        return vectors

    return embed
