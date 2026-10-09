from pathlib import Path

import pytest

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
