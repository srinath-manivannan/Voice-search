"""Building the things a call needs, in one place.

Both sockets want the same four objects and used to build them inline, which
is how the voice path and the chat path drift into disagreeing about the same
phone. Passing them in rather than constructing them inside the session is
also what lets the tests run the whole flow without a network.
"""

from __future__ import annotations

from typing import Any

from src.agent.tools import estate as estate_api
from src.agent.tools.catalogue import Catalogue
from src.agent.tools.declarations import tool_config
from src.agent.tools.runner import ToolSession
from src.config import Config


def catalogue_for(settings: Config) -> Catalogue | None:
    """Product search, when a token for it exists."""
    if not settings.catalogue_token:
        return None
    return Catalogue(
        settings.catalogue_url,
        settings.catalogue_token,
        settings.catalogue_origin,
    )


def tool_session(settings: Config) -> ToolSession:
    """One per call. A shortlist must never leak between customers."""
    return ToolSession(catalogue_for(settings), estate_api.build(settings))


def declarations_for(settings: Config) -> dict[str, Any] | None:
    """Tell the model about tools only when they can actually run.

    Declaring a search the server cannot perform produces an agent that
    promises to look something up and then reports a failure, which is worse
    than one that says plainly it cannot check.
    """
    return tool_config() if settings.catalogue_token else None


def tool_outcome(result: dict[str, Any]) -> dict[str, Any]:
    """What a tool answered, small enough to keep and enough to debug with.

    Counts and states, not products and prices. The states are the whole
    point: "the agent said there were no offers" and "the agent could not
    check offers" look identical in a transcript and are different bugs.
    """
    if error := result.get("error"):
        return {"error": error}
    if (products := result.get("products")) is not None:
        return {"products": len(products), "total": result.get("total_matching")}
    states = {
        part: value.get("state")
        for part, value in result.items()
        if isinstance(value, dict) and "state" in value
    }
    if could_not := result.get("could_not_check"):
        states["could_not_check"] = could_not
    return states
