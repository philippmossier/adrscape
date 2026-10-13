import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from neo4j import Driver
from neo4j.exceptions import ServiceUnavailable
from psycopg import sql

from adrscape.config import get_settings
from adrscape.store import graph, postgres
from tests.gitrepo import init_repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(
        tmp_path / "demo-repo",
        {
            "docs/adr/0001-use-sse.md": "# Use SSE\n",
            "README.md": "# Demo\n",
            "main.py": "print('hi')\n",
        },
    )


@pytest.fixture
def conn() -> Iterator[postgres.Conn]:
    """A connection whose tables live in a throwaway schema."""
    schema = f"test_{uuid.uuid4().hex[:8]}"
    ident = sql.Identifier(schema)
    dsn = get_settings().postgres_dsn
    try:
        admin = psycopg.connect(dsn, connect_timeout=2, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("Postgres not reachable (make up)")
    with admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(ident))
        scoped = f"{dsn}?options=-csearch_path%3D{schema},public"
        try:
            with postgres.connect(scoped) as c:
                yield c
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(ident))


# The throwaway instance from compose (`neo4j-test`), never the dev graph.
TEST_URI = "bolt://localhost:7688"


@pytest.fixture
def driver() -> Iterator[Driver]:
    settings = get_settings()
    password = settings.neo4j_password.get_secret_value()
    try:
        d = graph.connect(TEST_URI, settings.neo4j_user, password)
    except ServiceUnavailable:
        pytest.skip("Neo4j test instance not reachable (make up)")
    with d:
        d.execute_query("MATCH (n) DETACH DELETE n")
        yield d
