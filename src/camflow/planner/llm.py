"""Pluggable LLM caller for the planner.

The planner needs exactly one strong-model call to turn a natural-language
request into a workflow.yaml. We keep the call abstracted behind a single
function so tests can mock it and a later migration to the anthropic SDK
or another provider is a one-liner.

Default strategy (auto-detected):

1. If `ANTHROPIC_API_KEY` is set and the `anthropic` package is importable,
   use the SDK directly (best — single process, no tmux).
2. Otherwise, invoke `claude -p "<prompt>"` via subprocess. This uses the
   user's existing Claude Code CLI login and doesn't require a new API key.
3. If neither is available, raise `LLMUnavailable` with a clear message.

Consumers call `default_llm_call(prompt)` and get a response string.
"""

from __future__ import annotations

import os
import shutil
import subprocess


DEFAULT_TIMEOUT = 180  # seconds — a planner call should finish in <3 min


class LLMUnavailable(RuntimeError):
    """Raised when no LLM backend is reachable."""


def _try_anthropic_sdk(prompt, timeout=DEFAULT_TIMEOUT):
    """Call Claude via the anthropic SDK. Returns the text or raises."""
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError as exc:
        raise LLMUnavailable("anthropic SDK not installed") from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)
    # Use whatever the user's default is via env; hard-code a capable model.
    model = os.environ.get("CAMFLOW_PLANNER_MODEL", "claude-sonnet-4-6")
    msg = client.messages.create(
        model=model,
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}],
    )
    # Concatenate any text blocks in the response.
    parts = []
    for block in getattr(msg, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _try_claude_cli(prompt, timeout=DEFAULT_TIMEOUT):
    """Fallback: `claude -p "<prompt>"` via subprocess. Returns stdout."""
    claude_bin = shutil.which("claude")
    if not claude_bin:
        raise LLMUnavailable("claude CLI not on PATH")

    try:
        proc = subprocess.run(
            [claude_bin, "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMUnavailable(f"claude CLI timed out after {timeout}s") from exc
    except Exception as exc:
        raise LLMUnavailable(f"claude CLI failed to launch: {exc}") from exc

    if proc.returncode != 0:
        raise LLMUnavailable(
            f"claude CLI exit {proc.returncode}: {proc.stderr[:500]}"
        )
    return proc.stdout


def default_llm_call(prompt, timeout=DEFAULT_TIMEOUT):
    """Try anthropic SDK first, then claude CLI. Raise LLMUnavailable if both fail."""
    errors = []
    for backend in (_try_anthropic_sdk, _try_claude_cli):
        try:
            return backend(prompt, timeout=timeout)
        except LLMUnavailable as exc:
            errors.append(f"{backend.__name__}: {exc}")
            continue
    raise LLMUnavailable(
        "No LLM backend available. Tried: " + " | ".join(errors)
    )
