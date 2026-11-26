"""xAI Grok, over the OpenAI-compatible chat completions endpoint.

The key travels in the Authorization header and nowhere else. A URL ends up
in exception text, access logs and proxy logs; a header does not. The same
reason the text-model adapter refuses to put the Gemini key on the query
string applies here, and Grok's own API allows the header, so there is no
excuse to do the worse thing.

Nothing in this file knows what a handover is. It sends messages and reads
tokens back. The summariser decides what those messages say.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

HOST = "https://api.x.ai/v1/chat/completions"

# What `chat` is allowed to hand the transport. Narrow on purpose: a looser
# callable would let a test pass a client that logs its headers.
Post = Callable[..., Awaitable[tuple[int, dict[str, Any]]]]


class GrokError(RuntimeError):
    """The Grok call did not produce a usable completion.

    The message is a status or a short reason. It never includes the request
    URL, the headers, or the key. httpx puts none of those in the status
    line, and we do not paste the body: an error body is allowed to echo a
    header back.
    """


@dataclass(frozen=True)
class GrokCompletion:
    """One reply, plus the tokens xAI says it billed."""

    text: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    model: str


def _safe_reason(status: int) -> str:
    return f"grok returned {status}"


async def _post(
    url: str,
    *,
    headers: dict[str, str],
    json: dict[str, Any],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    import httpx

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=json)
        try:
            body: Any = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        return response.status_code, body


class GrokClient:
    """One model, one key, chat completions only."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 4.0,
        post: Post | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_s
        self._post = post or _post

    @property
    def model(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
    ) -> GrokCompletion:
        """Send one completion. Raises GrokError on any refusal.

        The timeout is the client's. A caller that also wraps this in
        `wait_for` is belt and braces; either one failing is a failed
        summary, and the call on the phone does not hear about it.
        """
        if not self._api_key:
            raise GrokError("grok api key is empty")
        status, body = await self._post(
            HOST,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=self._timeout,
        )
        if status >= 400:
            raise GrokError(_safe_reason(status))
        return _completion(body, self._model)


def _completion(body: dict[str, Any], fallback_model: str) -> GrokCompletion:
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise GrokError("grok returned no choices")
    message = choices[0].get("message") or {}
    if not isinstance(message, dict):
        raise GrokError("grok returned no message")
    text = message.get("content") or ""
    if not isinstance(text, str) or not text.strip():
        raise GrokError("grok returned an empty completion")

    usage = body.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}
    model = body.get("model")
    return GrokCompletion(
        text=text.strip(),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        model=model if isinstance(model, str) and model else fallback_model,
    )
