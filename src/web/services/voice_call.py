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
from src.observability.reporting import Reporter
from src.voice.budget import BudgetExceeded, SessionBudget
from src.voice.gemini_live import GeminiLiveSession
from src.voice.handover import note
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

    # -- helpers ------------------------------------------------------------

    def _minutes(self, floor: bool = False) -> float:
        elapsed = (time.monotonic() - self._opened_at) / 60
        return max(elapsed, 1 / 60) if floor else elapsed

    async def _push(self, payload: dict[str, Any]) -> None:
        """Send one update, then enforce the budget if a turn just ended."""
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

    def _model(self, tools: ToolSession) -> RotatingLiveSession:
        """One conversation, however many sockets it takes.

        The rotation threshold lives in config and zero turns it off, in
        which case this is one socket for the whole call - the behaviour it
        replaces. What crosses a rotation is `handover.note`: a few
        sentences composed from ToolSession, never asked of the model.
        """
        declarations = dependencies.declarations_for(self._settings)
        runner = self._tool_runner(tools)
        return RotatingLiveSession(
            lambda: GeminiLiveSession(
                self._settings, tools=declarations, on_tool_call=runner
            ),
            lambda: note(tools),
            rotate_above_audio_tokens=self._settings.rotate_above_audio_tokens,
        )

    async def run(self) -> None:
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
                if model.rotations and self._talking is not None:
                    self._talking.trace.record("rotations", count=model.rotations)
                await self._send_summary(model.usage)
        except WebSocketDisconnect:
            log.info("browser disconnected")
        except Exception as failed:
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
        if self._talking is not None:
            saved = self._talking.trace.save(TRACES)
            log.info("call trace written to %s", saved)
            with contextlib.suppress(Exception):
                await self._socket.send_json(
                    {"type": "trace_saved", "trace_file": saved.name}
                )
        await self._source.close()
        await self._sink.close()
