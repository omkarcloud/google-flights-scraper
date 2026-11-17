"""BotGuard token minter for the FlightsFrontendService RPCs.

Every /_/FlightsFrontendUi/data/... RPC answers [13] unless the POST carries
`X-Goog-BatchExecute-Bgr: <token>`, where the token is computed by the page's
BotGuard VM over the request's exact `f.req` string (the web client's request
interceptor does `bgr = await runner.run(freq)`). Measured 2026-09-26:

  * the token binds to the f.req body only — not to the IP, cookies, f.sid
    or session: minted in this browser, replayed over curl_cffi from any
    exit (direct or residential), reusable, still valid 20+ minutes later;
  * minting is ~10 ms once the page is up (no network round trip).

Since 2026-10-21 no browser runs on GKE: ONE parked headed Chrome on the
browser box (recipes/google_flights.py, a TokenMint recipe) stays on
google.com/travel/flights and mints a token per f.req; GKE's curl_cffi does
all fetching and asks this module for a token per body, which is one
browser_client.token("google-flights", freq) call. The box owns the
browser's lifecycle (idle close, max_uses, max_age in the recipe).

Finding the runner without relying on minified names (they change every
release): the module namespace is window.default_FlightsFrontendUi_* ; the
BotGuard service is the class whose prototype has a method whose source
mentions "mnsUbf" (the WIZ key of the BotGuard program) and "initialize"
(2026-09-22 build: _.w2a.prototype.r3). The live instance is found through
CDP Runtime.queryObjects on that prototype, and the page gets
`window.__gfMint = f => instance[method]().run(f)` (main world). That page
work (_prepare, _MINT_JS) lives here and is imported by the recipe, so this
module must keep importing on the box, where browser_client is irrelevant
(hence the lazy import in TokenMinter.token).
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper_errors import UpstreamError

SITE = "google-flights"              # recipes/google_flights.py
HOME = "https://www.google.com/travel/flights?hl=en&gl=us&curr=USD"
PREPARE_TIMEOUT_S = 25

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
    """No BotGuard token could be minted (box down, busy, refused or over
    its re-mint budget); the caller may fall back to the token-free page."""


def _prepare(page):
    """Owner-thread op (runs on the box): load Flights, find the BotGuard
    service, bind __gfMint."""
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


TOKEN_TIMEOUT = 180   # seconds GKE waits for one token (cold: 20 s queue + 120 s warm)


class TokenMinter:
    """GKE side: a token per f.req from the browser box. Holds no browser."""

    def __init__(self):
        self.mints = 0
        self.recycles = 0
        self.last_error = None

    def token(self, freq):
        """BotGuard token for this exact f.req string, minted on the box.
        BoxUnavailable / BoxRefused / RemintBudgetExceeded -> MinterUnavailable
        (flights.py falls back to the server-rendered search page)."""
        import browser_client
        import config
        # No proxy (the token is not IP-bound), so every candidate would key
        # the one "direct" browser: name INPAGE_BROWSER_SLOTS x
        # BROWSER_BURST_FACTOR sessions instead and let the box pick an idle
        # parked page or open another while it has headroom (2026-11-04).
        candidates = [(None, f"flights#{i}") for i in range(config.burst_slots(config.INPAGE_BROWSER_SLOTS))]
        try:
            # a cold mint = up to 20 s queue + a 120 s warm on the box, past
            # the client's 60 s default (section F, 2026-11-04)
            _i, tok = browser_client.token_any(SITE, candidates, freq, timeout=TOKEN_TIMEOUT)
        except (browser_client.BoxUnavailable, browser_client.BoxRefused,
                browser_client.RemintBudgetExceeded) as e:
            self.last_error = f"{type(e).__name__}: {e}"
            raise MinterUnavailable(f"could not mint a BotGuard token: {e}") from e
        if not isinstance(tok, str) or len(tok) <= 100:
            self.last_error = f"short token {tok!r}"[:200]
            raise MinterUnavailable("browser box answered without a usable BotGuard token")
        self.mints += 1
        return tok

    def recycle(self):
        """Called by fetch.py when Google rejects a minted token. The box
        owns the parked browser (idle close, max_uses, max_age in the
        recipe), so there is nothing to tear down here; counted only."""
        self.recycles += 1

    def status(self):
        return {"running": False, "backend": "browser-box", "mints": self.mints,
                "recycles": self.recycles, "last_error": self.last_error}


MINTER = TokenMinter()
