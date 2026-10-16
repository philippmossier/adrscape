"""The coding agent cofabriq delegates to: Claude Code in headless mode.

cofabriq doesn't parse code (scope cut); reading it is this agent's job.
One call = one fresh session in a working directory, a fixed tool list and
a JSON Schema the final answer must match. `claude -p` over the Agent SDK:
the SDK drives the same CLI, so cost and behaviour are the same, and the
subprocess needs no dependency. Swap when a step needs hooks.

Flags that make a run reproducible and contained:
- `--bare`: none of the caller's own Claude Code setup (user CLAUDE.md,
  memory, hooks, plugins); auth is `ANTHROPIC_API_KEY` only. The target's
  CLAUDE.md is passed back in with `--add-dir`, as a dev's agent would see it.
- `--restricted` + `--tools`: no Bash or code-running tools, file tools
  confined to the working directory.
- `--strict-mcp-config`: no MCP servers.
- `--max-budget-usd`: the run stops at the cap.
- `--permission-mode acceptEdits`, only with a writing tool: a headless run
  has nobody to ask. `--restricted` still keeps edits in the working
  directory and refuses git and settings files. No tool runs code: what the
  agent writes is only executed by cofabriq, in the sandbox (`sandbox.py`).
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field, ValidationError

READ_ONLY = ("Read", "Grep", "Glob")
EDIT = (*READ_ONLY, "Edit", "Write")


class AgentRun(BaseModel):
    output: dict[str, object]  # the structured answer, matching the schema
    model: str
    cost_usd: float
    turns: int
    latency_ms: int


class AgentError(RuntimeError):
    pass


class Agent(Protocol):
    """Runs one session in `cwd` and returns its answer matching `schema`."""

    def __call__(
        self, prompt: str, schema: dict[str, object], cwd: Path
    ) -> AgentRun: ...


@dataclass(frozen=True)
class ClaudeCode:
    api_key: str
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    tools: tuple[str, ...] = READ_ONLY
    budget_usd: float = 1.0
    timeout_s: float = 600
    binary: str = "claude"

    def editing(self, budget_usd: float) -> "ClaudeCode":
        """The same agent with Edit/Write (the implementer of the loop)."""
        return replace(self, tools=EDIT, budget_usd=budget_usd)

    def command(self, schema: dict[str, object], cwd: Path) -> list[str]:
        """`claude -p` as argv; the prompt goes in on stdin (see `__call__`)."""
        writes = bool({"Edit", "Write"} & set(self.tools))
        return [
            self.binary,
            "-p",  # headless: one prompt in, one answer out, no UI
            "--bare",  # none of the caller's own Claude Code setup
            "--restricted",  # no code-running tools, files confined to cwd
            "--strict-mcp-config",  # no MCP servers
            "--no-session-persistence",  # nothing saved for --resume
            "--tools",  # the only tools the agent gets
            ",".join(self.tools),
            "--add-dir",  # cwd: also loads the target's own CLAUDE.md
            str(cwd),
            # only when it can write: approve edits without asking
            *(["--permission-mode", "acceptEdits"] if writes else []),
            "--model",
            self.model,
            "--effort",
            self.effort,
            "--max-budget-usd",  # the run stops at this cost
            str(self.budget_usd),
            "--output-format",  # one JSON result: answer, cost, turns
            "json",
            "--json-schema",  # the final answer must match this schema
            json.dumps(schema),
        ]

    def __call__(self, prompt: str, schema: dict[str, object], cwd: Path) -> AgentRun:
        start = time.perf_counter()
        done = subprocess.run(
            self.command(schema, cwd),
            input=prompt,  # stdin: no argv size limit, not visible in `ps`
            capture_output=True,
            text=True,
            cwd=cwd,
            env=_env(self.api_key),
            timeout=self.timeout_s,
            check=False,
        )
        latency_ms = round((time.perf_counter() - start) * 1000)
        return parse(done.stdout, done.stderr, latency_ms)


def _env(api_key: str) -> dict[str, str]:
    """The caller's environment minus any Claude Code session it runs in
    (cofabriq itself may be started from one), plus the key."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE_CODE_") and k != "CLAUDECODE"
    }
    env["ANTHROPIC_API_KEY"] = api_key
    return env


class _Result(BaseModel):
    """The fields of the `--output-format json` result cofabriq reads."""

    subtype: str
    is_error: bool = False
    result: str | None = None
    structured_output: dict[str, object] | None = None
    total_cost_usd: float = 0.0
    num_turns: int = 0
    model_usage: dict[str, object] = Field(default={}, alias="modelUsage")


def parse(stdout: str, stderr: str, latency_ms: int) -> AgentRun:
    """The result line, or AgentError."""
    try:
        result = _Result.model_validate_json(stdout)
    except ValidationError as exc:
        raise AgentError(f"no JSON from claude: {stderr.strip()[:500]}") from exc
    if result.is_error or result.subtype != "success":
        raise AgentError(f"claude run failed: {result.result or result.subtype}")
    if result.structured_output is None:
        raise AgentError("claude returned no structured output")
    return AgentRun(
        output=result.structured_output,
        model=next(iter(result.model_usage), "unknown"),
        cost_usd=result.total_cost_usd,
        turns=result.num_turns,
        latency_ms=latency_ms,
    )
