"""Step lines for long CLI commands: a spinner while a step runs, then one
`✓ name  detail` line that stays. Written to stderr, so stdout stays the
command's real output (a card, JSON for `--json`).

Rich drops colour and animation when stderr is not a terminal (pipes, CI) or
`NO_COLOR` is set. `Steps(None)` is silent: the MCP server and the eval run
the same pipeline without printing anything.
"""

from collections.abc import Callable, Generator
from contextlib import contextmanager

from rich.console import Console
from rich.markup import escape

NAME_WIDTH = 10


class Steps:
    def __init__(self, console: Console | None) -> None:
        self._console = console

    @contextmanager
    def step(self, name: str, doing: str) -> Generator[Callable[[str], None]]:
        """Spinner with `doing` while the block runs; the block calls the
        yielded function with the detail for the ✓ line."""
        if self._console is None:
            yield lambda _: None
            return
        detail = [doing]
        label = f"[bold]{name:<{NAME_WIDTH}}[/]"
        try:
            with self._console.status(f"{label} {escape(doing)}...", spinner="dots"):
                yield lambda text: detail.__setitem__(0, text)
        except Exception:
            self._console.print(f"[red]✗[/] {label} {escape(doing)}")
            raise
        self._console.print(f"[green]✓[/] {label} {escape(detail[0])}")

    def note(self, name: str, detail: str, *, ok: bool = True) -> None:
        """A step with nothing to wait for: `✓`, or `!` if something was off."""
        if self._console is None:
            return
        mark = "[green]✓[/]" if ok else "[yellow]![/]"
        self._console.print(f"{mark} [bold]{name:<{NAME_WIDTH}}[/] {escape(detail)}")
