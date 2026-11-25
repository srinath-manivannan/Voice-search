"""Let the Worker's own modules import and run outside Cloudflare.

`apis.py` and `product.py` are written for pyodide: they import `js` and call
`js.fetch`. That is correct for the Worker and useless for a test, so this
installs the two globals they need and takes the fetch implementation as an
argument. Everything else — the deadline, the cache, the deduplication, the
fan-out, the adapters — is then the real code, not a copy of it.

    import worker_shim
    worker_shim.install(my_fetch)      # must be async (url, options) -> resp
    import apis, product
"""
from __future__ import annotations

import json
import sys
import types


class Signal:
    def __init__(self, ms):
        self.ms = ms


class Signals:
    @staticmethod
    def timeout(ms):
        return Signal(ms)


class Response:
    """Only the three members `apis._request` touches."""

    def __init__(self, status: int, text: str):
        self.status = status
        self.ok = 200 <= status < 300
        self._text = text

    async def text(self):
        return self._text


def install(fetch) -> None:
    js = types.ModuleType("js")
    js.fetch = fetch
    js.AbortSignal = Signals
    js.Object = types.SimpleNamespace(fromEntries=dict)
    js.JSON = types.SimpleNamespace(stringify=json.dumps)
    sys.modules["js"] = js

    ffi = types.ModuleType("pyodide.ffi")
    # to_js is the identity here: a dict is what the stub fetch wants anyway.
    ffi.to_js = lambda value, **kw: value
    pyodide = types.ModuleType("pyodide")
    pyodide.ffi = ffi
    sys.modules["pyodide"] = pyodide
    sys.modules["pyodide.ffi"] = ffi
