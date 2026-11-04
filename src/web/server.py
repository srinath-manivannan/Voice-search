"""Browser UI for Phase 1.

One page and one WebSocket. The socket carries raw PCM both ways plus JSON for
anything the screen needs; `src/voice/web_transport.py` explains why those are
kept separate.

This exists because a conversation you cannot see is hard to judge. The CLI
reports latency after the fact; this shows which side is speaking, what the
agent said, and what each turn cost while it happens - which is what the Phase 1
measurement run actually needs.

Nothing here knows about Gemini. It wires a transport to a session, the same way
the Phase 2 telephony entry point will.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from src.agent.persona import GREETING
from src.agent.tools.catalogue import Catalogue
from src.agent.tools.declarations import tool_config
from src.agent.tools.runner import run_tool
from src.chat.gemini_text import GeminiTextModel
from src.chat.session import ChatSession
from src.config import Config, load
from src.voice.gemini_live import GeminiLiveSession
from src.voice.session import Conversation
from src.voice.budget import BudgetExceeded, SessionBudget
from src.voice.usage import SessionUsage
from src.voice.web_transport import BrowserSink, BrowserSource

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
# Gitignored. Traces hold decisions and timings, never audio or speech, but
# they are still call records and belong on the machine that made them.
TRACES = Path(__file__).resolve().parents[2] / "out" / "traces"


def create_app(config: Config | None = None) -> FastAPI:
    load_dotenv()
    settings = config or load()
    app = FastAPI(title="Poorvika voice agent")
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def no_cache(request: Request, call_next: Any) -> Response:
        """Never cache the page or its assets.

        This is a development tool that is edited while it runs. A cached
        app.js means a change appears not to work, which costs more time than
        the caching ever saves. If this is ever served to real users, the
        static mount should get proper cache headers instead.
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def config_summary() -> dict[str, Any]:
        """Only what the browser needs to open its audio devices.

        Deliberately no model names, no word caps, no thinking budget. Which
        vendor and which model Poorvika runs on is commercial information, and
        a public page has no reason to publish it - this project learned the
        reference implementation's entire stack by reading exactly such a blob
        off its page. Not repeating that mistake outward.

        The key is not here and never will be: the browser talks to this
        server, and only this server talks to the model.
        """
        return {
            "capture_sample_rate": settings.capture_sample_rate,
            "playback_sample_rate": settings.playback_sample_rate,
            "greeting": GREETING,
        }

    @app.websocket("/ws")
    async def conversation(websocket: WebSocket) -> None:
        await websocket.accept()
        source = BrowserSource(settings.capture_sample_rate)
        sink = BrowserSink(websocket, settings.playback_sample_rate)
        # Declared here so the finally can reach it however we leave.
        talking: Conversation | None = None
        budget = SessionBudget(
            max_turns=settings.max_session_turns,
            max_minutes=settings.max_session_minutes,
            max_inr=settings.max_session_inr,
        )
        opened_at = time.monotonic()
        # Set once the session exists; push() runs before that on the ready
        # message, so it has to tolerate None.
        usage: SessionUsage | None = None

        async def push(payload: dict[str, Any]) -> None:
            with contextlib.suppress(Exception):
                await websocket.send_json(payload)

            # Checked on every completed turn, which is the only moment the
            # spend figure has moved and the only clean point to stop.
            if payload.get("type") != "turn" or talking is None or usage is None:
                return
            # The re-billed reading, deliberately: until the billing console
            # settles which applies, a ceiling should assume the dearer one.
            spent = (usage.input_usd_rebilled + usage.output_usd) * settings.usd_inr
            try:
                budget.check(
                    turns=len(talking.result.turns),
                    minutes=(time.monotonic() - opened_at) / 60,
                    inr=spent,
                )
            except BudgetExceeded as stop:
                log.warning("session stopped: %s", stop.reason)
                talking.trace.record("budget_exceeded", reason=stop.reason)
                with contextlib.suppress(Exception):
                    await websocket.send_json({"type": "ended", "message": stop.spoken})
                raise

        async def read_microphone() -> None:
            """Binary is audio, JSON is control. Nothing else arrives."""
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if (pcm := message.get("bytes")) is not None:
                    source.offer(pcm)

        try:
            voice_catalogue = (
                Catalogue(
                    settings.catalogue_url,
                    settings.catalogue_token,
                    settings.catalogue_origin,
                )
                if settings.catalogue_token
                else None
            )

            async def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
                return await run_tool(voice_catalogue, name, args)

            async with GeminiLiveSession(
                settings,
                tools=tool_config() if settings.catalogue_token else None,
                on_tool_call=call_tool,
            ) as model:
                usage = model.usage
                await push({"type": "ready"})
                # Speak first. A call that answers and then says nothing until
                # the caller speaks sounds like a dead line.
                await model.greet(GREETING)
                # agent_opens: the greeting is already on its way, so the
                # agent holds the floor before its first chunk arrives.
                talking = Conversation(
                    model, source, sink, on_update=push, agent_opens=True
                )
                reader = asyncio.create_task(read_microphone(), name="browser-reader")
                runner = asyncio.create_task(talking.run(), name="conversation")

                _, pending = await asyncio.wait(
                    {reader, runner}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

                minutes = max(len(talking.result.turns) * 0.25, 1 / 60)
                cost = model.usage.report_inr(
                    settings.usd_inr, settings.gst_rate, minutes
                )
                await push(
                    {
                        "type": "summary",
                        "cost": cost,
                        "trace": talking.trace.summary(),
                    }
                )
        except WebSocketDisconnect:
            log.info("browser disconnected")
        except Exception:
            log.exception("conversation failed")
            await push(
                {"type": "error", "message": "the conversation ended unexpectedly"}
            )
        finally:
            # In the finally, not on the happy path. The calls worth having a
            # trace of are the ones that fell over, and saving it only where
            # everything went right loses exactly those.
            if talking is not None:
                saved = talking.trace.save(TRACES)
                log.info("call trace written to %s", saved)
                with contextlib.suppress(Exception):
                    await websocket.send_json(
                        {"type": "trace_saved", "trace_file": saved.name}
                    )
            await source.close()
            await sink.close()

    @app.websocket("/ws/chat")
    async def chat(websocket: WebSocket) -> None:
        """Text chat.

        A socket of its own rather than message types on the voice socket. The
        voice socket carries fifty binary audio frames a second; multiplexing a
        text conversation onto it would couple two unrelated lifecycles for no
        gain, since the UI shows one tab at a time.
        """
        await websocket.accept()
        model = GeminiTextModel(
            api_key=settings.gemini_api_key,
            model=settings.gemini_text_model,
            system_instruction=settings.chat_instruction,
            tools=tool_config() if settings.catalogue_token else None,
        )
        catalogue = (
            Catalogue(
                settings.catalogue_url,
                settings.catalogue_token,
                settings.catalogue_origin,
            )
            if settings.catalogue_token
            else None
        )
        session = ChatSession(model, catalogue=catalogue)

        try:
            await websocket.send_json({"type": "ready", "greeting": GREETING})
            while True:
                message = await websocket.receive_json()
                text = (message.get("text") or "").strip()
                if not text:
                    continue

                await websocket.send_json({"type": "start"})
                async for delta in session.send(text):
                    await websocket.send_json({"type": "delta", "text": delta})

                record = session.result.turns[-1] if session.result.turns else None
                await websocket.send_json(
                    {
                        "type": "done",
                        "turn": record.turn if record else None,
                        "prompt_tokens": record.prompt_tokens if record else 0,
                        "response_tokens": record.response_tokens if record else 0,
                        "thinking_tokens": record.thinking_tokens if record else 0,
                        "conversation_inr": session.usage.input_usd_rebilled
                        * settings.usd_inr
                        + session.usage.output_usd * settings.usd_inr,
                    }
                )
        except WebSocketDisconnect:
            log.info("chat disconnected")
        except Exception:
            log.exception("chat failed")
            with contextlib.suppress(Exception):
                await websocket.send_json(
                    {"type": "error", "message": "that message could not be answered"}
                )

    return app


app = create_app
