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
from src.voice.budget import BudgetExceeded, SessionBudget
from src.memory.grok import GrokClient
from src.memory.summariser import CallMemory
from src.voice.gemini_live import GeminiLiveSession
from src.voice.handover import note, with_memory
from src.voice.rotating import RotatingLiveSession
from src.voice.session import Conversation
from src.voice.usage import SessionUsage
from src.agent.tools.runner import ToolSession
from src.voice.web_transport import BrowserSink, BrowserSource
from src.web.services import dependencies
from src.web.services.cost_report import report, spent_inr

log = logging.getLogger(__name__)

# Gitignored. Traces hold decisions and timings, never audio or speech, but
# they are still call records and belong on the machine that made them.
TRACES = Path(__file__).resolve().parents[3] / "out" / "traces"


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
        self._live: RotatingLiveSession | None = None
        # None when memory is off. The summary tasks are the Grok calls
        # kicked after a reply; they are cancelled with the call.
        self._memory = self._open_memory()
        self._summaries: set[asyncio.Task[None]] = set()

    # -- helpers ------------------------------------------------------------

    def _minutes(self, floor: bool = False) -> float:
        elapsed = (time.monotonic() - self._opened_at) / 60
        return max(elapsed, 1 / 60) if floor else elapsed

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
        if payload.get("type") == "transcript" and self._memory is not None:
            turn_id = payload.get("turn_id")
            self._memory.add_agent(
                str(payload.get("text") or ""),
                turn_id if isinstance(turn_id, int) else None,
            )
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
            payload["cost"] = report(self._usage, self._settings, self._minutes(True))

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
        except BudgetExceeded as stop:
            METRICS.count(
                "voice_calls_stopped_by_budget_total",
                "Calls a session ceiling ended mid-conversation",
            )
            log.warning("session stopped: %s", stop.reason)
            self._talking.trace.record("budget_exceeded", reason=stop.reason)
            with contextlib.suppress(Exception):
                await self._socket.send_json({"type": "ended", "message": stop.spoken})
            raise

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
        if not settings.grok_memory_enabled:
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
                "cost": report(self._usage, self._settings, self._minutes(True)),
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

    def _model(self, tools: ToolSession) -> RotatingLiveSession:
        """One conversation, however many sockets it takes.

        The rotation threshold lives in config and zero turns it off, in
        which case this is one socket for the whole call - the behaviour it
        replaces. What crosses a rotation is the tool note, plus Grok's
        summary when one has landed. The summary is never asked for here.
        """
        declarations = dependencies.declarations_for(self._settings)
        runner = self._tool_runner(tools)
        memory = self._memory

        def carry() -> str:
            # Read at rotation time, which is after a reply, so a summary
            # that has already landed is included. One that has not yet
            # returned leaves the tool note, which is the fail-safe.
            if memory is None:
                return note(tools)
            return with_memory(tools, memory.handover)

        return RotatingLiveSession(
            lambda: GeminiLiveSession(
                self._settings, tools=declarations, on_tool_call=runner
            ),
            carry,
            rotate_above_audio_tokens=self._settings.rotate_above_audio_tokens,
        )

    async def run(self) -> None:
        METRICS.count("voice_calls_total", "Voice calls accepted")
        try:
            tools = dependencies.tool_session(self._settings)
            async with self._model(tools) as model:
                self._live = model
                self._usage = model.usage
                await self._push({"type": "ready"})
                # Speak first. A call that answers and then says nothing
                # until the caller speaks sounds like a dead line.
                await model.greet(GREETING)
                # agent_opens: the greeting is already on its way, so the
                # agent holds the floor before its first chunk arrives.
                self._talking = Conversation(
                    model,
                    self._source,
                    self._sink,
                    on_update=self._push,
                    agent_opens=True,
                )
                await self._pump_until_either_ends()
                await self._drain_summaries()
                if model.rotations and self._talking is not None:
                    self._talking.trace.record("rotations", count=model.rotations)
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
                "cost": report(usage, self._settings, self._minutes(True)),
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
        pending = list(self._summaries)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
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
