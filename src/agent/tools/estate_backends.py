"""The two ways to reach the per-item estate, and choosing between them.

Split out of `estate.py` so that file is about what a fact IS - the states,
the shapes, and reading one off a payload - and this one is about where it
comes from. The split matters because the parsing is pure and testable and
the transports are neither.

`estate.py` re-exports `build`, so callers still say
`estate_api.build(settings)` and do not care that there are two of these.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.agent.tools.estate import Backend, EstateUnavailable

if TYPE_CHECKING:
    from src.config import Config

log = logging.getLogger(__name__)


class WorkerEstate:
    """`POST /product` on the Search Blocks Worker. The production path.

    Everything the agent needs is one request, and the caching, the shared
    deadline and the in-flight deduplication happen on the Worker where they
    are shared between callers rather than per voice session.

    Not yet usable: the deployed Worker predates the route and answers
    "q is required" to a body with an item code. Until it is deployed this
    raises on first use and the composition root falls back.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        channel: str = "online",
        client_factory: Any = None,
    ) -> None:
        self._url = base_url.rstrip("/")
        self._token = token
        self._channel = channel
        self._client_factory = client_factory

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import httpx

        # Longer than the catalogue's 10s: this is up to five upstream calls
        # behind one request, and the Worker's own budget is 9s.
        return httpx.AsyncClient(timeout=12.0)

    async def product(
        self, item_code: str, sections: tuple[str, ...]
    ) -> dict[str, Any]:
        try:
            response = await self._client().post(
                self._url,
                headers={
                    "Content-Type": "application/json",
                    "X-Search-Token": self._token,
                },
                json={
                    "item_code": str(item_code),
                    "sections": list(sections),
                    "channel": self._channel,
                },
            )
        except Exception as exc:  # noqa: BLE001 - every transport failure is one thing
            raise EstateUnavailable(str(exc)[:200]) from exc

        if response.status_code != 200:
            raise EstateUnavailable(f"product service returned {response.status_code}")
        body = response.json()
        if "sections" not in body:
            # An older deployment without the route answers the search
            # endpoint's own error instead, which is the signal to fall back.
            raise EstateUnavailable(
                f"this Worker has no /product route: {str(body.get('error'))[:80]}"
            )
        return dict(body)


_WORKER_MODULES: dict[str, Any] = {}


def _worker_modules(src_path: str) -> tuple[Any, Any]:
    """Import the Worker's product service and run it here. Once per process.

    The Worker's only platform dependency is the `js` global - `fetch` and
    `AbortSignal` - and the repository already ships the shim that stands in
    for it, because its own probe runs this same code from a laptop. So this
    is not a port: `product.py`, `apis.py` and `adapters.py` are the objects
    the Worker runs, with httpx underneath instead of the runtime's fetch.

    What it does not prove: pyodide is not CPython, and latency measured in a
    container is not the Worker's latency.
    """
    if cached := _WORKER_MODULES.get(src_path):
        return cached["product"], cached["apis"]

    import sys
    from pathlib import Path

    root = Path(src_path).expanduser().resolve()
    worker, tools = root / "src" / "worker", root / "src" / "tools"
    if not (worker / "product.py").is_file():
        raise EstateUnavailable(f"no Worker product service at {worker}")
    for folder in (worker, tools):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))

    import httpx
    import worker_shim  # type: ignore[import-not-found]

    client = httpx.AsyncClient(
        # One pool, so five calls to one gateway cost one TLS handshake. The
        # Worker's runtime gives this for free; here it has to be asked for.
        limits=httpx.Limits(max_connections=12, max_keepalive_connections=12),
        follow_redirects=True,
    )

    async def fetch(url: str, options: Any) -> Any:
        spec = options if isinstance(options, dict) else dict(options)
        signal = spec.get("signal")
        timeout = signal.ms / 1000.0 if isinstance(signal, worker_shim.Signal) else 10.0
        got = await client.request(
            spec.get("method", "GET"),
            url,
            headers=dict(spec.get("headers") or {}),
            content=spec.get("body"),
            timeout=timeout,
        )
        return worker_shim.Response(got.status_code, got.text)

    worker_shim.install(fetch)

    import apis  # type: ignore[import-not-found]
    import product  # type: ignore[import-not-found]

    _WORKER_MODULES[src_path] = {"product": product, "apis": apis}
    return product, apis


class _Env:
    """The Cloudflare `env` binding, filled from this service's own config.

    Duck-typed on purpose: `apis.py` reads it with `getattr(env, name, "")`,
    so a missing host is an empty string and `apis.host()` reports it as
    not_configured rather than raising.
    """

    def __init__(self, api_env: str, secrets: dict[str, str]) -> None:
        self.API_ENV = api_env
        for name, value in secrets.items():
            setattr(self, name, value or "")


class InProcessEstate:
    """The Worker's product service, run inside this process.

    This is the Phase 1 path. It needs a checkout of the Worker rather than a
    deployment, which is the right trade while the route is unreleased and the
    wrong one for a container in Mumbai - hence `WorkerEstate` beside it.
    """

    def __init__(
        self,
        src_path: str,
        api_env: str,
        secrets: dict[str, str],
        channel: str = "online",
    ) -> None:
        self._src_path = src_path
        self._api_env = api_env
        self._secrets = secrets
        self._channel = channel

    def describe(self) -> dict[str, Any]:
        """Which services this environment can actually reach, and why not."""
        _, apis = _worker_modules(self._src_path)
        return dict(apis.describe(_Env(self._api_env, self._secrets)))

    async def product(
        self, item_code: str, sections: tuple[str, ...]
    ) -> dict[str, Any]:
        product, _ = _worker_modules(self._src_path)
        try:
            return dict(
                await product.one(
                    _Env(self._api_env, self._secrets),
                    str(item_code),
                    channel=self._channel,
                    sections=tuple(sections),
                )
            )
        except EstateUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - one failure mode to the caller
            raise EstateUnavailable(str(exc)[:200]) from exc


REPO_ROOT = Path(__file__).resolve().parents[3]


def build(config: Config) -> Backend | None:
    """The backend this deployment should use, or None to run without one.

    Returning None is a supported state, not a failure: the agent still finds
    products and still quotes the catalogue's own price, and says that stock
    and EMI could not be checked. That is a worse agent and an honest one.
    """
    backend = (config.estate_backend or "off").strip().lower()

    if backend == "worker":
        if not config.estate_worker_url:
            log.warning("ESTATE_BACKEND=worker but ESTATE_WORKER_URL is unset")
            return None
        return WorkerEstate(config.estate_worker_url, config.estate_worker_token)

    if backend == "inprocess":
        source = Path(config.estate_src_path).expanduser()
        if not source.is_absolute():
            # Relative to this repository, not to wherever uvicorn was started.
            source = (REPO_ROOT / source).resolve()
        if not (source / "src" / "worker" / "product.py").is_file():
            log.warning("no Worker checkout at %s; per-item facts are off", source)
            return None
        return InProcessEstate(
            str(source), config.estate_api_env, dict(config.estate_secrets)
        )

    if backend not in ("off", ""):
        log.warning("unknown ESTATE_BACKEND %r; per-item facts are off", backend)
    return None
