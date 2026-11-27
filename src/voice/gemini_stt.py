"""Gemini Flash-Lite transcription for one completed microphone utterance."""

from __future__ import annotations

import httpx

from src.guided.listen import Ear
from src.voice.whisper import Heard, seconds_of, to_wav


class GeminiEar:
    def __init__(self, api_key: str, model: str, language: str) -> None:
        self._api_key = api_key
        self._model = model
        self._language = language

    async def hear(self, pcm: bytes, *, rate: int) -> Heard:
        seconds = seconds_of(pcm, rate)
        async with httpx.AsyncClient() as client:
            ear = Ear(self._api_key, self._model, poster_factory=lambda: client)
            text, usd = await ear.transcribe(to_wav(pcm, rate), self._language)
        return Heard(
            text=text,
            seconds=seconds,
            billed_seconds=0,
            model=self._model,
            token_usd=usd,
        )
