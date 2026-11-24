"""POST /api/v1/guided/* - the guided call, one tap at a time.

Plain requests rather than a socket. A tap is a question and an answer, the
server holds what little state there is, and nothing is streamed - so a
socket would be a connection held open for minutes in order to carry five
small messages.

Every body is bounded and patterned before it reaches the flow, and what
reaches the flow is still only ever the id of a button: `engine.answer`
checks it against the buttons it would draw. There is no free text on this
surface at all, which is most of why it cannot be made to spend money.

Three limits, per client, loosest to tightest: taps, sessions, thinks. The
governor inside the flow is what holds the rupees; these hold the load on
the catalogue and the estate, which are Poorvika's own and not free to them.
"""

from __future__ import annotations

import hashlib
import json
import re
import logging
from urllib.parse import urlsplit

import httpx
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from src.agent.tools.catalogue import CatalogueUnavailable
from src.config import Config
from src.guided.flow import Flow
from src.guided.local_speech import bounded_audio
from src.guided.script import line
from src.guided.hear import MOST_BYTES
from src.guided.sessions import UnknownSession
from src.guided.store import ReusedRequest
from src.guided.voice import RECORDINGS
from src.observability.metrics import METRICS
from src.web.ratelimit import RateLimiter
from src.web.services.dependencies import guided_flow

log = logging.getLogger(__name__)

TAPS_PER_MINUTE = 240
SESSIONS_MULTIPLIER = 5
THINKS_PER_MINUTE = 12
HEARS_PER_MINUTE = 20
QUESTIONS_PER_MINUTE = 30

