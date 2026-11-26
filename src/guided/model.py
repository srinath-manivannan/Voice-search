"""One request to the text model, answered in JSON, with its bill.

The guided call asks a model two kinds of question - which five, and what a
customer typed about the one they picked - and both are the same shape: an
instruction, some data, a schema for the reply, and a hard limit on how long
the reply may be. This is that shape, once, so that the things which must be
true of every paid call are true in one place:

- the key travels in a header and never in a URL or an exception;
- the reply is bounded before it is generated, which is what lets the
  governor admit a call against the most it CAN cost;
- what was spent is reported even when what came back was no use.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from src.voice.cost import FLASH_LITE, Rates

HOST = "https://generativelanguage.googleapis.com"


class ModelUnavailable(RuntimeError):
    """The model could not be asked.

    Never carries the URL or a header. An exception's text ends up in a log,
    and the key travels in a header for exactly that reason.
    """


class Poster(Protocol):
    async def post(self, url: str, *, json: Any = ..., headers: Any = ...) -> Any: ...


@dataclass(frozen=True)
class Reply:
    """What came back. `data` is None when it was not the JSON asked for."""

    data: dict[str, Any] | None
    prompt_tokens: int
    response_tokens: int
    thinking_tokens: int
    usd: float
    took_ms: int

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.response_tokens + self.thinking_tokens


def usd_for(
    prompt: int, response: int, thinking: int, rates: Rates = FLASH_LITE
) -> float:
    return (
        prompt * rates.prompt_text
        + response * rates.response_text
        + thinking * rates.thinking
    ) / 1e6


def _default_poster() -> Poster:
    import httpx

    # Five seconds is the promise made to the customer, and the catalogue
    # has already been read by the time this is called.
    return httpx.AsyncClient(timeout=6.0)


class JsonModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        rates: Rates = FLASH_LITE,
        poster_factory: Callable[[], Poster] | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._rates = rates
        self._poster_factory = poster_factory or _default_poster
        # One client, kept. A new TLS handshake on every call is a quarter
        # of a second the customer spends looking at an indicator.
        self._poster: Poster | None = None

    @staticmethod
    def request(
        instruction: str, said: str, schema: dict[str, Any], max_output_tokens: int
    ) -> dict[str, Any]:
        """The request body. A function of its arguments, so it can be tested."""
        return {
            "systemInstruction": {"parts": [{"text": instruction}]},
            "contents": [{"role": "user", "parts": [{"text": said}]}],
            "generationConfig": {
                # Nothing to be creative about: the same question about the
                # same facts should get the same answer.
                "temperature": 0.0,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }

    async def ask(
        self,
        instruction: str,
        said: str,
        schema: dict[str, Any],
        max_output_tokens: int,
    ) -> Reply:
        if self._poster is None:
            self._poster = self._poster_factory()
        started = time.perf_counter()
        try:
            response = await self._poster.post(
                f"{HOST}/v1beta/models/{self._model}:generateContent",
                # A header, never the query string: a URL ends up in
                # exception text, access logs and proxy logs.
                headers={"x-goog-api-key": self._api_key},
                json=self.request(instruction, said, schema, max_output_tokens),
            )
        except Exception as failed:  # noqa: BLE001 - any transport failure is the same here
            raise ModelUnavailable(type(failed).__name__) from None

        if response.status_code != 200:
            raise ModelUnavailable(f"the model returned {response.status_code}")
        body = response.json()
        usage = body.get("usageMetadata") or {}
        prompt = int(usage.get("promptTokenCount") or 0)
        answered = int(usage.get("candidatesTokenCount") or 0)
        thinking = int(usage.get("thoughtsTokenCount") or 0)
        return Reply(
            _read(body),
            prompt,
            answered,
            thinking,
            usd_for(prompt, answered, thinking, self._rates),
            int((time.perf_counter() - started) * 1000),
        )


def _read(body: dict[str, Any]) -> dict[str, Any] | None:
    """The JSON object in the reply, or None. Unreadable is still paid for."""
    try:
        parts = body["candidates"][0]["content"]["parts"]
        data = json.loads("".join(str(part.get("text") or "") for part in parts))
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None
