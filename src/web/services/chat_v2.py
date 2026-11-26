"""Chat protocol v2: one socket per intentional send, spend that survives it.

The browser opens this socket only when the shopper sends. A ``hello`` resumes
the conversation or starts one, and replays anything the previous socket did
not see. A ``message_id`` is claimed once. Repeating it does not call the
model again, and repeating it with different text is refused.

Every model round reserves its worst case before the call and settles the
actual usage after, including the rounds that only exist to run a tool.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, cast

from fastapi import WebSocket, WebSocketDisconnect

from src.chat.allowlist import (
    guard_display,
    neutral_fallback,
    sanitise_tool_result,
)
from src.chat.copy import LIMIT_REACHED, SERVICE_ERROR, UNAVAILABLE
from src.chat.gemini_text import GeminiTextModel
from src.chat.session import ChatSession
from src.chat.shopper import Catalogue, ShopperGuide, Slots
from src.config import Config
from src.observability.metrics import METRICS
from src.observability.reporting import Reporter
from src.voice.cost import FLASH_LITE
from src.web.services.conversation_store import (
    Conversation,
    MemoryStore,
    inr_to_paise,
    paise_to_inr,
)
from src.web.services.dependencies import tool_session

log = logging.getLogger(__name__)

_HELLO = {"type", "v", "token", "last_seq", "context"}
_CONTEXT = {"host_origin", "host_session", "pincode", "product"}
_PRODUCT = {"product_id", "variant_id"}
_USER = {"type", "message_id", "text"}
_ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
_PIN = re.compile(r"^\d{6}$")
_SESSION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class EmptyCatalogue:
    async def search(self, *, brand: str | None, max_price: int | None) -> list[Any]:
        return []

    async def lookup(self, product_id: str, variant_id: str) -> None:
        return None


def socket_origin_allowed(origin: str | None, settings: Config) -> bool:
    """The iframe is on this service's origin. Anything else is refused."""
    if not origin:
        return False
    if settings.widget_public_origin:
        return origin == settings.widget_public_origin
    if settings.env == "development":
        return origin == "http://testserver" or origin.startswith(
            ("http://localhost", "http://127.0.0.1")
        )
    return False


