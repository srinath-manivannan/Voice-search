"""The Live model hears and thinks; Grok speaks.

Not the cascade. The cascade replaced the EAR, and that is what killed it:
xAI's STT cannot begin until the caller stops talking, so a turn cost the
transcription before any thinking started. Measured against the Live path on
2026-11-16:

    Live       caller stops -> agent speaks    0.9 - 3.5 s
    cascade    ear 0.75-2.1s, brain 3.5s, mouth 0.45s   ~7.0 s

and a better brain made it worse, not better: flash-lite answered in 3,470
ms and answered badly, gemini-3.8-flash answered well in 11,668 ms. There
was no setting in between.

Here the ear and the brain are untouched. The same Live session hears the
caller's audio while they are still speaking - which is the whole of its
latency advantage - and the same model decides what to say. Only the last
step changes: it is asked for TEXT instead of audio, and Grok says it.

What that is worth, on the measured call:

    agent voice, 25 s     Rs 0.79 on Live   ->   Rs 0.55 through Grok
    output rate           $12.00 /M audio   ->   $4.50 /M text
    audio history         the agent's own replies stop entering the context
                          at 25 tokens a second, so they are not re-billed
                          on every turn after

What it costs: about 450 ms, measured warm. The text is fed to the mouth as
it streams rather than at the end of the turn, so the mouth is usually
already speaking the first clause while the model is still writing the last.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from src.voice.events import AudioOut, Event, Transcript, TurnComplete
from src.voice.grok_tts import GrokMouth
from src.voice.session import VoiceModel

log = logging.getLogger(__name__)


class SpokenByGrok:
    """A `VoiceModel` whose words come from Live and whose voice does not."""

    def __init__(self, inner: VoiceModel, mouth: GrokMouth) -> None:
        self._inner = inner
        self._mouth = mouth

    @property
    def turn_id(self) -> int:
        return self._inner.turn_id

    async def send_audio(self, pcm: bytes) -> None:
        await self._inner.send_audio(pcm)

    async def end_of_speech(self) -> None:
        await self._inner.end_of_speech()

    @property
    def characters(self) -> int:
        """What the mouth has been asked to say, for the bill."""
        return self._mouth.characters

    async def events(self) -> AsyncIterator[Event]:
        async for event in self._inner.events():
            match event:
                case Transcript(text=text):
                    # Fed as it arrives, not gathered and sent at the end.
                    # The model has usually written its first clause long
                    # before its last, and waiting would add the whole
                    # generation to the mouth's own 450 ms.
                    await self._mouth.feed(text)
                    # Still passed through: the page prints what was said,
                    # and the summariser reads the same stream.
                    yield event

                case TurnComplete() as finished:
                    await self._mouth.done()
                    async for spoken in self._speak():
                        yield spoken
                    # The Live usage, unchanged. The mouth's own cost is
                    # counted in characters and priced separately - see
                    # cost.grok_tts_usd - because a character is not a token.
                    yield finished

                case _:
                    yield event

    async def _speak(self) -> AsyncIterator[Event]:
        """The mouth's audio for this turn, up to its own completion.

        The mouth's TurnComplete is swallowed: it marks the end of the
        SPEECH, and the turn is ended by the Live session's, which is the
        one carrying the usage. Emitting both would tell `Conversation` the
        turn ended twice and open the microphone a turn early.
        """
        async for event in self._mouth.events():
            match event:
                case AudioOut(pcm=pcm):
                    yield AudioOut(turn_id=self._inner.turn_id, pcm=pcm)
                case TurnComplete():
                    return
                case _:
                    continue
