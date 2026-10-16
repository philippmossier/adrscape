"""Where the target's commands run: a container, never the host.

The coding agent edits files but runs nothing; cofabriq runs the target's
install, repro and checks. Those execute code the agent wrote, and the
agent is steered by issue text anyone can write. So every command runs in
a throwaway podman container that sees only the worktree (mounted at
/app) and two cache volumes. Only the install gets network, and it runs
before the agent has changed anything (the target's own lockfile, as a dev
would install it); the repro and the checks run with `--network none`.
"""

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

TAIL = 4000  # characters of output kept from a failing command
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Ran(BaseModel):
    ok: bool
    seconds: float
    output: str  # the tail of stdout + stderr, ANSI codes stripped


class Sandbox(Protocol):
    """Runs `cmd` (a shell line) in `worktree`, with or without network."""

    def __call__(self, cmd: str, worktree: Path, *, network: bool) -> Ran: ...


@dataclass(frozen=True)
class Podman:
    image: str
    setup: str = ""  # shell line run first in every container (e.g. corepack)
    timeout_s: float = 600
    binary: str = "podman"

    def command(self, cmd: str, worktree: Path, *, network: bool) -> list[str]:
        """`podman run` of one shell line in a new container, as argv.

        Roughly: `podman run --rm --network none -v <worktree>:/app -w /app
        node:24-slim sh -c "corepack enable && pnpm test"`.
        """
        line = f"{self.setup} && {cmd}" if self.setup else cmd
        return [
            self.binary,
            "run",
            "--rm",  # delete the container when the command exits (not files)
            # "none": no network at all; "bridge": podman's default network
            "--network",
            "bridge" if network else "none",
            # the worktree is the only host folder the container sees, as /app
            "--volume",
            f"{worktree.resolve()}:/app",
            "--workdir",
            "/app",
            # named volumes (podman-managed, not host folders): the pnpm
            # package store and the pnpm binary, kept between runs, so a
            # warm install is fast and pnpm exists without network
            "--volume",
            "cofabriq-pnpm-store:/pnpm-store",
            "--volume",
            "cofabriq-corepack:/corepack",
            "--env",
            "pnpm_config_store_dir=/pnpm-store",  # pnpm 11 reads this name
            "--env",
            "COREPACK_HOME=/corepack",
            # plain output for the agent: no interactive mode, no colours
            "--env",
            "CI=1",
            "--env",
            "NO_COLOR=1",
            self.image,  # from the target file, e.g. node:24-slim
            "sh",
            "-c",
            line,  # the shell line runs inside the container, never on the host
        ]

    def __call__(self, cmd: str, worktree: Path, *, network: bool) -> Ran:
        return run_command(
            self.command(cmd, worktree, network=network), worktree, self.timeout_s
        )


def run_command(argv: list[str], cwd: Path, timeout_s: float) -> Ran:
    start = time.perf_counter()
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_s,
            check=False,
        )
        ok, output = done.returncode == 0, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        ok, output = False, f"timed out after {timeout_s:.0f} s"
    return Ran(
        ok=ok,
        seconds=round(time.perf_counter() - start, 1),
        output=tail(output),
    )


def tail(output: str) -> str:
    text = _ANSI.sub("", output).strip()
    return text if len(text) <= TAIL else "...\n" + text[-TAIL:]