# What `secrets.token_urlsafe(16)` produces, and nothing else.
SessionId = Annotated[
    str, Field(min_length=16, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
]


class Turn(BaseModel):
    session: SessionId


class Tap(Turn):
    # A question id. Digits allowed after the first letter: the purpose
    # tree's third level is "use2", and a letters-only pattern refused every
    # tap on it - the owner hit that on work -> IT -> the role question.
    question: str = Field(min_length=1, max_length=24, pattern=r"^[a-z_][a-z0-9_]*$")
    # A button id: a shelf, a band, a brand as the catalogue spells it.
    choice: str = Field(min_length=1, max_length=80)


class Chosen(Turn):
    ref: str = Field(pattern=r"^[1-9][0-9]?$")


class Refined(Turn):
    kind: str = Field(pattern=r"^(cheaper|more|next|list)$")


class Said(Turn):
    # One utterance as the browser transcribed it. Bounded like everything
    # else; it feeds a string matcher and, on a card, the grounded answerer.
    text: str = Field(min_length=1, max_length=300)
    # Typed into the Guided box, or heard by the browser. Same path, same
    # price (none); counted apart so the meter shows which.
    kind: str = Field(default="speech", pattern=r"^(speech|text)$")


class Asked(Turn):
    # The id of one of the question buttons under a card. Not the question:
    # nothing a customer typed is accepted anywhere on this surface.
    question: str = Field(pattern=r"^[a-z_]{2,24}$")


# Where the recorded lines are fetched from. Not under /static: that mount is
# registered first and would answer for anything beneath it.
VOICE_PATH = "/guided/voice"


def install(app: FastAPI, settings: Config) -> None:
    # The recorded questions, as files. Every view names the one it wants by
    # key, and only if the recording still says what the screen says - see
    # `src/guided/voice.py`. check_dir is off so that a checkout with nothing
    # recorded yet starts, and simply plays nothing.
    app.mount(
        VOICE_PATH,
        StaticFiles(directory=RECORDINGS, check_dir=False),
        name="guided-voice",
    )

    sessions = RateLimiter(settings.max_calls_per_minute * SESSIONS_MULTIPLIER)
    taps = RateLimiter(TAPS_PER_MINUTE)
    thinks = RateLimiter(THINKS_PER_MINUTE)
    hears = RateLimiter(HEARS_PER_MINUTE)
    questions = RateLimiter(QUESTIONS_PER_MINUTE)

    @app.on_event("startup")
    async def warm_the_shelves() -> None:
        """Build every shelf before the first customer, not under them.

        The first phone tap of the day cost its customer six cold seconds.
        A spinner on the very first answer is the whole first impression,
        so the burst of catalogue and stock calls happens at boot instead.
        """
        if not hasattr(app.state, "guided"):
            app.state.guided = guided_flow(settings)
        flow: Flow | None = app.state.guided
        if flow is not None:
            flow.warm()

    def flow_for(request: Request) -> Flow:
        # Built on the first request rather than at startup: building it
        # loads the estate, and a process that never serves a guided call
        # should not pay for that. Tests put their own here beforehand.
        state = request.app.state
        if not hasattr(state, "guided"):
            state.guided = guided_flow(settings)
        if state.guided is None:
            raise HTTPException(status_code=503, detail=line("say_down"))
        flow: Flow = state.guided
        return flow

    def admit(limiter: RateLimiter, request: Request) -> None:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            log.warning("guided refused: %s is over the limit", client)
            raise HTTPException(status_code=429, detail=line("say_slow_down"))

    async def answered(step: Any) -> dict[str, Any]:
        """Run one step of the flow, and say its two failures in Tamil."""
        try:
            view: dict[str, Any] = await step
        except ReusedRequest:
            raise HTTPException(
                409, "Request ID reused for a different action"
            ) from None
        except UnknownSession:
            raise HTTPException(status_code=404, detail=line("say_expired")) from None
        except CatalogueUnavailable as down:
            log.warning("guided: catalogue unavailable: %s", down)
            raise HTTPException(status_code=503, detail=line("say_down")) from None
        METRICS.count("guided_steps_total", "Guided steps answered")
        if view.get("how", {}).get("inr"):
            # The only step that can cost anything, counted when it did.
            METRICS.count("guided_paid_thinks_total", "Guided recommendations paid for")
        return view

    def journey_turn(
        request: Request, method: str, sid: str, *args: Any
    ) -> Awaitable[dict[str, Any]]:
        key = request.headers.get("Idempotency-Key", "")
        if key and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", key):
            raise HTTPException(422, "Invalid request ID")
        material = [a.hex() if isinstance(a, bytes) else a for a in args]
        signature = hashlib.sha256(json.dumps([method, *material]).encode()).hexdigest()
        return flow_for(request).turn(method, sid, args, key, signature)

    async def verified_account(request: Request) -> bool:
        user = request.scope.get("user")
        if getattr(user, "is_authenticated", False) and getattr(
            user, "is_verified", False
        ):
            return True
        token = request.headers.get("authorization", "")
        if (
            not token.startswith("Bearer ")
            or urlsplit(settings.guided_userinfo_url).scheme != "https"
        ):
            return False
        try:
            async with httpx.AsyncClient(timeout=2, follow_redirects=False) as client:
                response = await client.get(
                    settings.guided_userinfo_url, headers={"Authorization": token}
                )
                response.raise_for_status()
                info = response.json()
                return bool(
                    isinstance(info, dict)
                    and info.get("sub")
                    and (
                        info.get("email_verified") is True
                        or info.get("phone_number_verified") is True
                    )
                )
        except (ValueError, httpx.HTTPError):
            return False

    @app.post("/api/v1/guided/start")
    async def start(request: Request) -> dict[str, Any]:
        admit(sessions, request)
        return await answered(
            flow_for(request).start(verified=await verified_account(request))
        )

    @app.post("/api/v1/guided/answer")
    async def answer(tap: Tap, request: Request) -> dict[str, Any]:
        admit(taps, request)
        return await answered(
            journey_turn(request, "answer", tap.session, tap.question, tap.choice)
        )

    @app.post("/api/v1/guided/back")
    async def back(turn: Turn, request: Request) -> dict[str, Any]:
        admit(taps, request)
        return await answered(journey_turn(request, "back", turn.session))

    @app.post("/api/v1/guided/recommend")
    async def recommend(turn: Turn, request: Request) -> dict[str, Any]:
        admit(thinks, request)
        return await answered(journey_turn(request, "recommend", turn.session))

    @app.post("/api/v1/guided/pick")
    async def pick(chosen: Chosen, request: Request) -> dict[str, Any]:
        admit(taps, request)
        return await answered(journey_turn(request, "pick", chosen.session, chosen.ref))

    @app.post("/api/v1/guided/refine")
    async def refine(refined: Refined, request: Request) -> dict[str, Any]:
        admit(taps, request)
        return await answered(
            journey_turn(request, "refine", refined.session, refined.kind)
        )

    @app.post("/api/v1/guided/ask")
    async def ask(asked: Asked, request: Request) -> dict[str, Any]:
        admit(questions, request)
        return await answered(
            journey_turn(request, "ask", asked.session, asked.question)
        )

    @app.get("/api/v1/guided/speech/{key}.wav", include_in_schema=False)
    async def speech(
        request: Request,
        key: str = Path(pattern=r"^[0-9a-f]{32}$"),
    ) -> Response:
        """A sentence built from live data, in the call's voice.

        Waits while it is being made. 204 means "nothing to play" - it was
        refused by the budget, or the voice did not say it as written - and
        the page simply moves on: the words are already on screen.
        """
        admit(taps, request)
        audio = await flow_for(request).speech(key)
        if audio is None:
            return Response(status_code=204)
        return Response(content=audio, media_type="audio/wav")

    @app.post("/api/v1/guided/said")
    async def said(spoken_words: Said, request: Request) -> dict[str, Any]:
        """What the BROWSER heard, free, as text. Code presses the buttons.

        No model can be reached from here before a product is picked, so
        the whole question phase stays at the owner's zero. After a pick,
        unmatched words become one governed question about that product.
        """
        admit(taps, request)
        return await answered(
            journey_turn(
                request,
                "heard_text",
                spoken_words.session,
                spoken_words.text,
                spoken_words.kind,
            )
        )

    @app.post("/api/v1/guided/transcribe")
    async def transcribe(
        request: Request,
        session: str = Query(min_length=16, max_length=32, pattern=r"^[A-Za-z0-9_-]+$"),
        lang: str = Query(pattern="^(ta|en|auto)$"),
    ) -> dict[str, str]:
        admit(hears, request)
        if not await flow_for(request).has_session(session):
            raise HTTPException(404, line("say_expired"))
        audio = await bounded_audio(request)
        try:
            async with httpx.AsyncClient(timeout=18, trust_env=False) as client:
                response = await client.post(
                    settings.guided_asr_url,
                    params={"lang": lang},
                    content=audio,
                    headers={"Content-Type": "application/octet-stream"},
                )
                response.raise_for_status()
                text = response.json()["text"]
                if not isinstance(text, str):
                    raise ValueError("invalid transcript")
                return {"text": text[:300]}
        except (KeyError, ValueError, httpx.HTTPError):
            raise HTTPException(503, line("say_tap", lang)) from None

    @app.post("/api/v1/guided/hear")
    async def hear(
        request: Request,
        session: str = Query(min_length=16, max_length=32, pattern=r"^[A-Za-z0-9_-]+$"),
    ) -> dict[str, Any]:
        """What the customer said: raw 16-bit mono PCM at 16 kHz, eight seconds at most.

        Read with a cap rather than trusted to a header: a body that runs
        past eight seconds of audio is refused before the rest of it is
        read, so this cannot be made to hold a large upload in memory.
        """
        admit(hears, request)
        pcm = bytearray()
        async for chunk in request.stream():
            pcm.extend(chunk)
            if len(pcm) > MOST_BYTES:
                raise HTTPException(status_code=413, detail=line("say_again"))
        return await answered(journey_turn(request, "hear", session, bytes(pcm)))
