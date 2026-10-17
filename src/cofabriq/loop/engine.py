"""The coding agent cofabriq delegates to: Claude Code in headless mode.

cofabriq doesn't parse code (scope cut); reading it is this agent's job.
One call = one fresh session in a working directory, a fixed tool list and
a JSON Schema the final answer must match. `claude -p` over the Agent SDK:
the SDK drives the same CLI, so cost and behaviour are the same, and the
subprocess needs no dependency. Swap when a step needs hooks.

Flags that make a run reproducible and contained:
- `--bare` (API key): none of the caller's own Claude Code setup (user
  CLAUDE.md, memory, hooks, plugins); auth is `ANTHROPIC_API_KEY` only.
- `--append-system-prompt-file`: the target's own CLAUDE.md, as a dev's
  agent would see it. Both modes skip CLAUDE.md discovery (and `--add-dir`
  doesn't bring it back, measured), so it is passed in explicitly; parent
  directories' CLAUDE.md files (cofabriq's own, a dev's private one) never.
- `--safe-mode` (subscription): `--bare` never reads an OAuth token, so the
  same isolation comes from safe mode (no CLAUDE.md, hooks, plugins, skills;
  the target's own `.claude/settings.json` hooks included) plus an empty
  `CLAUDE_CONFIG_DIR` per run. Opt-in for the dev's own runs: Anthropic
  doesn't allow offering claude.ai login to others, so cofabriq never does.
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
import tempfile
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


@dataclass(frozen=True)
class ApiKey:
    key: str  # ANTHROPIC_API_KEY, billed per token


@dataclass(frozen=True)
class Subscription:
    token: str  # from `claude setup-token`: the dev's own Claude plan


type Auth = ApiKey | Subscription


class Agent(Protocol):
    """Runs one session in `cwd` and returns its answer matching `schema`."""

    def __call__(
        self, prompt: str, schema: dict[str, object], cwd: Path
    ) -> AgentRun: ...


@dataclass(frozen=True)
class ClaudeCode:
    auth: Auth
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
        rules = (cwd / "CLAUDE.md").absolute()  # claude runs inside cwd
        return [
            self.binary,
            "-p",  # headless: one prompt in, one answer out, no UI
            # none of the caller's own Claude Code setup (see the docstring)
            "--bare" if isinstance(self.auth, ApiKey) else "--safe-mode",
            "--restricted",  # no code-running tools, files confined to cwd
            "--strict-mcp-config",  # no MCP servers
            "--no-session-persistence",  # nothing saved for --resume
            "--tools",  # the only tools the agent gets
            ",".join(self.tools),
            # the target's own rules (see the docstring)
            *(["--append-system-prompt-file", str(rules)] if rules.is_file() else []),
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
        with tempfile.TemporaryDirectory(prefix="cofabriq-claude-") as config:
            done = subprocess.run(
                self.command(schema, cwd),
                input=prompt,  # stdin: no argv size limit, not visible in `ps`
                capture_output=True,
                text=True,
                cwd=cwd,
                env=_env(self.auth, Path(config)),
                timeout=self.timeout_s,
                check=False,
            )
        latency_ms = round((time.perf_counter() - start) * 1000)
        return parse(done.stdout, done.stderr, latency_ms)


def _env(auth: Auth, config: Path) -> dict[str, str]:
    """The caller's environment minus any Claude Code session it runs in
    (cofabriq itself may be started from one) and any credential, plus the
    one `auth` names. An API key outranks an OAuth token in Claude Code, so a
    stray key would silently bill a subscription run: none is passed."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("CLAUDE_CODE_", "ANTHROPIC_"))
        and k not in ("CLAUDECODE", "CLAUDE_CONFIG_DIR")
    }
    match auth:
        case ApiKey(key):
            env["ANTHROPIC_API_KEY"] = key
        case Subscription(token):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            env["CLAUDE_CONFIG_DIR"] = str(config)  # no user settings or login
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
