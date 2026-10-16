from pathlib import Path

import pytest

from cofabriq.eval.bugs import load_bugs

PLAYGROUND = Path(__file__).parent.parent / "playground"


def test_answer_key_matches_seed_and_files() -> None:
    bugs = load_bugs(PLAYGROUND / "bugs.toml", PLAYGROUND / "seed.toml")
    assert len(bugs) == 22
    assert {b.kind for b in bugs} == {"logic", "ui"}


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bugs.toml").write_text(
        '[[bug]]\nkey = "nope"\nkind = "ui"\nsize = "S"\n'
        'files = []\nrepro = "x.spec.ts"\n'
    )
    with pytest.raises(ValueError, match="no seed issue"):
        load_bugs(tmp_path / "bugs.toml", PLAYGROUND / "seed.toml")
