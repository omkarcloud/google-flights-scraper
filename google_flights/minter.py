"""BotGuard token minter for the FlightsFrontendService RPCs.

Every /_/FlightsFrontendUi/data/... RPC answers [13] unless the POST carries
`X-Goog-BatchExecute-Bgr: <token>`, where the token is computed by the page's
BotGuard VM over the request's exact `f.req` string (the web client's request
interceptor does `bgr = await runner.run(freq)`). Measured 2026-09-26:

  * the token binds to the f.req body only — not to the IP, cookies, f.sid
    or session: minted in this browser, replayed over curl_cffi from any
    exit (direct or residential), reusable, still valid 20+ minutes later;
  * minting is ~10 ms once the page is up (no network round trip).

So ONE long-lived patchright Chrome parked on google.com/travel/flights is
enough for the whole service: curl_cffi does all fetching and asks this
module for a token per body.

Finding the runner without relying on minified names (they change every
release): the module namespace is window.default_FlightsFrontendUi_* ; the
BotGuard service is the class whose prototype has a method whose source
mentions "mnsUbf" (the WIZ key of the BotGuard program) and "initialize"
(2026-09-22 build: _.w2a.prototype.r3). The live instance is found through
CDP Runtime.queryObjects on that prototype, and the page gets
`window.__gfMint = f => instance[method]().run(f)` (main world).

The browser is recycled after GOOGLE_FLIGHTS_MINTER_MAX_AGE seconds and
whenever a mint fails or Google rejects a minted token (fetch.py calls
recycle()).
"""
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from scraper_errors import UpstreamError

HOME = "https://www.google.com/travel/flights?hl=en&gl=us&curr=USD"
PREPARE_TIMEOUT_S = 25
MINT_TIMEOUT_S = 20

_FIND_JS = r"""() => {
  const nsKey = Object.keys(window).find(k => k.startsWith('default_FlightsFrontendUi'));
  if (!nsKey) return null;
  const ns = window[nsKey];
  for (const k of Object.keys(ns)) {
    const v = ns[k];
    if (typeof v !== 'function' || !v.prototype) continue;
    for (const m of Object.getOwnPropertyNames(v.prototype)) {
      let f;
      try { f = v.prototype[m]; } catch (e) { continue; }
      if (typeof f !== 'function') continue;
      const s = Function.prototype.toString.call(f);
      if (s.includes('"mnsUbf"') && s.includes('initialize')) return {nsKey, cls: k, method: m};
    }
  }
  return null;
}"""

_BIND_FN = """function(method) {
  const svc = this[0];
  window.__gfMint = async (freq) => await svc[method]().run(freq);
  return this.length;
}"""

_MINT_JS = "async (freq) => window.__gfMint ? await window.__gfMint(freq) : null"


class MinterUnavailable(UpstreamError):
    """The token browser could not start or mint (the caller may fall back)."""


def _prepare(page):
    """Owner-thread op: load Flights, find the BotGuard service, bind __gfMint."""
    page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
    deadline = time.monotonic() + PREPARE_TIMEOUT_S
    found = None
    while time.monotonic() < deadline:
        found = page.evaluate(_FIND_JS, isolated_context=False)
        if found:
            break
        page.wait_for_timeout(500)
    if not found:
        raise MinterUnavailable("BotGuard service class not found (Google Flights bundle changed?)")
    cdp = page.context.new_cdp_session(page)
    try:
        while True:
            proto = cdp.send("Runtime.evaluate", {
                "expression": f"window[{json.dumps(found['nsKey'])}][{json.dumps(found['cls'])}].prototype"})
            object_id = (proto.get("result") or {}).get("objectId")
            if not object_id:
                raise MinterUnavailable("BotGuard service prototype not reachable")
            instances = cdp.send("Runtime.queryObjects", {"prototypeObjectId": object_id})
            bound = cdp.send("Runtime.callFunctionOn", {
                "objectId": instances["objects"]["objectId"],
                "functionDeclaration": _BIND_FN,
                "arguments": [{"value": found["method"]}],
                "returnByValue": True})
            count = (bound.get("result") or {}).get("value") or 0
            if count:
                break
            if time.monotonic() > deadline:
                raise MinterUnavailable("BotGuard service not instantiated on the page")
            page.wait_for_timeout(500)
    finally:
        try:
            cdp.detach()
        except Exception:
            pass
    return found


class TokenMinter:
    def __init__(self):
        self._lock = threading.Lock()
        self._driver = None
        self._born = 0.0
        self.mints = 0
        self.info = None

    def _start(self):
        from chrome_manager import create_scope
        from patchright_driver import PatchrightDriver
        with create_scope():
            driver = PatchrightDriver(proxy_url=config.google_flights_minter_proxy(),
                                      headless=config.GOOGLE_FLIGHTS_MINTER_HEADLESS)
        try:
            self.info = driver.call(_prepare, timeout=PREPARE_TIMEOUT_S + 70)
        except Exception:
            try:
                driver.close()
            except Exception:
                pass
            raise
        self._driver = driver
        self._born = time.monotonic()
        print(f"google-flights: token minter ready ({self.info})")

    def _ensure(self):
        """The live driver (started or recycled as needed). Caller holds _lock."""
        expired = self._driver is not None and time.monotonic() - self._born > config.GOOGLE_FLIGHTS_MINTER_MAX_AGE
        if expired:
            self._close()
        if self._driver is None:
            try:
                self._start()
            except MinterUnavailable:
                raise
            except Exception as e:
                raise MinterUnavailable(f"token browser failed to start: {type(e).__name__}: {e}")
        return self._driver

    def _close(self):
        driver, self._driver = self._driver, None
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass

    def recycle(self):
        with self._lock:
            self._close()

    def token(self, freq):
        """BotGuard token for this exact f.req string (two tries, the second
        on a fresh browser)."""
        last = None
        for attempt in range(2):
            with self._lock:
                driver = self._ensure()
            try:
                tok = driver.call(lambda page: page.evaluate(_MINT_JS, freq, isolated_context=False),
                                  timeout=MINT_TIMEOUT_S)
            except Exception as e:
                last = e
                tok = None
            if isinstance(tok, str) and len(tok) > 100:
                self.mints += 1
                return tok
            print(f"google-flights: mint attempt {attempt + 1} failed ({last or tok!r}); recycling the browser")
            self.recycle()
        raise MinterUnavailable(f"could not mint a BotGuard token: {last}")

    def status(self):
        return {"running": self._driver is not None, "mints": self.mints,
                "age_seconds": int(time.monotonic() - self._born) if self._driver else None}


MINTER = TokenMinter()


def warm_async():
    """Start the browser in the background at boot (first RPC then skips the
    ~5-10 s launch)."""
    def _warm():
        try:
            with MINTER._lock:
                MINTER._ensure()
        except Exception as e:
            print(f"google-flights: minter warm-up failed: {type(e).__name__}: {e}")
    threading.Thread(target=_warm, name="google-flights-minter-warm", daemon=True).start()
