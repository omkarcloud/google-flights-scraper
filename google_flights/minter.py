"""BotGuard token minter for the FlightsFrontendService RPCs.

Every /_/FlightsFrontendUi/data/... RPC answers [13] unless the POST carries
`X-Goog-BatchExecute-Bgr: <token>`, where the token is computed by the page's
BotGuard VM over the request's exact `f.req` string (the web client's request
interceptor does `bgr = await runner.run(freq)`). Measured 2026-09-26:

  * the token binds to the f.req body only — not to the IP, cookies, f.sid
    or session: minted in this browser, replayed over curl_cffi from any
    exit (direct or residential), reusable, still valid 20+ minutes later;
  * minting is ~10 ms once the page is up (no network round trip).

Re-measured 2026-11-28: no longer true. Google now accepts a token only on
a request with the minting Chrome's User-Agent (user_agent(), sent by
fetch.rpc) AND from that Chrome's IP: a local Chrome's tokens passed 8/8
from its own IP, 1/8 from a residential exit, 0-1/8 from a datacenter exit;
box tokens POSTed from a GKE pod passed 0/60 with or without the box's UA
(fetch.GoogleFlightsRefused). Page age is not a factor (36/36 over 26 min).

The box's own IP does not pass either, even with the RPC sent from the box
itself (2026-11-28: token from the Ubuntu box's Flights page, the RPC an
in-page fetch on the same box's google-finance page, same IP and Chrome:
round trip 1/50, one-way 6/50, Explore 2/20; the same pair of pages on this
Mac's IP: 10/10). So since 2026-11-28 the box Chrome runs on a sticky US
residential exit that GKE sends (EXIT_COUNTRY, browser_client.ExitPool),
token() names the exit the token was minted on, and fetch.rpc POSTs through
that same exit with the Chrome's UA. A Chrome on one residential exit with
the POST through the same exit, 2026-11-28: round trip 50/50, one-way
50/50, Explore 20/20 (the token minted ~10 ms on the page, the POST over
curl_cffi; the same searches sent from the page itself passed the same).
Where the recipe runs in this process (the kit: browser_worker.inprocess),
Chrome and curl_cffi share the machine's own IP and no exit is used.

Since 2026-10-21 no browser runs on GKE: parked headed Chromes on the
browser box (recipes/google_flights.py, a TokenMint recipe) stay on
google.com/travel/flights and mint a token per f.req; GKE's curl_cffi does
all fetching and asks this module for a token per body, which is one
browser_client.token_any("google-flights", candidates, freq) call. The box
owns the browser's lifecycle (idle close, max_uses, max_age in the recipe).

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
import re
import sys
import threading
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

# The token is only accepted on a request that carries the minting Chrome's
# User-Agent (2026-11-28, BFS-MAN round trip, tokens from a local headed
# Chrome sent from its own IP: 9/10 passed with the page's UA, 0/10 with
# curl_cffi's), so the same page also tells its UA: the input UA_INPUT
# answers navigator.userAgent instead of a token. A box still on the older
# _MINT_JS answers a token for that input, which user_agent() discards.
UA_INPUT = "__user_agent__"
_MINT_JS = ("async (freq) => freq === " + json.dumps(UA_INPUT) + " ? navigator.userAgent"
            " : (window.__gfMint ? await window.__gfMint(freq) : null)")
UA_TTL = 600          # seconds a known UA is kept (a box Chrome update changes it)
UA_RETRY = 60         # seconds before an unknown UA is asked again


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
# Country of the box Chromes' sticky residential exits (one exit per parked
# page; GKE's POST goes out through the exit its token was minted on).
EXIT_COUNTRY = "us"


def _same_machine():
    """True when the recipe runs in this process (browser_client.INPROCESS:
    the kit, a laptop): Chrome and curl_cffi already share one IP."""
    import browser_client
    try:
        return browser_client.box_url(SITE) == browser_client.INPROCESS
    except (browser_client.BoxUnavailable, KeyError):
        return False


def _new_exit():
    import config
    return config.residential_proxy(EXIT_COUNTRY, states=[])


_LOGIN_RE = re.compile(r"//[^/@\s]+:[^/@\s]+@")


def scrub(text):
    """`text` without proxy logins (an exit URL in an error message, now
    that the box is sent exits)."""
    return _LOGIN_RE.sub("//***@", str(text))


class TokenMinter:
    """GKE side: a token per f.req from the browser box, and the exit it was
    minted on. Holds no browser."""

    def __init__(self):
        self.mints = 0
        self.recycles = 0
        self.rotations = 0
        self.last_error = None
        self._ua = None
        self._ua_until = 0.0
        self._ua_lock = threading.Lock()
        self._exits = None
        self._exits_lock = threading.Lock()

    def _pool(self):
        """The sticky exits of the box Chromes (built on first use: this
        module also imports on the box, where browser_client is not used)."""
        with self._exits_lock:
            if self._exits is None:
                import browser_client
                import config
                self._exits = browser_client.ExitPool(SITE, _new_exit, size=lambda: config.INPAGE_BROWSER_SLOTS,
                                                      burst=True)
            return self._exits

    def _candidates(self):
        """The browsers the box may mint on: INPAGE_BROWSER_SLOTS x
        BROWSER_BURST_FACTOR sticky residential exits, one Chrome each (the
        box picks an idle parked page or opens another while it has
        headroom, 2026-11-04). In this process: as many sessions on the
        machine's own IP (no proxy, so they need names to be told apart)."""
        import config
        if _same_machine():
            return [(None, f"flights#{i}") for i in range(config.burst_slots(config.INPAGE_BROWSER_SLOTS))]
        return [(proxy, None) for proxy in self._pool().all()]

    def rotate(self, exit):
        """Replace `exit` (refused or blocked there, or its Chrome could not
        load Flights through it): the next mint offers a fresh exit, and the
        box opens a new Chrome on it (the old one idles out)."""
        if exit and self._pool().rotate(exit):
            self.rotations += 1

    def user_agent(self):
        """navigator.userAgent of the Chrome that mints the tokens (every
        candidate is the same Chrome build on the same machine), cached
        UA_TTL; None when it cannot be told (the box down, or still on the
        older _MINT_JS)."""
        import browser_client
        with self._ua_lock:
            if time.monotonic() < self._ua_until:
                return self._ua
            try:
                _i, ua = browser_client.token_any(SITE, self._candidates(), UA_INPUT, timeout=TOKEN_TIMEOUT)
            except (browser_client.BoxUnavailable, browser_client.BoxRefused,
                    browser_client.RemintBudgetExceeded) as e:
                self.last_error = scrub(f"user agent: {type(e).__name__}: {e}")
                # as in token(): fetch.rpc asks the UA before the token, so a
                # dead exit is met here first
                self.rotate(getattr(e, "proxy", None))
                ua = None
            ua = ua if isinstance(ua, str) and ua.startswith("Mozilla/") else None
            # Counted from the answer, not from the ask: the ask can take up
            # to TOKEN_TIMEOUT (180 s), longer than UA_RETRY, and a deadline
            # set from before it was already past when a slow box failed.
            # Every caller waiting on this lock then asked again in turn,
            # 180 s each, instead of taking the answer just learned.
            self._ua, self._ua_until = ua, time.monotonic() + (UA_TTL if ua else UA_RETRY)
            return ua

    def token(self, freq):
        """(token, exit): a BotGuard token for this exact f.req string,
        minted on the box, and the exit of the Chrome that minted it (the
        proxy URL fetch.rpc must POST through; None = the machine's own IP).
        BoxUnavailable / BoxRefused / RemintBudgetExceeded -> MinterUnavailable
        (flights.py falls back to the server-rendered search page); the exit
        of the candidate it failed on is rotated first."""
        import browser_client
        candidates = self._candidates()
        try:
            # a cold mint = up to 20 s queue + a 120 s warm on the box, past
            # the client's 60 s default (section F, 2026-11-04)
            i, tok = browser_client.token_any(SITE, candidates, freq, timeout=TOKEN_TIMEOUT)
        except (browser_client.BoxUnavailable, browser_client.BoxRefused,
                browser_client.RemintBudgetExceeded) as e:
            self.last_error = scrub(f"{type(e).__name__}: {e}")
            # Whichever error names an exit (`proxy`: the candidate the box
            # was on), not BoxRefused alone: a Chrome that cannot load
            # Flights through its exit fails in warm() with a Page.goto
            # timeout or _prepare's MinterUnavailable, neither a RecipeError,
            # so the box answers 500, which is a BoxUnavailable. Rotating on
            # BoxRefused only left that exit in the pool, and the box
            # launches the first candidate without a browser, so a cold box
            # tried the same dead exit on every mint.
            self.rotate(getattr(e, "proxy", None))
            raise MinterUnavailable(scrub(f"could not mint a BotGuard token: {e}")) from e
        if not isinstance(tok, str) or len(tok) <= 100:
            self.last_error = f"short token {tok!r}"[:200]
            raise MinterUnavailable("browser box answered without a usable BotGuard token")
        self.mints += 1
        return tok, candidates[i][0]

    def recycle(self):
        """Called by fetch.py when Google rejects a minted token. The box
        owns the parked browser (idle close, max_uses, max_age in the
        recipe), so there is nothing to tear down here; counted only."""
        self.recycles += 1

    def status(self):
        return {"running": False, "backend": "in-process" if _same_machine() else "browser-box",
                "mints": self.mints, "recycles": self.recycles, "exit_rotations": self.rotations,
                "user_agent_known": bool(self._ua), "last_error": self.last_error}


MINTER = TokenMinter()
