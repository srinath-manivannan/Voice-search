"""The Grok client: header, not URL, and a failure that cannot leak the key."""

from __future__ import annotations

from typing import Any

import pytest

from src.memory.grok import GrokClient, GrokError


class Scripted:
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.calls.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return self.status, self.body


async def test_the_key_is_a_bearer_header_and_not_part_of_the_url():
    scripted = Scripted(
        200,
        {
            "model": "grok-4.3",
            "choices": [{"message": {"content": "Budget:\nnone"}}],
            "usage": {
                "prompt_tokens": 40,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
        },
    )
    client = GrokClient("xai-test-secret", "grok-4.3", timeout_s=2.5, post=scripted)
    done = await client.chat(
        [{"role": "user", "content": "hello"}],
        max_tokens=150,
    )

    sent = scripted.calls[0]
    assert sent["headers"]["Authorization"] == "Bearer xai-test-secret"
    assert "xai-test-secret" not in sent["url"]
    assert sent["json"]["model"] == "grok-4.3"
    assert sent["json"]["max_tokens"] == 150
    assert sent["timeout"] == 2.5
    assert done.text == "Budget:\nnone"
    assert done.prompt_tokens == 40
    assert done.cached_tokens == 8
    assert done.completion_tokens == 12


async def test_a_refusal_names_the_status_and_not_the_key():
    scripted = Scripted(401, {"error": "bad key xai-test-secret"})
    client = GrokClient("xai-test-secret", "grok-4.3", post=scripted)
    with pytest.raises(GrokError) as raised:
        await client.chat([{"role": "user", "content": "hi"}], max_tokens=10)
    assert "401" in str(raised.value)
    assert "xai-test-secret" not in str(raised.value)


async def test_an_empty_completion_is_a_failure():
    scripted = Scripted(200, {"choices": [{"message": {"content": "  "}}]})
    client = GrokClient("xai-test-secret", "grok-4.3", post=scripted)
    with pytest.raises(GrokError, match="empty"):
        await client.chat([{"role": "user", "content": "hi"}], max_tokens=10)
