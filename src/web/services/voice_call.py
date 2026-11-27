"""One voice call, from accept to trace.

This is the business logic the `/ws` route used to hold inline: budget
enforcement, the cost payload, tool tracing, and the order the pieces are
started and stopped in. It knows about a websocket and about Gemini; the
route above it knows about neither, and the transport seam below it means
Phase 2 swaps a telephony source and sink in without touching this file.

The ordering rules worth keeping in mind:

- the greeting is sent BEFORE the conversation object exists, and the
  conversation is told the agent already holds the floor. A call that accepts
  and then says nothing sounds like a dead line.
- the trace is written in a `finally`. The calls worth having a trace of are
  the ones that fell over, and saving it only on the happy path loses exactly
  those.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import AsyncExitStack
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from src.agent.persona import GREETING
from src.config import Config
from src.observability.alerts import announce
from src.observability.metrics import (
    LATENCY_BUCKETS_MS,
    METRICS,
    RUPEE_BUCKETS,
)
from src.observability.reporting import Reporter
from src.voice.active_minute import ActiveClock
from src.voice.budget import BudgetExceeded, SessionBudget
from src.voice.guard import worst_case_inr
from src.voice.route_b import HashCachedMouth, RoutedBrain
from src.agent.prefetch import Prefetcher
from src.voice.gemini_stt import GeminiEar
from src.voice.grok_tts import GrokMouth
from src.voice.spoken_by_grok import SpokenByGrok
from src.voice.opening import HOLDING_RECORDING, SpeaksTheOpening, load_recording
from src.memory.grok import GrokClient
from src.memory.summariser import CallMemory
from src.voice.gemini_live import GeminiLiveSession
from src.voice.handover import note, with_memory
from src.voice.rotating import RotatingLiveSession
from src.chat.gemini_text import GeminiTextModel
from src.chat.session import ChatSession
from src.voice.cascade import MOUTH_TEMPERATURE, SPEAK_ONLY, CascadeSession, Ear
from src.voice.grok_stt import GrokEar
from src.voice.session import Conversation, VoiceModel
from src.voice.shadow import ShadowEar
from src.voice.trace import CallTrace
from src.voice.usage import SessionUsage
from src.voice.whisper import GroqWhisper
from src.agent.tools.runner import ToolSession
from src.voice.web_transport import BrowserSink, BrowserSource
from src.web.services import dependencies
from src.web.services.cost_report import report, spent_inr

# The two shapes a call can take. Not a protocol: there are exactly two, they
# are both named here, and a union says which they are where a protocol would
# only say what they have in common.
CallModel = RotatingLiveSession | CascadeSession

# Brand names the catalogue carries, handed to the ear as bias. These are the
# words a mis-hearing costs most on: a phone nobody stocks cannot be searched
# for. The assistant's own name is not one of them.
CATALOGUE_TERMS = (
    "Redmi",
    "Xiaomi",
    "Poco",
    "OPPO",
    "Realme",
    "OnePlus",
    "Samsung",
    "Vivo",
    "iPhone",
    "Apple",
    "Motorola",
    "Nothing",
    "EMI",
)

log = logging.getLogger(__name__)


def remember(memory: CallMemory | None, payload: dict[str, Any]) -> None:
    """Copy a screen update into the summariser, when memory is on.

    Caller text arrives once, already joined, as `caller_transcript`.
    Agent text still arrives in chunks. Neither call waits on Grok.
    """
    if memory is None:
        return
    if payload.get("type") == "transcript":
        turn_id = payload.get("turn_id")
        memory.add_agent(
            str(payload.get("text") or ""),
            turn_id if isinstance(turn_id, int) else None,
        )
    elif payload.get("type") == "caller_transcript":
        memory.add_caller(str(payload.get("text") or ""))


# Gitignored. Traces hold decisions and timings, never audio or speech, but
# they are still call records and belong on the machine that made them.
TRACES = Path(__file__).resolve().parents[3] / "out" / "traces"


class _Routed:
    """A tool session whose calls go through one given runner."""

    def __init__(
        self, run: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
    ) -> None:
        self._run = run

    async def run(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return await self._run(name, args)


class VoiceCall:
    """Everything one browser call does, with the socket already accepted."""

    def __init__(
        self, websocket: WebSocket, settings: Config, reporter: Reporter
    ) -> None:
        self._socket = websocket
        self._settings = settings
        self._reporter = reporter
        self._source = BrowserSource(settings.capture_sample_rate)
        self._sink = BrowserSink(websocket, settings.playback_sample_rate)
        self._budget = SessionBudget(
            max_turns=settings.max_session_turns,
            max_minutes=settings.max_session_minutes,
            max_inr=settings.max_session_inr,
            max_inr_per_minute=settings.max_inr_per_minute,
        )
        self._opened_at = time.monotonic()
        self._talking: Conversation | None = None
        # Set once the session exists; push() runs before that on the ready
        # message, so it has to tolerate None.
        self._usage: SessionUsage | None = None
        # Kept so a failure can report how far the call got.
        self._live: CallModel | None = None
        # None when memory is off. The summary tasks are the Grok calls
        # kicked after a reply; they are cancelled with the call.
        self._memory = self._open_memory()
        self._summaries: set[asyncio.Task[None]] = set()
        # Built in `_model`, once there is a ToolSession to search with.
        self._prefetch: Prefetcher | None = None
        # Set when the cascade speaks through Grok; read for the bill.
        self._grok_mouth: GrokMouth | None = None
        # One trace for the call, made here rather than by `Conversation`,
        # because the shadow ear writes to it and exists before the
        # conversation does.
        self._trace = CallTrace()
        self._shadow = self._open_shadow()
        # Silence is not recorded here, so it does not earn a rupee.
        self._active = ActiveClock()
        self._active_mark = time.monotonic()

    # -- helpers ------------------------------------------------------------

    def _minutes(self, floor: bool = False) -> float:
        elapsed = (time.monotonic() - self._opened_at) / 60
        return max(elapsed, 1 / 60) if floor else elapsed

    @property
    def spent_inr(self) -> float:
        """What this call cost, GST in - for the day's ceiling."""
        return (
            spent_inr(self._usage, self._settings) if self._usage is not None else 0.0
        )

    async def _push(self, payload: dict[str, Any]) -> None:
        """Send one update, then enforce the budget if a turn just ended."""
        if payload.get("type") == "turn":
            first_audio = payload.get("first_audio_ms")
            if isinstance(first_audio, (int, float)):
                METRICS.observe(
                    "voice_first_audio_ms",
                    "End of speech to the first byte of the reply",
                    float(first_audio),
                    LATENCY_BUCKETS_MS,
                )
        self._note_active(payload)
        remember(self._memory, payload)
        if (
            payload.get("type") == "turn"
            and payload.get("caller_reply")
            and self._memory is not None
            and self._memory.caller_reply_finished()
        ):
            # After the reply. The task is not awaited here: awaiting it
            # would hold the next audio frame behind a Grok round trip.
            self._kick_summary()
        if payload.get("type") == "turn" and self._usage is not None:
            # The whole bill, broken out per charge line, live - rather than
            # one number after the call has ended, which is too late to tell
            # you which line to go and fix.
            payload["cost"] = report(
                self._usage, self._settings, self._minutes(True), self._spoken_chars()
            )

        with contextlib.suppress(Exception):
            await self._socket.send_json(payload)

        # Checked on every completed turn, which is the only moment the spend
        # figure has moved and the only clean point to stop.
        if payload.get("type") != "turn":
            return
        if self._talking is None or self._usage is None:
            return
        await self._enforce_budget()

    async def _enforce_budget(self) -> None:
        assert self._talking is not None and self._usage is not None
        minutes = self._minutes()
        try:
            self._budget.check(
                turns=len(self._talking.result.turns),
                minutes=minutes,
                inr=spent_inr(self._usage, self._settings),
                # The commercial ceiling: every line of the bill, GST in, per
                # connected minute. Watching the audio streams alone would
                # have waved through the call that cost Rs 9 a minute,
                # because only Rs 1.39 of it was audio.
                inr_per_minute=self._usage.inr_per_minute(
                    self._settings.usd_inr, self._settings.gst_rate, minutes
                ),
            )
            self._guard_active_minute()
        except BudgetExceeded as stop:
            await self._stop_for_budget(stop)
            raise

    def _note_active(self, payload: dict[str, Any]) -> None:
        """Count speech. A quiet gap is left off the clock on purpose."""
        now = time.monotonic()
        kind = payload.get("type")
        if kind == "caller_transcript":
            self._active.shopper_spoke(self._active_mark, now)
            self._active_mark = now
        elif kind in {"transcript", "turn"}:
            self._active.assistant_spoke(self._active_mark, now)
            self._active_mark = now

    def _guard_active_minute(self) -> None:
        """₹2 per active minute, GST in, reserved before the next paid step."""
        if not self._settings.voice_route_b_enabled or self._usage is None:
            return
        spent = self.spent_inr
        active = self._active.active_minutes()
        if spent > worst_case_inr(active):
            raise BudgetExceeded(
                "active-minute guard",
                "This call has reached its budget. You can continue in Chat.",
            )
        if active >= 1.0 and spent / active > 2.0:
            raise BudgetExceeded(
                "rupee per active minute",
                "This call has reached its budget. You can continue in Chat.",
            )

    async def _stop_for_budget(self, stop: BudgetExceeded) -> None:
        METRICS.count(
            "voice_calls_stopped_by_budget_total",
            "Calls a session ceiling ended mid-conversation",
        )
        log.warning("session stopped: %s", stop.reason)
        if self._talking is not None:
            self._talking.trace.record("budget_exceeded", reason=stop.reason)
        with contextlib.suppress(Exception):
            await self._socket.send_json({"type": "ended", "message": stop.spoken})

    async def _read_microphone(self) -> None:
        """Binary is audio, JSON is control. Nothing else arrives."""
        while True:
            message = await self._socket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if (pcm := message.get("bytes")) is not None:
                self._source.offer(pcm)

    def _tool_runner(
        self, tools: ToolSession
    ) -> Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]:
        """Run a tool, and put the decision in the trace.

        The first call trace of a conversation with tools showed six turns,
        their latencies and not one tool call - so whether the agent checked
        stock or answered from memory was invisible, which is the only thing
        worth knowing about those turns. What is recorded is the decision and
        the shape of the answer, never the payload: a trace is a call record,
        not a copy of the catalogue.
        """

        async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
            started = time.perf_counter()
            if name == "search_products" and self._prefetch is not None:
                await self._prefetch.before_search(args)
            result = await tools.run(name, args)
            if self._talking is not None:
                self._talking.trace.record(
                    "tool_call",
                    tool=name,
                    args=args,
                    took_ms=round((time.perf_counter() - started) * 1000),
                    outcome=dependencies.tool_outcome(result),
                )
            return result

        return call_tool

    # -- the call -----------------------------------------------------------

    def _open_memory(self) -> CallMemory | None:
        """The summariser, or nothing when the flag or the key is absent.

        A missing key does not refuse the call. The voice still works, and
        the handover stays the tool note, which is what `GROK_MEMORY` off
        already does.
        """
        settings = self._settings
        if settings.voice_gemini_only or not settings.grok_memory_enabled:
            return None
        if not settings.xai_api_key:
            log.warning(
                "GROK_MEMORY is on but XAI_API_KEY is empty; summaries stay off"
            )
            return None
        return CallMemory(
            GrokClient(
                settings.xai_api_key,
                settings.grok_model,
                timeout_s=settings.grok_timeout_seconds,
            ),
            every=settings.grok_summary_every_turns,
            timeout_s=settings.grok_timeout_seconds,
            on_usage=self._charge_grok,
        )

    def _ear(self) -> Ear | None:
        """Whoever can transcribe, preferring the key this project already has.

        xAI first, because `XAI_API_KEY` is already set for the summariser,
        so the cascade needs no second vendor and no second account. Groq's
        Whisper is the alternative and the two are interchangeable behind
        `cascade.Ear` - one letter apart in the name, and neither key is a
        fallback for the other.
        """
        settings = self._settings
        if settings.voice_gemini_only:
            return GeminiEar(
                settings.gemini_api_key,
                settings.gemini_text_model,
                settings.stt_language,
            )
        if settings.xai_api_key:
            return GrokEar(
                settings.xai_api_key,
                settings.stt_model,
                language=settings.stt_language,
                keyterms=CATALOGUE_TERMS,
                timeout_s=settings.whisper_timeout_seconds,
            )
        if settings.groq_api_key:
            return GroqWhisper(
                settings.groq_api_key,
                settings.whisper_model,
                timeout_s=settings.whisper_timeout_seconds,
            )
        return None

    def _open_cascade(
        self, tools: ToolSession, listener: Callable[[str], None] | None
    ) -> CascadeSession | None:
        """The whole conversation off native audio, or None to stay as we are.

        Same rule as the other two side models: the flag alone is not enough,
        a missing key logs and falls back rather than refusing the call. The
        fallback here is the entire Live path, which is the one that works.
        """
        settings = self._settings
        route_b = settings.voice_route_b_enabled or settings.cascade_enabled
        if not route_b:
            return None
        factory = getattr(self._socket.app.state, "cascade_factory", None)
        if factory is not None:
            made = factory(tools, listener)
            return made if isinstance(made, CascadeSession) else None
        ear = self._ear()
        if ear is None:
            log.warning(
                "Route B is on but no ear is configured; native audio stays off "
                "unless Route A is the selected path"
            )
            return None

        brain: Any = ChatSession(
            GeminiTextModel(
                api_key=settings.gemini_api_key,
                model=settings.gemini_text_model,
                system_instruction=settings.system_instruction,
                tools=dependencies.declarations_for(settings),
            ),
            # The same traced runner as the Live path. Handing the brain the
            # bare ToolSession left its searches out of the trace, and let it
            # search without waiting for the prefetch that was already running.
            tools=_Routed(self._tool_runner(tools)),
        )
        mouth: Any = self._mouth()
        if settings.voice_route_b_enabled:
            # The Gemini conversation keeps each shopper turn in its history so a
            # stock or EMI follow-up resolves against the preceding shortlist.
            if not settings.voice_gemini_only:
                brain = RoutedBrain(
                    brain,
                    route_b=True,
                    route_a=settings.voice_route_a_enabled,
                )
            mouth = HashCachedMouth(mouth)
        return CascadeSession(
            ear,
            brain,
            mouth,
            rate=settings.capture_sample_rate,
            ear_timeout_s=settings.whisper_timeout_seconds,
            on_caller_speech=listener,
        )

    def _grok_is_the_voice(self) -> bool:
        """Whether Grok says the agent's words, on whichever path.

        One flag, one meaning. On the cascade it replaces the second Gemini
        Live socket; on the Live path it takes the model's text and speaks
        it. Both need the key, and a missing key logs and falls back rather
        than refusing the call.
        """
        settings = self._settings
        if settings.voice_gemini_only or not settings.grok_mouth_enabled:
            return False
        if not settings.xai_api_key:
            log.warning("GROK_MOUTH is on but XAI_API_KEY is empty; using Gemini")
            return False
        if not settings.cascade_enabled and not settings.voice_route_b_enabled:
            # Only on the cascade, and not by choice.
            #
            # Having Live hear and think and Grok speak was the right shape:
            # it kept the ear that makes this fast and swapped only the
            # expensive half. The model refuses:
            #
            #   1007 The requested combination of response modalities (TEXT)
            #        is not supported by the model
            #
            # gemini-3.8-live is native-audio. It has no text output to take.
            # The socket closed on setup and the call died before the
            # greeting, so this is a refusal here rather than a dead call.
            log.warning(
                "GROK_MOUTH needs CASCADE: %s is native-audio and cannot "
                "answer in text. Using the Gemini voice.",
                settings.gemini_live_model,
            )
            return False
        return True

    async def _let_the_prefetch_land(self) -> None:
        """Hold the turn for a prefetch in flight, up to the configured cap.

        Does nothing when there is no prefetch running, which is most turns:
        `settle` returns at once unless a task is actually in flight.
        """
        wait = self._settings.prefetch_wait_seconds
        if self._prefetch is None or wait <= 0:
            return
        await self._prefetch.settle(wait)

    def _spoken_chars(self) -> int:
        """Characters the Grok mouth has been asked to say, for the bill.

        Zero on the Live path, which is billed in audio tokens and already
        counted in SessionUsage. Two units in one table is why the line is
        labelled rather than folded into the Grok memory row.
        """
        return self._grok_mouth.characters if self._grok_mouth is not None else 0

    def _mouth(self) -> GrokMouth | RotatingLiveSession | GeminiLiveSession:
        """Who says the brain's reply out loud.

        Grok where the flag and the key are both there, a rotating Gemini Live
        socket otherwise. Rotating the mouth drops accumulated audio context
        so speaking stays under the ₹1.50/min target instead of growing with turns.
        """
        settings = self._settings
        if self._grok_is_the_voice():
            return self._new_grok_mouth()
        return RotatingLiveSession(
            lambda: GeminiLiveSession(
                settings, instruction=SPEAK_ONLY, temperature=MOUTH_TEMPERATURE
            ),
            lambda: "",
            rotate_above_audio_tokens=150,
        )

    def _new_grok_mouth(self) -> GrokMouth:
        settings = self._settings
        self._grok_mouth = GrokMouth(
            settings.xai_api_key,
            rate=settings.playback_sample_rate,
            language=settings.stt_language or "ta",
        )
        return self._grok_mouth

    def _open_shadow(self) -> ShadowEar | None:
        """Whisper on Groq, or nothing when the flag or the key is absent.

        Same rule as the summariser: a missing key logs and stays off rather
        than refusing the call. This one answers nobody, so a deploy that
        forgot the key loses a measurement, not a customer.
        """
        settings = self._settings
        if settings.voice_gemini_only or not settings.whisper_compare_enabled:
            return None
        if not settings.groq_api_key:
            log.warning(
                "WHISPER_COMPARE is on but GROQ_API_KEY is empty; "
                "the second ear stays off"
            )
            return None
        return ShadowEar(
            GroqWhisper(
                settings.groq_api_key,
                settings.whisper_model,
                timeout_s=settings.whisper_timeout_seconds,
            ),
            self._trace,
            rate=settings.capture_sample_rate,
            timeout_s=settings.whisper_timeout_seconds,
        )

    def _charge_grok(
        self, prompt: int, cached: int, completion: int, model: str
    ) -> None:
        if self._usage is not None:
            self._usage.add_grok(prompt, cached, completion, model)

    def _kick_summary(self) -> None:
        if self._memory is None:
            return
        task = asyncio.create_task(self._run_summary(), name="grok-summary")
        self._summaries.add(task)
        task.add_done_callback(self._summaries.discard)

    async def _run_summary(self) -> None:
        assert self._memory is not None
        await self._memory.summarise()
        if self._usage is None:
            return
        await self._push(
            {
                "type": "cost",
                "cost": report(
                    self._usage,
                    self._settings,
                    self._minutes(True),
                    self._spoken_chars(),
                ),
            }
        )

    async def _drain_summaries(self) -> None:
        """Let an in-flight summary land before the end-of-call total.

        Bounded by the Grok timeout. The caller has already hung up or the
        pumps have stopped, so this wait is not a silence on the line.
        """
        if not self._summaries:
            return
        await asyncio.wait(
            set(self._summaries),
            timeout=self._settings.grok_timeout_seconds + 0.5,
        )

    def _model(self, tools: ToolSession) -> CallModel:
        """One conversation, however many sockets it takes.

        The rotation threshold lives in config and zero turns it off, in
        which case this is one socket for the whole call - the behaviour it
        replaces. What crosses a rotation is the tool note, plus Grok's
        summary when one has landed. The summary is never asked for here.
        """
        declarations = dependencies.declarations_for(self._settings)
        runner = self._tool_runner(tools)
        memory = self._memory

        async def prime(text: str) -> None:
            # Resolved when the prefetch lands rather than captured now: the
            # session does not exist until this method returns, and by then
            # the factory below has already been handed the listener.
            if self._live is not None:
                await self._live.prime(text)

        # Only where there is a catalogue to search. Without one the search
        # tool is not even declared, so there is nothing to get ahead of.
        self._prefetch = (
            Prefetcher(tools, prime, on_event=self._trace.record)
            if declarations
            else None
        )
        listener = self._prefetch.heard if self._prefetch else None

        def carry() -> str:
            # Read at rotation time, which is after a reply, so a summary
            # that has already landed is included. One that has not yet
            # returned leaves the tool note, which is the fail-safe.
            if memory is None:
                return note(tools)
            return with_memory(tools, memory.handover)

        if (cascade := self._open_cascade(tools, listener)) is not None:
            return cascade
        # Route B with Route A off must not open the native Live socket.
        if (
            self._settings.voice_route_b_enabled
            and not self._settings.voice_route_a_enabled
        ):
            raise RuntimeError("route B cascade is not available")

        return RotatingLiveSession(
            lambda: GeminiLiveSession(
                self._settings,
                tools=declarations,
                on_tool_call=runner,
                on_caller_speech=listener,
                # Text out when Grok is the voice. The ear and the brain are
                # untouched - this model still hears the caller's audio while
                # they are speaking, which is the whole reason the cascade
                # could not match it.
                speaks=not self._grok_is_the_voice(),
            ),
            carry,
            rotate_above_audio_tokens=self._settings.rotate_above_audio_tokens,
        )

    async def run(self) -> None:
        METRICS.count("voice_calls_total", "Voice calls accepted")
        try:
            tools = dependencies.tool_session(self._settings)
            async with AsyncExitStack() as open_for_the_call:
                # Annotated because AsyncExitStack widens a union to
                # `object`, and everything below asks the model for things
                # only a model has.
                model: CallModel = await open_for_the_call.enter_async_context(
                    self._model(tools)
                )
                self._live = model
                self._usage = model.usage

                # Grok says the words on the Live path too, where the model
                # was asked for text rather than audio. Not on the cascade,
                # which already has it as its mouth. The socket is opened
                # here so its ~944 ms handshake happens behind the recorded
                # greeting and never in front of a reply.
                voice: VoiceModel = model
                if self._grok_is_the_voice() and not isinstance(model, CascadeSession):
                    mouth = await open_for_the_call.enter_async_context(
                        self._new_grok_mouth()
                    )
                    voice = SpokenByGrok(model, mouth)

                await self._push({"type": "ready"})
                # Speak first. A call that answers and then says nothing
                # until the caller speaks sounds like a dead line.
                #
                # Off disk where there is a recording, out of the model where
                # there is not. The greeting is fixed text, so generating it
                # bought nothing and cost Rs 0.45 and half a second of
                # silence on the traced call - see src/voice/opening.py.
                # `make greeting` writes the recording; deleting it comes
                # back here.
                recording = load_recording()
                speaking: VoiceModel = voice
                if recording is not None:
                    opening = SpeaksTheOpening(
                        voice, recording, self._settings.playback_sample_rate
                    )
                    speaking = opening
                    # It did not generate the line, so it does not know it
                    # said it. Context, not a turn: no generation, no charge.
                    await model.opened_with(GREETING)
                    self._trace.record(
                        "opening", recorded=True, seconds=round(opening.seconds, 2)
                    )
                else:
                    await model.greet(GREETING)
                    self._trace.record("opening", recorded=False)
                # agent_opens: the greeting is already on its way, so the
                # agent holds the floor before its first chunk arrives.
                # Which architecture answered this call. Without it a trace
                # cannot be read without knowing what the .env said at the
                # time, and a call that quietly fell back to the Live path
                # because a key was missing looks exactly like one that was
                # meant to.
                self._trace.record(
                    "model",
                    path="cascade" if isinstance(model, CascadeSession) else "live",
                )
                self._talking = Conversation(
                    speaking,
                    self._source,
                    self._sink,
                    on_update=self._push,
                    agent_opens=True,
                    shadow=self._shadow,
                    trace=self._trace,
                    # The tool layer decides; this file only relays it.
                    should_end=lambda: tools.hang_up,
                    # Played when a generation comes back with no speech and
                    # the caller is still owed an answer. None where the
                    # recording is missing, which leaves the line silent -
                    # the behaviour this is fixing, not a crash.
                    holding=load_recording(HOLDING_RECORDING),
                    # Let our own search land before the model is told the
                    # caller finished. See Prefetcher.settle.
                    before_reply=self._let_the_prefetch_land,
                )
                await self._pump_until_either_ends()
                await self._drain_summaries()
                if model.rotations and self._talking is not None:
                    self._talking.trace.record("rotations", count=model.rotations)
                if self._prefetch is not None and self._prefetch.fired:
                    # Recorded so the lever can be counted the way the silent
                    # generations were: `used` means the model was handed a
                    # shortlist, not that it then refrained from searching.
                    if self._talking is not None:
                        self._talking.trace.record("prefetch", used=self._prefetch.used)
                await self._send_summary(model.usage)
        except WebSocketDisconnect:
            log.info("browser disconnected")
        except Exception as failed:
            METRICS.count("voice_calls_failed_total", "Voice calls that errored")
            self._reporter.capture(
                failed,
                where="voice",
                turns=len(self._talking.result.turns) if self._talking else 0,
                minutes=round(self._minutes(), 2),
                rotations=self._live.rotations if self._live else 0,
            )
            await self._push(
                {"type": "error", "message": "the conversation ended unexpectedly"}
            )
        finally:
            await self._close()

    async def _pump_until_either_ends(self) -> None:
        assert self._talking is not None
        reader = asyncio.create_task(self._read_microphone(), name="browser-reader")
        runner = asyncio.create_task(self._talking.run(), name="conversation")
        _, pending = await asyncio.wait(
            {reader, runner}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    async def _send_summary(self, usage: SessionUsage) -> None:
        assert self._talking is not None
        await self._push(
            {
                "type": "summary",
                "cost": report(
                    usage, self._settings, self._minutes(True), self._spoken_chars()
                ),
                "trace": self._talking.trace.summary(),
            }
        )

    async def _close(self) -> None:
        if self._usage is not None and self._minutes() > 1 / 60:
            METRICS.observe(
                "voice_rupees_per_minute",
                "Model cost of a connected minute, GST included",
                self._usage.inr_per_minute(
                    self._settings.usd_inr,
                    self._settings.gst_rate,
                    self._minutes(),
                ),
                RUPEE_BUCKETS,
            )
        if self._live is not None and self._live.rotations:
            METRICS.count(
                "voice_rotations_total",
                "Times a call moved to a fresh socket",
                self._live.rotations,
            )
        # Evaluated when a call ends, which is the only moment every number
        # it contributes is final. One ALERT line per breach, for a log
        # search to match - alerting that works before a host is chosen.
        pending: list[asyncio.Task[None] | asyncio.Task[bool]] = list(self._summaries)
        if self._prefetch is not None and self._prefetch.task is not None:
            pending.append(self._prefetch.task)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._shadow is not None:
            # Before the trace is written, so the tally is in the file, and
            # bounded as a whole rather than per utterance: teardown is not
            # allowed to sit on Groq once the caller has gone.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._shadow.close(), self._settings.whisper_timeout_seconds
                )
        announce()
        if self._talking is not None:
            saved = self._talking.trace.save(TRACES)
            log.info("call trace written to %s", saved)
            with contextlib.suppress(Exception):
                await self._socket.send_json(
                    {"type": "trace_saved", "trace_file": saved.name}
                )
        await self._source.close()
        await self._sink.close()