def client_ip(headers: Any, fallback: str, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = str(headers.get("x-forwarded-for", "") or "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    return fallback or "unknown"


def _valid_product(product: Any) -> dict[str, str] | None:
    if not isinstance(product, dict) or set(product) - _PRODUCT:
        return None
    product_id = product.get("product_id") or ""
    variant_id = product.get("variant_id") or ""
    if not isinstance(product_id, str) or not _ID.match(product_id):
        return None
    if variant_id and (not isinstance(variant_id, str) or not _ID.match(variant_id)):
        return None
    return {"product_id": product_id, "variant_id": variant_id}


def parse_hello(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or set(payload) - _HELLO:
        return None
    if payload.get("type") != "hello" or payload.get("v") != 2:
        return None
    context = payload.get("context")
    if not isinstance(context, dict) or set(context) - _CONTEXT:
        return None
    origin = context.get("host_origin")
    if not isinstance(origin, str) or not origin.startswith("http"):
        return None
    return payload


def parse_user(payload: Any) -> tuple[str, str] | None:
    if not isinstance(payload, dict) or set(payload) - _USER:
        return None
    if payload.get("type") != "user_message":
        return None
    message_id = payload.get("message_id")
    text = payload.get("text")
    if not isinstance(message_id, str) or not _UUID.match(message_id):
        return None
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        return None
    return message_id, text.strip()


def worst_case_paise(
    input_tokens: int, max_output: int, usd_inr: float, gst: float
) -> int:
    usd = (
        input_tokens * FLASH_LITE.prompt_text + max_output * FLASH_LITE.response_text
    ) / 1_000_000
    return max(1, inr_to_paise(usd * usd_inr * (1 + gst)))


def usage_paise(usage: dict[str, Any], usd_inr: float, gst: float) -> int:
    prompt = int(usage.get("promptTokenCount") or 0)
    output = int(usage.get("candidatesTokenCount") or 0)
    thinking = int(usage.get("thoughtsTokenCount") or 0)
    usd = (
        prompt * FLASH_LITE.prompt_text + (output + thinking) * FLASH_LITE.response_text
    ) / 1_000_000
    return inr_to_paise(usd * usd_inr * (1 + gst))


class _Meter:
    def __init__(
        self,
        store: MemoryStore,
        conv: Conversation,
        client_key: str,
        message_id: str,
        settings: Config,
        session_box: dict[str, ChatSession],
    ) -> None:
        self._store = store
        self._conv = conv
        self._client_key = client_key
        self._message_id = message_id
        self._settings = settings
        self._box = session_box
        self.generations = 0

    def _rid(self, round_index: int) -> str:
        return f"{self._message_id}:{round_index}"

    async def before(self, round_index: int) -> bool:
        session = self._box.get("session")
        history = session.history if session is not None else []
        estimate = min(
            8000,
            max(500, len(self._settings.widget_instruction + json.dumps(history)) // 4),
        )
        amount = worst_case_paise(
            estimate,
            self._settings.chat_max_output_tokens,
            self._settings.usd_inr,
            self._settings.gst_rate,
        )
        result = await self._store.reserve(
            self._conv, self._client_key, self._rid(round_index), amount
        )
        if not result.ok:
            METRICS.count(
                "cap_reached_total", "Paid chat refused because a cap was reached"
            )
            return False
        self.generations += 1
        return True

    async def after(self, round_index: int, usage: dict[str, Any]) -> None:
        actual = usage_paise(usage, self._settings.usd_inr, self._settings.gst_rate)
        result = await self._store.settle(self._rid(round_index), actual)
        METRICS.count(
            "paid_generations_total", "Model generations that were reserved and settled"
        )
        if result.overrun:
            METRICS.count(
                "reservation_overrun_total",
                "Generations whose actual cost exceeded the reservation",
            )

    async def failed(self, round_index: int) -> None:
        await self._store.release(self._rid(round_index))


class WidgetRelay:
    def __init__(
        self,
        websocket: WebSocket,
        settings: Config,
        store: MemoryStore,
        reporter: Reporter,
        client: str,
    ) -> None:
        self._socket = websocket
        self._settings = settings
        self._store = store
        self._reporter = reporter
        self._client = client
        self._denylist = settings.neutrality_denylist

    async def run(self) -> None:
        try:
            hello = await self._socket.receive_json()
        except WebSocketDisconnect:
            return
        except Exception:
            await self._close(1008)
            return
        if parse_hello(hello) is None:
            await self._close(1008)
            return
        context = hello["context"]
        host_origin = str(context["host_origin"])
        if not self._host_allowed(host_origin):
            await self._close(1008)
            return
        conv, token = await self._open(hello, host_origin)
        replay = await self._store.events_after(
            conv.id, int(hello.get("last_seq") or 0)
        )
        last = replay[-1]["seq"] if replay else int(hello.get("last_seq") or 0)
        await self._socket.send_json(
            {
                "type": "welcome",
                "token": token,
                "conv_seq": last,
                "replay": replay,
                "context_label": conv.context_label,
            }
        )
        try:
            while True:
                incoming = await self._socket.receive_json()
                if isinstance(incoming, dict) and incoming.get("type") == "ping":
                    await self._socket.send_json({"type": "pong"})
                    continue
                parsed = parse_user(incoming)
                if parsed is None:
                    await self._socket.send_json(
                        {"type": "error", "code": "bad_message"}
                    )
                    continue
                await self._on_message(conv, parsed[0], parsed[1])
        except WebSocketDisconnect:
            log.info("widget chat disconnected")
        except Exception as failed:
            self._reporter.capture(failed, where="widget-chat")
            await self._send_quiet(
                {"type": "error", "code": "upstream", "message": SERVICE_ERROR}
            )

    async def _open(
        self, hello: dict[str, Any], host_origin: str
    ) -> tuple[Conversation, str]:
        context = hello["context"]
        product, invalid = self._product(context.get("product"))
        if invalid:
            METRICS.count(
                "context_invalid_total", "Host product ids that failed validation"
            )
        pincode = context.get("pincode") or ""
        if not isinstance(pincode, str) or not _PIN.match(pincode):
            pincode = ""
        host_session = context.get("host_session") or ""
        if not isinstance(host_session, str) or not _SESSION.match(host_session):
            host_session = ""
        token = hello.get("token")
        if isinstance(token, str) and token:
            resumed = await self._store.resume(token, host_origin)
            if resumed is not None:
                conv, rotated = resumed
                if product is not None:
                    conv.product = product
                elif invalid:
                    conv.product = None
                    conv.context_label = "this page"
                conv.pincode = pincode or conv.pincode
                await self._store.save(conv)
                return conv, rotated
        conv, minted = await self._store.create(
            host_origin=host_origin,
            host_session=host_session,
            pincode=pincode,
            product=product,
        )
        if invalid:
            conv.context_label = "this page"
            await self._store.save(conv)
        return conv, minted

    def _product(self, raw: Any) -> tuple[dict[str, str] | None, bool]:
        if raw is None:
            return None, False
        parsed = _valid_product(raw)
        if parsed is None:
            return None, True
        return parsed, False

    def _host_allowed(self, origin: str) -> bool:
        allowed = self._settings.widget_host_origins
        if allowed:
            return origin in allowed
        return self._settings.env == "development" and origin.startswith(
            ("http://localhost", "http://127.0.0.1", "https://localhost")
        )

    async def _on_message(self, conv: Conversation, message_id: str, text: str) -> None:
        METRICS.count(
            "chat_send_total", "Shopper messages accepted onto a widget socket"
        )
        claim = await self._store.claim_message(conv.id, message_id, text)
        if claim.status == "reused":
            await self._socket.send_json(
                {"type": "error", "code": "message_id_reused", "message_id": message_id}
            )
            return
        if claim.status in {"done", "in_progress"}:
            METRICS.count(
                "duplicate_prevented_total",
                "Repeated message ids that did not start another model call",
            )
            for event in claim.final_events:
                await self._socket.send_json(event)
            if claim.status == "in_progress" and not claim.final_events:
                await self._socket.send_json(
                    {"type": "ack", "message_id": message_id, "status": "in_progress"}
                )
            return
        await self._emit(
            conv.id, {"type": "ack", "message_id": message_id, "status": "accepted"}
        )
        if not self._settings.chat_paid_enabled:
            await self._finish_text(conv, message_id, text, UNAVAILABLE, [], [])
            await self._close(1013)
            return
        guide = ShopperGuide(self._catalogue(), pincode=conv.pincode)
        try:
            turn = await guide.respond(Slots.from_dict(conv.slots), text, conv.product)
        except Exception as failed:
            self._reporter.capture(failed, where="widget-guide")
            await self._finish_text(
                conv, message_id, text, SERVICE_ERROR, [], ["Try again"]
            )
            await self._store.fail_message(conv.id, message_id)
            return
        if turn.context_invalid:
            METRICS.count(
                "context_invalid_total", "Host product ids that failed validation"
            )
            conv.context_label = "this page"
        elif turn.context_label:
            conv.context_label = turn.context_label
        if turn.model_needed:
            await self._model(conv, message_id, text)
            return
        allowed = _card_tokens(turn.cards)
        shown, blocked = guard_display(
            turn.text,
            self._denylist,
            neutral_fallback([], conv.pincode),
            allowed,
        )
        if blocked:
            METRICS.count(
                "neutrality_block_total", "Replies replaced by the neutrality filter"
            )
        conv.slots = turn.slots
        await self._finish_text(
            conv, message_id, text, shown, turn.cards, turn.suggestions
        )

    async def _model(self, conv: Conversation, message_id: str, text: str) -> None:
        model = self._model_client()
        if model is None:
            await self._finish_text(
                conv, message_id, text, SERVICE_ERROR, [], ["Try again"]
            )
            await self._store.fail_message(conv.id, message_id)
            return
        box: dict[str, ChatSession] = {}
        meter = _Meter(
            self._store,
            conv,
            self._store.client_key(self._client),
            message_id,
            self._settings,
            box,
        )
        session = ChatSession(
            model,
            tools=self._tools(),
            sanitise=lambda result: sanitise_tool_result(result, self._denylist),
            hooks=meter,
        )
        box["session"] = session
        note = _slot_note(conv.slots)
        if note:
            session.remember(note)
        chunks: list[str] = []
        try:
            async for delta in session.send(text):
                if delta:
                    chunks.append(delta)
                    await self._emit(
                        conv.id,
                        {"type": "delta", "message_id": message_id, "text": delta},
                    )
        except Exception as failed:
            self._reporter.capture(failed, where="widget-model")
            await self._store.release(f"{message_id}:0")
            await self._finish_text(
                conv, message_id, text, SERVICE_ERROR, [], ["Try again"]
            )
            await self._store.fail_message(conv.id, message_id)
            return
        if session.stopped == "cap":
            await self._finish_text(conv, message_id, text, LIMIT_REACHED, [], [])
            return
        reply = "".join(chunks).strip() or SERVICE_ERROR
        shown, blocked = guard_display(
            reply, self._denylist, neutral_fallback([], conv.pincode)
        )
        if blocked:
            METRICS.count(
                "neutrality_block_total", "Replies replaced by the neutrality filter"
            )
        await self._finish_text(conv, message_id, text, shown, [], [])

    async def _finish_text(
        self,
        conv: Conversation,
        message_id: str,
        user_text: str,
        text: str,
        cards: list[dict[str, Any]],
        suggestions: list[str],
    ) -> None:
        conv.history.append(
            {"role": "user", "text": user_text, "message_id": message_id}
        )
        conv.history.append(
            {
                "role": "assistant",
                "text": text,
                "cards": cards,
                "suggestions": suggestions,
                "message_id": message_id,
            }
        )
        await self._store.save(conv)
        spent = paise_to_inr(await self._store.spent_paise(conv))
        remaining = round(max(0.0, self._settings.chat_session_cap_inr - spent), 2)
        await self._emit(
            conv.id,
            {
                "type": "final",
                "message_id": message_id,
                "display": {"lang": "en", "text": text},
                "speech": None,
                "cards": cards,
                "suggestions": suggestions,
            },
        )
        await self._emit(
            conv.id,
            {
                "type": "done",
                "message_id": message_id,
                "spend": {"conversation_inr": spent, "remaining_inr": remaining},
            },
        )
        await self._store.finish_message(conv.id, message_id, inr_to_paise(spent))

    async def _emit(self, conv_id: str, body: dict[str, Any]) -> None:
        seq = await self._store.append_event(conv_id, body)
        body = dict(body)
        body["seq"] = seq
        await self._socket.send_json(body)

    def _catalogue(self) -> Catalogue:
        override = getattr(self._socket.app.state, "widget_catalogue", None)
        if override is not None:
            return cast(Catalogue, override)
        return EmptyCatalogue()

    def _model_client(self) -> GeminiTextModel | None:
        override = getattr(self._socket.app.state, "widget_model", None)
        if override is not None:
            return cast(GeminiTextModel, override)
        if not self._settings.gemini_api_key:
            return None
        return GeminiTextModel(
            api_key=self._settings.gemini_api_key,
            model=self._settings.gemini_text_model,
            system_instruction=self._settings.widget_instruction,
            tools=None,
        )

    def _tools(self) -> Any:
        if getattr(self._socket.app.state, "widget_model", None) is not None:
            return getattr(self._socket.app.state, "widget_tools", None)
        if not self._settings.catalogue_token:
            return None
        return tool_session(self._settings)

    async def _close(self, code: int) -> None:
        try:
            await self._socket.close(code=code)
        except Exception:
            return

    async def _send_quiet(self, body: dict[str, Any]) -> None:
        try:
            await self._socket.send_json(body)
        except Exception:
            return


def _card_tokens(cards: list[dict[str, Any]]) -> tuple[str, ...]:
    tokens: list[str] = []
    for card in cards:
        for key in ("brand", "model_name", "variant_label"):
            value = card.get(key)
            if isinstance(value, str) and value:
                tokens.append(value)
    return tuple(tokens)


def _slot_note(slots: dict[str, Any]) -> str:
    parts: list[str] = []
    if slots.get("budget"):
        hardness = "hard" if slots.get("budget_hard") else "soft"
        parts.append(f"budget {slots['budget']} {hardness}")
    if slots.get("brand"):
        parts.append(f"brand {slots['brand']}")
    if slots.get("purpose"):
        parts.append(f"purpose {slots['purpose']}")
    if slots.get("needs"):
        parts.append("needs " + ",".join(slots["needs"]))
    if not parts:
        return ""
    return "Shopper constraints, already agreed: " + "; ".join(parts) + "."
