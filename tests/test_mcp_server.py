import asyncio
from typing import Any

from mcp import Client
from neo4j import Driver

from cofabriq.context.store import postgres
from cofabriq.mcp_server import build
from tests.test_embed_store import FakeEmbedder, chunk
from tests.test_graph import adr, link, sync


def call(conn: postgres.Conn, driver: Driver, tool: str, args: dict[str, Any]) -> Any:
    """Call one tool through a real MCP session (in process); return its data."""

    async def run() -> Any:
        async with Client(build(FakeEmbedder(), conn, driver)) as client:
            result = await client.call_tool(tool, args)
        assert not result.is_error, result.content
        assert result.structured_content is not None
        return result.structured_content["result"]

    return asyncio.run(run())


def test_lists_the_three_tools(conn: postgres.Conn, driver: Driver) -> None:
    async def run() -> dict[str, Any]:
        async with Client(build(FakeEmbedder(), conn, driver)) as client:
            listed = await client.list_tools()
        return {t.name: t.output_schema for t in listed.tools}

    schemas = asyncio.run(run())

    assert sorted(schemas) == ["affected", "decisions", "search"]
    assert "in_force" in str(schemas["decisions"])  # a field, not a property


def test_search_returns_hits_with_document_ids(
    conn: postgres.Conn, driver: Driver
) -> None:
    docs = [chunk("a.md", "fan out requests in the browser"), chunk("b.md", "kafka")]
    postgres.sync(conn, docs, FakeEmbedder())

    hits = call(conn, driver, "search", {"query": "fan out", "k": 1})

    assert [(h["repo"], h["path"]) for h in hits] == [("repo-a", "a.md")]


def test_decisions_show_a_revisit_across_projects(
    conn: postgres.Conn, driver: Driver
) -> None:
    sync(
        driver,
        adr("web", 1),
        adr("api", 1, revisits=[link("web:docs/adr/0001.md")]),
    )

    found = {d["id"]: d for d in call(conn, driver, "decisions", {})}

    assert found["web:docs/adr/0001.md"]["revisited_by"] == ["api:docs/adr/0001.md"]
    assert all(d["in_force"] for d in found.values())


def test_affected_is_empty_for_an_unknown_id(
    conn: postgres.Conn, driver: Driver
) -> None:
    assert call(conn, driver, "affected", {"adr": "nope:x.md"}) == []
