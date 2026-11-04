"""Text model adapter - the sibling of src/voice/gemini_live.py.

Chat deliberately does not go through the Live API. `gemini-3.8-live` is a
speech-to-speech model; billing a text conversation at audio-model rates buys
nothing. Flash-Lite is 2.5x cheaper on input and 1.8x on output for a job the
Live model is no better at.

Streaming is over server-sent events rather than a socket, because a text turn
is one request and a websocket would be ceremony. Tokens are yielded as they
arrive: a reply that appears word by word reads as fast at a latency that would
feel like waiting if it landed all at once.

The transport is injected for the same reason the voice adapter injects its
websocket - so the whole thing can be tested without spending anything.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

HOST = "https://generativelanguage.googleapis.com"


class Response(Protocol):
    def raise_for_status(self) -> Any: ...
    async def aiter_lines(self) -> AsyncIterator[str]: ...


class Streamer(Protocol):
    """The slice of an HTTP client this adapter uses.

    Declared as exactly the one call shape used below rather than **kwargs -
    httpx takes these keyword-only, and a looser protocol here silently stops
    checking the thing it exists to check.
    """

    def stream(
        self,
        method: str,
        url: str,
        *,
        params: Any = ...,
        json: Any = ...,
        headers: Any = ...,
    ) -> Any: ...


def _default_streamer() -> Streamer:
    import httpx

    return httpx.AsyncClient(timeout=60.0)


class UpstreamError(RuntimeError):
    """The model API rejected the request.

    Carries the status and the response body, and deliberately NOT the URL.
    httpx puts the full URL in its own exception text, and with the key as a
    query parameter that writes the secret into every traceback and log line.
    """


class ChatTurn:
    """One model reply, streamed.

    Collects text as it arrives, holds the usage block from the final chunk,
    and carries any tool the model asked us to call.
    """

    def __init__(self) -> None:
        self.text = ""
        self.usage: dict[str, Any] = {}
        self.function_call: dict[str, Any] | None = None
        # The whole part, not just the call. Gemini returns a
        # thoughtSignature beside the functionCall and rejects the follow-up
        # request with a 400 if it is not echoed back verbatim.
        self.function_call_part: dict[str, Any] | None = None


class GeminiTextModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        system_instruction: str,
        streamer_factory: Callable[[], Streamer] | None = None,
        tools: dict[str, Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._system = system_instruction
        self._streamer_factory = streamer_factory or _default_streamer
        self._tools = tools

    def _payload(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contents": history,
            "systemInstruction": {"parts": [{"text": self._system}]},
            # No thinkingConfig. The voice path sets thinkingBudget 0 because
            # the Live model thinks by default and bills for it; Flash-Lite
            # does not think at all and REJECTS the field outright with a 400
            # INVALID_ARGUMENT. Copying the voice config across cost an hour
            # and is the reason the upstream body is now in the error.
            "generationConfig": {"temperature": 0.7},
        }
        if self._tools:
            payload["tools"] = [self._tools]
        return payload

    async def stream(
        self, history: list[dict[str, Any]]
    ) -> AsyncIterator[tuple[str, ChatTurn]]:
        """Yield (delta, turn) as text arrives. The final turn carries usage."""
        turn = ChatTurn()
        url = f"{HOST}/v1beta/models/{self._model}:streamGenerateContent"
        client = self._streamer_factory()

        async with client.stream(
            "POST",
            url,
            params={"alt": "sse"},
            # The key goes in a header, never the query string. A URL ends up in
            # exception text, access logs and proxy logs; a header does not.
            headers={"x-goog-api-key": self._api_key},
            json=self._payload(history),
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", "replace")[:400]
                raise UpstreamError(
                    f"{self._model} returned {response.status_code}: {body}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk = json.loads(line[5:].strip())

                if usage := chunk.get("usageMetadata"):
                    turn.usage = usage

                for candidate in chunk.get("candidates") or []:
                    for part in (candidate.get("content") or {}).get("parts") or []:
                        if call := part.get("functionCall"):
                            turn.function_call = call
                            turn.function_call_part = part
                        if text := part.get("text"):
                            turn.text += text
                            yield text, turn

        # Always hand the turn back once at the end, even with no text.
        # A reply that is only a functionCall produces no text at all, so a
        # loop that yields solely on text never runs and the caller never sees
        # the turn - which looked exactly like the model saying nothing.
        yield "", turn
