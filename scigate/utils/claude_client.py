"""Thin wrapper around the Anthropic SDK for SciGate's Claude calls."""

from __future__ import annotations

import os
from typing import Optional

import anthropic

_client: Optional[anthropic.Anthropic] = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. "
                "Export it before running SciGate: export ANTHROPIC_API_KEY=sk-..."
            )
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def ask_claude(
    system: str,
    user_message: str,
    *,
    model: str = "claude-sonnet-4-20250514",
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> str:
    """Single-turn Claude call. Returns the assistant's text response."""
    client = get_client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )
    return resp.content[0].text
