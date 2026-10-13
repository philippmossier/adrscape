from typer.testing import CliRunner

from cofabriq.cli import app
from cofabriq.config import Settings


def test_settings_have_defaults() -> None:
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.neo4j_uri.startswith("bolt://")


def test_cli_lists_health_command() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "health" in result.output
